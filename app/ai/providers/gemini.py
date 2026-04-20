"""
NEXYA Couche IA — Provider Google Gemini (chat) et Imagen (images) via Vertex AI.

Traduit les types neutres de `base.py` vers les appels Vertex AI, et mappe
toutes les erreurs du SDK vers `ProviderError` pour que le router et le
circuit breaker puissent réagir uniformément.

Models supportés :
- Chat   : `gemini-2.5-flash` (défaut, rapide, $0.001/req) et `gemini-2.5-pro` (réflexion profonde)
- Images : `imagen-3.0-generate-002`
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

import structlog

from app.config import settings

from .base import (
    ChatChunk,
    ChatCompletionRequest,
    ChatProvider,
    ChatUsage,
    FinishReason,
    GeneratedImage,
    ImageGenerationRequest,
    ImageProvider,
    ProviderAuthError,
    ProviderCapability,
    ProviderContentFilteredError,
    ProviderError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)

if TYPE_CHECKING:
    from google.genai import Client


log = structlog.get_logger()


# ═══════════════════════════════════════════════════════════════════
# CLIENT SINGLETON — construit paresseusement
# ═══════════════════════════════════════════════════════════════════
#
# On ne crée le client qu'à la première utilisation pour :
# 1. Éviter un crash à l'import si les variables GCP ne sont pas chargées
#    (ex: tests unitaires, scripts de seed).
# 2. Laisser la config être résolue au démarrage d'uvicorn.
# ═══════════════════════════════════════════════════════════════════

_client: Client | None = None


def _get_client() -> Client:
    """Retourne le client Vertex AI partagé, créé à la première demande."""
    global _client
    if _client is None:
        from google import genai  # import local — dépendance lourde

        _client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.gcp_location,
        )
        log.info(
            "ai.provider.gemini.client_initialized",
            project=settings.gcp_project_id,
            location=settings.gcp_location,
        )
    return _client


# ═══════════════════════════════════════════════════════════════════
# MAPPING DES ERREURS Vertex AI → ProviderError
# ═══════════════════════════════════════════════════════════════════


def _map_sdk_exception(exc: Exception, *, model: str) -> ProviderError:
    """Traduit une exception du SDK google-genai en `ProviderError` typée.

    On inspecte le status code si présent. Sinon on retombe sur un code
    neutre (`ProviderUnavailableError`) — mieux vaut un fallback que crasher.
    """
    # google.genai lève des APIError avec attribut `code` (status HTTP)
    status_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    message = str(exc) or exc.__class__.__name__

    if status_code == 401 or status_code == 403:
        return ProviderAuthError(message, provider="gemini", model=model)
    if status_code == 429:
        return ProviderRateLimitError(message, provider="gemini", model=model)
    if status_code == 400:
        # Les 400 sur Gemini sont souvent des violations de safety
        if "safety" in message.lower() or "blocked" in message.lower():
            return ProviderContentFilteredError(message, provider="gemini", model=model)
        return ProviderInvalidRequestError(message, provider="gemini", model=model)

    # 5xx, timeout, connection reset… → retryable
    return ProviderUnavailableError(
        message, provider="gemini", model=model, status_code=status_code
    )


# ═══════════════════════════════════════════════════════════════════
# GeminiChatProvider — streaming texte
# ═══════════════════════════════════════════════════════════════════


class GeminiChatProvider(ChatProvider):
    """Adaptateur Vertex AI pour les modèles Gemini en mode chat streaming."""

    name = "gemini"
    default_model = "gemini-2.5-flash"
    supported_models = frozenset(
        {
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-1.5-flash",
            "gemini-1.5-pro",
        }
    )
    capabilities = frozenset(
        {
            ProviderCapability.TEXT_CHAT,
            ProviderCapability.VISION,
            ProviderCapability.FUNCTION_CALLING,
            ProviderCapability.JSON_MODE,
        }
    )
    # Gemini 2.5 : 1M tokens de contexte ; on garde une marge
    max_context_tokens = 900_000

    async def stream_chat(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[ChatChunk]:
        from google.genai import types  # import local — dépendance lourde

        model = request.model or self.default_model
        if not self.supports_model(model):
            raise ProviderInvalidRequestError(
                f"Modèle '{model}' non supporté par le provider Gemini.",
                provider=self.name,
                model=model,
            )

        contents = _messages_to_vertex_contents(request, types)
        config_kwargs: dict[str, Any] = {"temperature": request.temperature}
        if request.system_prompt:
            config_kwargs["system_instruction"] = request.system_prompt
        if request.max_tokens is not None:
            config_kwargs["max_output_tokens"] = request.max_tokens
        if request.stop_sequences:
            config_kwargs["stop_sequences"] = list(request.stop_sequences)

        client = _get_client()

        try:
            stream = await client.aio.models.generate_content_stream(
                model=model,
                contents=contents,
                config=types.GenerateContentConfig(**config_kwargs),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — on traduit en ProviderError
            raise _map_sdk_exception(exc, model=model) from exc

        last_usage: ChatUsage | None = None
        last_finish: FinishReason | None = None
        produced_any = False

        try:
            async for chunk in stream:
                # Métadonnées éventuelles (pas toujours présentes avant la fin)
                if getattr(chunk, "usage_metadata", None) is not None:
                    last_usage = _extract_usage(chunk.usage_metadata)

                if chunk.candidates:
                    cand = chunk.candidates[0]
                    finish = getattr(cand, "finish_reason", None)
                    if finish is not None:
                        last_finish = _map_finish_reason(finish)

                text = getattr(chunk, "text", None)
                if text:
                    produced_any = True
                    yield ChatChunk(delta=text)
        except asyncio.CancelledError:
            # L'appelant (endpoint ou worker) annule : on propage proprement.
            raise
        except Exception as exc:  # noqa: BLE001
            raise _map_sdk_exception(exc, model=model) from exc

        # Chunk final — toujours yield pour signaler la fin, même si aucune sortie
        if last_finish is None:
            last_finish = FinishReason.STOP if produced_any else FinishReason.ERROR
        yield ChatChunk(delta="", finish_reason=last_finish, usage=last_usage)

    async def health_check(self) -> bool:
        """Ping léger — un appel minimal pour valider l'authentification."""
        try:
            client = _get_client()
            # Un appel `list_models` est la méthode la moins chère
            models = await asyncio.to_thread(lambda: list(client.models.list()))
            return len(models) > 0
        except Exception:  # noqa: BLE001
            return False


