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
import json
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
    ToolCallDelta,
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


def _reset_client_for_tests() -> None:
    """Réinitialise le singleton client (isolation tests).

    Pattern aligné `anthropic_provider`, `openai_provider`, `qwen_provider`,
    `openrouter_provider`, `gemini_vision`, `openai_voice`, `openai_vision`.
    Appelé en fixture pour garantir qu'un test qui monkeypatch `_get_client`
    ne pollue pas le singleton d'un test suivant.
    """
    global _client
    _client = None


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

    async def stream_chat(self, request: ChatCompletionRequest) -> AsyncIterator[ChatChunk]:
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

        # G2 V1.1 2026-05-18 — Désactivation explicite du thinking mode
        # (Gemini 2.5 Pro/Flash) pour les experts dont le `ExpertConfig.disable_thinking=True`.
        # Cause : Gemini 2.5 Pro a un `thinkingBudget=-1` (adaptatif) par défaut
        # qui peut consommer 5-25k tokens de raisonnement AVANT de produire le
        # premier token de réponse — incompatible UX chat live (mesuré 25-30s
        # par appel sur le blind test G2). Pour cooking où on n'a pas besoin
        # de raisonnement multi-étapes (recette = format structuré), on coupe.
        # Cap réduit la latence first-token de ~20s à ~3s sur Gemini 2.5 Pro,
        # qualité préservée car les réponses cooking sont du formatage de
        # contenu (issu du corpus RAG), pas du raisonnement.
        #
        # [Bug-experts-Pro 2026-05-23] **DIFFÉRENCIATION FLASH vs PRO obligatoire.**
        # Cause racine bug terrain Ivan « 5 experts ne répondent pas » :
        # l'API Gemini accepte `thinking_budget` selon des ranges spécifiques
        # par modèle (cf. https://ai.google.dev/gemini-api/docs/thinking) :
        #   - **Gemini 2.5 Flash**     : range [0, 24576] OU -1 (dynamic)
        #                                → `0` désactive complètement le thinking ✅
        #   - **Gemini 2.5 Flash-Lite** : range [512, 24576] OU 0 OU -1
        #                                → `0` accepté pour off ✅
        #   - **Gemini 2.5 Pro**       : range [128, 32768] OU -1 (dynamic)
        #                                → `0` **REJETÉ par l'API** ❌
        # Le code précédent posait `thinking_budget=0` pour TOUS les modèles
        # → les 5 experts Pro (Langue, Sciences, Ingénierie, Médecine, Légal)
        # recevaient un stream vide silencieux (l'API rejette le payload mais
        # le SDK ne lève pas toujours d'exception nette — selon version, on
        # obtient soit `parts=[]` soit un 400 mal mappé). Cuisine/Informatique/
        # Général/Finance/Productivité fonctionnaient car ils sont sur Flash.
        # Le fix : poser le **minimum API** `128` pour Pro (ce qui revient à
        # ~1.5% du budget sur max_tokens=8192, négligeable côté coût et
        # latence — on récupère 99% du budget pour la réponse réelle).
        # Détection robuste via `"pro" in model.lower()` — couvre `gemini-2.5-pro`,
        # futur `gemini-3-pro`, et anciens `gemini-1.5-pro` (qui n'expose pas
        # thinking, mais le SDK ignore proprement le param dans ce cas).
        if request.extra.get("disable_thinking") is True:
            if "pro" in model.lower():
                # Gemini 2.5 Pro refuse `thinking_budget=0` → minimum API forcé.
                # 128 = 0.4% du budget thinking max, libère 99% pour la réponse.
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=128)
            else:
                # Gemini 2.5 Flash / Flash-Lite : `0` accepté pour off complet.
                config_kwargs["thinking_config"] = types.ThinkingConfig(thinking_budget=0)

        # F2.5 — function calling. Format spécifique Gemini :
        # `tools=[{function_declarations: [{name, description, parameters}]}]`
        # — wrapper `function_declarations` obligatoire (Gemini supporte
        # plusieurs « tool sets » : function_declarations, retrieval, etc.).
        # Le `parameters` JSON Schema est passé tel quel ; si un type non
        # supporté par Gemini est présent, le SDK lève une `BadRequest`
        # qu'on remontera en `ProviderInvalidRequestError` via le mapper.
        if request.tools:
            gemini_tools = _to_gemini_tools(request.tools)
            if gemini_tools:
                config_kwargs["tools"] = gemini_tools
                # [planner-from-chat LOT 5] — tool_config dynamique AUTO/ANY.
                # Historique Bug-010 : Gemini 2.5 Flash ignorait souvent les
                # tools en `mode=AUTO`. Le fix : quand l'intent classifier a
                # détecté une demande de planification claire (round 0,
                # `request.extra["force_tool_call"]` posé par
                # `streaming._run_link`), on bascule en `mode=ANY` — Gemini
                # DOIT alors émettre un function_call. Garantit que
                # `create_task` parte même si Flash flake. Les rounds suivants
                # de l'orchestrateur repassent en `AUTO` (force absent) pour
                # laisser le LLM produire sa réponse texte de confirmation —
                # sinon il serait forcé d'enchaîner un tool call à l'infini.
                force_tool = bool(request.extra.get("force_tool_call"))
                config_kwargs["tool_config"] = {
                    "function_calling_config": {"mode": "ANY" if force_tool else "AUTO"}
                }

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

        # F2.5 — détection function_call dans les parts. Gemini ne stream
        # PAS les arguments fragment par fragment comme OpenAI/Anthropic :
        # le `function_call` arrive en un chunk unique avec son `name` et
        # ses `args` complets. On émet alors un seul `ChatChunk` portant
        # un `ToolCallDelta` complet (id synthétique, args sérialisés JSON).
        function_call_seen = False

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

                    # Cherche un function_call dans les parts du candidate.
                    content = getattr(cand, "content", None)
                    parts = getattr(content, "parts", None) if content is not None else None
                    if parts:
                        for part_index, part in enumerate(parts):
                            fc = getattr(part, "function_call", None)
                            if fc is None:
                                continue
                            fc_name = getattr(fc, "name", None) or ""
                            if not fc_name:
                                continue
                            fc_args_raw = getattr(fc, "args", None)
                            args_json = _gemini_args_to_json(fc_args_raw)
                            function_call_seen = True
                            yield ChatChunk(
                                tool_call=ToolCallDelta(
                                    id=f"call_gemini_{part_index}",
                                    name=fc_name,
                                    arguments_json_partial=args_json,
                                    index=part_index,
                                )
                            )

                text = getattr(chunk, "text", None)
                if text:
                    produced_any = True
                    yield ChatChunk(delta=text)
        except asyncio.CancelledError:
            # L'appelant (endpoint ou worker) annule : on propage proprement.
            raise
        except Exception as exc:  # noqa: BLE001
            raise _map_sdk_exception(exc, model=model) from exc

        # Si Gemini a émis un function_call, on force `FinishReason.TOOL_CALLS`
        # pour signaler à l'orchestrateur qu'il doit exécuter le tool —
        # même si Gemini a renvoyé `finish_reason=STOP` (cas standard avec
        # function_call : le LLM a fini son tour de parole, mais il attend
        # un résultat tool avant de répondre à l'user).
        if function_call_seen:
            last_finish = FinishReason.TOOL_CALLS

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

    async def generate_images(self, request: ImageGenerationRequest) -> list[GeneratedImage]:
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
        total = int(getattr(usage_metadata, "total_token_count", None) or (prompt + completion))
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
    if "FUNCTION_CALL" in name_upper or "TOOL" in name_upper:
        return FinishReason.TOOL_CALLS
    return FinishReason.ERROR


