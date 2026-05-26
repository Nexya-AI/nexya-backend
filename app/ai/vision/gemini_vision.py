"""
GeminiVisionProvider — impl réelle via `google.genai` SDK (Vertex AI).

Réutilise le pattern client singleton lazy de `app/ai/providers/gemini.py`
(B1 chat) mais avec un nouveau module pour isoler le code vision (SDK
multimodal usage : `types.Part.from_bytes(data=..., mime_type=...)`).

Tiers supportés :
- `flash` → `gemini-2.0-flash` ($0.075/1M in + $0.30/1M out)
- `pro`   → `gemini-2.0-pro`   ($1.25/1M in  + $5.00/1M out)

Prix tracé par row via `cost_usd` — permet le benchmark a posteriori
`SUM(cost_usd) GROUP BY model` contre un futur `PixtralVisionProvider`
ou `QwenVLVisionProvider`.
"""

from __future__ import annotations

from typing import Final

import structlog

from app.ai.vision.base import (
    ImageInput,
    VisionAuthError,
    VisionContentFilteredError,
    VisionInvalidRequestError,
    VisionProvider,
    VisionRateLimitError,
    VisionResult,
    VisionTier,
    VisionUnavailableError,
)

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Grille de prix Gemini 2.0 (2026-04-24) — $/1M tokens
# ══════════════════════════════════════════════════════════════

_GEMINI_PRICES: Final[dict[str, tuple[float, float]]] = {
    # model → (input_price_per_1M, output_price_per_1M)
    "gemini-2.0-flash": (0.075, 0.30),
    "gemini-2.0-pro": (1.25, 5.00),
    # Fallback 1.5 au cas où (compat historique)
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
}


# ══════════════════════════════════════════════════════════════
# Client singleton lazy — identique pattern B1
# ══════════════════════════════════════════════════════════════

_client: object | None = None


def _get_client():
    """Retourne un `google.genai.Client` singleton process-wide.

    Bug v1.0.4 fix (2026-05-26) : respecte `settings.gemini_use_vertex`.
    Avant ce fix, `vertexai=True` était hardcodé et causait un
    `DefaultCredentialsError` en prod sans ADC GCP, menant à un hang ~70s
    sur `/vision/analyze` (mêmes symptômes que `/chat/stream`, retry chain
    qui expire silencieusement). Pattern aligné `embeddings/gemini_embeddings.py`
    qui faisait déjà le branching correct.
    """
    global _client
    if _client is not None:
        return _client

    from app.config import settings  # noqa: PLC0415

    if not settings.gemini_api_key and not settings.gcp_project_id:
        raise VisionAuthError(
            "GEMINI_API_KEY ou GCP_PROJECT_ID absents",
            provider="gemini",
        )

    from google import genai  # noqa: PLC0415

    if settings.gemini_use_vertex:
        _client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.gcp_location,
        )
        log.info(
            "vision.gemini.client_initialized",
            mode="vertex",
            project=settings.gcp_project_id,
            location=settings.gcp_location,
        )
    else:
        _client = genai.Client(api_key=settings.gemini_api_key)
        log.info(
            "vision.gemini.client_initialized",
            mode="api_key",
            api_key_prefix=settings.gemini_api_key[:8] if settings.gemini_api_key else "<empty>",
        )
    return _client


def _reset_client_for_tests() -> None:
    """Reset du singleton — usage tests uniquement."""
    global _client
    _client = None


# ══════════════════════════════════════════════════════════════
# Mapping erreurs SDK → VisionError typée
# ══════════════════════════════════════════════════════════════


def _map_sdk_exception(exc: Exception, *, model: str) -> Exception:
    """Traduit une exception du SDK `google-genai` en `VisionError`.

    Pattern miroir `app/ai/providers/gemini.py::_map_sdk_exception` (B1 chat).
    """
    status_code = getattr(exc, "code", None) or getattr(exc, "status_code", None)
    message = str(exc) or exc.__class__.__name__

    if status_code in (401, 403):
        return VisionAuthError(message, provider="gemini")
    if status_code == 429:
        return VisionRateLimitError(message, provider="gemini")
    if status_code == 400:
        low = message.lower()
        if "safety" in low or "blocked" in low or "policy" in low:
            return VisionContentFilteredError(message, provider="gemini")
        return VisionInvalidRequestError(message, provider="gemini")
    # 5xx, timeout, connection → retryable
    return VisionUnavailableError(message, provider="gemini")


# ══════════════════════════════════════════════════════════════
# GeminiVisionProvider
# ══════════════════════════════════════════════════════════════


class GeminiVisionProvider(VisionProvider):
    """Impl réelle via `google-genai` SDK pour Gemini 2.0 Flash + Pro."""

    name: Final[str] = "gemini"
    supports_tiers: Final[set[VisionTier]] = {"flash", "pro"}

    def __init__(
        self,
        *,
        flash_model: str = "gemini-2.0-flash",
        pro_model: str = "gemini-2.0-pro",
    ) -> None:
        self._flash_model = flash_model
        self._pro_model = pro_model

    def _resolve_model(self, tier: VisionTier) -> str:
        return self._pro_model if tier == "pro" else self._flash_model

    async def analyze_images(
        self,
        images: list[ImageInput],
        prompt: str,
        *,
        tier: VisionTier = "flash",
        system_prompt: str | None = None,
        max_output_tokens: int = 1024,
    ) -> VisionResult:
        if not images:
            raise VisionInvalidRequestError("Au moins une image est requise", provider="gemini")

        model = self._resolve_model(tier)
        client = _get_client()

        from google.genai import types  # noqa: PLC0415

        # Construit les Parts : texte système (optionnel) + images + prompt.
        parts: list = []
        if system_prompt:
            parts.append(types.Part.from_text(text=system_prompt + "\n\n"))
        for img in images:
            parts.append(types.Part.from_bytes(data=img.data, mime_type=img.mime_type))
        parts.append(types.Part.from_text(text=prompt))

        contents = [types.Content(role="user", parts=parts)]

        config = types.GenerateContentConfig(
            max_output_tokens=max_output_tokens,
            temperature=0.2,  # analyse factuelle, pas créative
        )

        try:
            response = await client.aio.models.generate_content(
                model=model,
                contents=contents,
                config=config,
            )
        except Exception as exc:  # noqa: BLE001
            raise _map_sdk_exception(exc, model=model) from exc

        # Extraction text + usage (selon shape SDK).
        text = getattr(response, "text", None) or ""
        usage = getattr(response, "usage_metadata", None)
        tokens_input = int(getattr(usage, "prompt_token_count", 0) or 0)
        tokens_output = int(getattr(usage, "candidates_token_count", 0) or 0)

        # Calcul coût selon la grille.
        input_price, output_price = _GEMINI_PRICES.get(model, (0.0, 0.0))
        cost_usd = round(
            tokens_input * input_price / 1_000_000 + tokens_output * output_price / 1_000_000,
            6,
        )

        log.info(
            "vision.gemini.analyze_ok",
            model=model,
            tier=tier,
            n_images=len(images),
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            cost_usd=cost_usd,
        )

        return VisionResult(
            text=text,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            model=model,
            provider=self.name,
            cost_usd=cost_usd,
        )