# ═══════════════════════════════════════════════════════════════════
# GeminiImageProvider — génération d'images via Imagen 3
# ═══════════════════════════════════════════════════════════════════


class GeminiImageProvider(ImageProvider):
    """Adaptateur Imagen 3 via Vertex AI. Supporte jusqu'à 4 images par appel."""

    name = "gemini-imagen"
    default_model = "imagen-3.0-generate-002"
    supported_models = frozenset({"imagen-3.0-generate-002"})
    max_images_per_call = 4

    async def generate_images(
        self, request: ImageGenerationRequest
    ) -> list[GeneratedImage]:
        from google.genai import types

        count = max(1, min(request.count, self.max_images_per_call))
        client = _get_client()

        config_kwargs: dict[str, Any] = {
            "numberOfImages": count,
            "aspectRatio": request.aspect_ratio,
            "outputMimeType": "image/jpeg",
        }
        if request.negative_prompt:
            config_kwargs["negativePrompt"] = request.negative_prompt

        try:
            response = await client.aio.models.generate_images(
                model=self.default_model,
                prompt=request.prompt,
                config=types.GenerateImagesConfig(**config_kwargs),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise _map_sdk_exception(exc, model=self.default_model) from exc

        images: list[GeneratedImage] = []
        if response.generated_images:
            for img in response.generated_images:
                b64 = base64.b64encode(img.image.image_bytes).decode("utf-8")
                images.append(GeneratedImage(base64_data=b64, mime_type="image/jpeg"))

        if not images:
            # Aucun résultat = soit safety filter, soit bug côté provider
            raise ProviderContentFilteredError(
                "Aucune image n'a pu être générée (filtre de sécurité ou erreur provider).",
                provider=self.name,
                model=self.default_model,
            )
        return images


# ═══════════════════════════════════════════════════════════════════
# HELPERS DE TRADUCTION
# ═══════════════════════════════════════════════════════════════════


def _messages_to_vertex_contents(request: ChatCompletionRequest, types: Any) -> list[Any]:
    """Convertit la liste de `ChatMessage` vers le format `types.Content` de Vertex AI.

    Note sur le rôle :
    - Gemini utilise "user" et "model" (pas "assistant").
    - Les messages system ne sont pas dans le flux : ils vont dans `system_instruction`.
    """
    contents: list[Any] = []
    for msg in request.messages:
        if msg.role == "system":
            # Les system messages inline sont ignorés — on s'attend à ce que
            # le ContextBuilder les consolide dans `request.system_prompt`.
            continue
        role = "user" if msg.role == "user" else "model"
        contents.append(
            types.Content(
                role=role,
                parts=[types.Part.from_text(text=msg.content)],
            )
        )
    return contents


def _extract_usage(usage_metadata: Any) -> ChatUsage | None:
    """Parse les métadonnées de consommation Gemini — best effort.

    La forme exacte varie selon la version du SDK. On tente plusieurs
    attributs connus et on tombe sur None si rien n'est exploitable.
    """
    try:
        prompt = int(
            getattr(usage_metadata, "prompt_token_count", None)
            or getattr(usage_metadata, "input_tokens", None)
            or 0
        )
        completion = int(
            getattr(usage_metadata, "candidates_token_count", None)
            or getattr(usage_metadata, "output_tokens", None)
            or 0
        )
        total = int(
            getattr(usage_metadata, "total_token_count", None)
            or (prompt + completion)
        )
        if prompt == 0 and completion == 0:
            return None
        return ChatUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
        )
    except Exception:  # noqa: BLE001
        return None


def _map_finish_reason(raw: Any) -> FinishReason:
    """Traduit un `FinishReason` Vertex AI vers notre enum neutre."""
    name = getattr(raw, "name", None) or str(raw)
    name_upper = name.upper()
    if "STOP" in name_upper:
        return FinishReason.STOP
    if "MAX_TOKENS" in name_upper or "LENGTH" in name_upper:
        return FinishReason.LENGTH
    if "SAFETY" in name_upper or "BLOCKED" in name_upper or "RECITATION" in name_upper:
        return FinishReason.CONTENT_FILTER
    return FinishReason.ERROR