# ═══════════════════════════════════════════════════════════════════
# F2.5 — Helpers function calling Gemini
# ═══════════════════════════════════════════════════════════════════


def _to_gemini_tools(
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Convertit le format OpenAI natif vers le format Gemini.

    Entrée :
        [{"type": "function",
          "function": {"name": ..., "description": ..., "parameters": {...}}}]

    Sortie :
        [{"function_declarations": [{"name", "description", "parameters"}]}]

    On retourne UN SEUL tool set qui groupe toutes les `function_declarations`
    — c'est le pattern recommandé par Google quand on n'utilise que du
    function calling (pas de `retrieval` ni `code_execution` mélangés).
    """
    declarations: list[dict[str, Any]] = []
    for raw in tools:
        if not isinstance(raw, dict):
            continue
        fn = raw.get("function") if raw.get("type") == "function" else raw
        if not isinstance(fn, dict):
            continue
        name = fn.get("name")
        if not name:
            continue
        decl: dict[str, Any] = {"name": name}
        if "description" in fn:
            decl["description"] = fn["description"]
        # `parameters` JSON Schema. Gemini supporte le subset standard
        # (STRING/NUMBER/INTEGER/BOOLEAN/ARRAY/OBJECT) — un type non
        # supporté lèvera côté SDK, qu'on remonte via `_map_sdk_exception`.
        if "parameters" in fn and fn["parameters"]:
            decl["parameters"] = fn["parameters"]
        declarations.append(decl)

    if not declarations:
        return []
    return [{"function_declarations": declarations}]


def _gemini_args_to_json(raw_args: Any) -> str:
    """Sérialise les arguments Gemini en chaîne JSON.

    Le SDK `google.genai` 1.0+ retourne typiquement un `dict` Python
    natif pour `function_call.args`. Mais certaines versions peuvent
    retourner un `proto.Message` (Struct protobuf). On gère les deux
    cas avec un fallback `str(...)` — au pire l'orchestrateur lèvera
    `TOOL_ARGS_INVALID` à l'exécution, ce qui est récupérable.
    """
    if raw_args is None:
        return "{}"
    if isinstance(raw_args, dict):
        try:
            return json.dumps(raw_args, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return "{}"
    # Tentative MessageToDict pour les protobuf Struct.
    try:
        from google.protobuf.json_format import MessageToDict  # type: ignore

        as_dict = MessageToDict(raw_args)
        return json.dumps(as_dict, ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        pass
    # Dernier recours : itération sur les attributs.
    try:
        return json.dumps(dict(raw_args), ensure_ascii=False, default=str)
    except Exception:  # noqa: BLE001
        return "{}"
