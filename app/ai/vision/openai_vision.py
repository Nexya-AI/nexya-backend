"""
OpenAIVisionProvider — impl réelle via `openai` SDK (GPT-4o).

GPT-4o utilise le même endpoint `chat.completions.create()` que le chat
texte, mais avec un `content` multimodal : liste de parts `{type: 'text'}`
et `{type: 'image_url', image_url: {url: 'data:image/png;base64,...'}}`.

Prix tracé (2026-04-24) :
- gpt-4o      : $2.50/1M input + $10.00/1M output
- gpt-4o-mini : $0.15/1M input + $0.60/1M output

Supports `pro` uniquement — GPT-4o-mini n'est pas encore exposé dans
cette session (on peut l'ajouter plus tard si on veut un tier flash
OpenAI). `GeminiVisionProvider.flash` suffit pour le tier flash initial.
"""

from __future__ import annotations

import base64
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
# Grille de prix OpenAI (2026-04-24)
# ══════════════════════════════════════════════════════════════

_OPENAI_PRICES: Final[dict[str, tuple[float, float]]] = {
    "gpt-4o": (2.50, 10.00),
    "gpt-4o-mini": (0.15, 0.60),
}


# ══════════════════════════════════════════════════════════════
# Client singleton lazy
# ══════════════════════════════════════════════════════════════

_client: object | None = None


def _get_client():
    global _client
    if _client is not None:
        return _client

    import openai  # noqa: PLC0415

    from app.config import settings  # noqa: PLC0415

    if not settings.openai_api_key:
        raise VisionAuthError("OPENAI_API_KEY absente", provider="openai")
    _client = openai.AsyncOpenAI(
        api_key=settings.openai_api_key,
        timeout=120.0,
        max_retries=0,
    )
    return _client


def _reset_client_for_tests() -> None:
    global _client
    _client = None


# ══════════════════════════════════════════════════════════════
# Mapping erreurs SDK → VisionError typée
# ══════════════════════════════════════════════════════════════


def _map_sdk_exception(exc: Exception, *, model: str) -> Exception:
    """Miroir strict du pattern B1 OpenAIChatProvider."""
    import openai  # noqa: PLC0415

    if isinstance(exc, openai.AuthenticationError):
        return VisionAuthError(str(exc), provider="openai")
    if isinstance(exc, openai.PermissionDeniedError):
        return VisionAuthError(str(exc), provider="openai")
    if isinstance(exc, openai.RateLimitError):
        retry_after_raw = None
        headers = getattr(exc.response, "headers", None) if hasattr(exc, "response") else None
        if headers is not None:
            try:
                retry_after_raw = headers.get("retry-after")
            except Exception:  # noqa: BLE001
                retry_after_raw = None
        retry_after: float | None = None
        if retry_after_raw:
            try:
                retry_after = float(retry_after_raw)
            except (TypeError, ValueError):
                retry_after = None
        return VisionRateLimitError(str(exc), provider="openai", retry_after=retry_after)
    if isinstance(exc, openai.NotFoundError):
        return VisionInvalidRequestError(f"Modèle '{model}' introuvable", provider="openai")
    if isinstance(exc, openai.BadRequestError):
        low = str(exc).lower()
        if "content_filter" in low or "safety" in low:
            return VisionContentFilteredError(str(exc), provider="openai")
        return VisionInvalidRequestError(str(exc), provider="openai")
    if isinstance(exc, (openai.APIConnectionError, openai.APITimeoutError)):
        return VisionUnavailableError(str(exc), provider="openai")
    return VisionUnavailableError(str(exc), provider="openai")


# ══════════════════════════════════════════════════════════════
# OpenAIVisionProvider
# ══════════════════════════════════════════════════════════════


class OpenAIVisionProvider(VisionProvider):
    """Impl GPT-4o via SDK `openai`."""

    name: Final[str] = "openai"
    supports_tiers: Final[set[VisionTier]] = {"pro"}

    def __init__(self, *, default_model: str = "gpt-4o") -> None:
        self._default_model = default_model

    async def analyze_images(
        self,
        images: list[ImageInput],
        prompt: str,
        *,
        tier: VisionTier = "pro",
        system_prompt: str | None = None,
        max_output_tokens: int = 1024,
    ) -> VisionResult:
        if not images:
            raise VisionInvalidRequestError("Au moins une image est requise", provider="openai")
        if tier not in self.supports_tiers:
            raise VisionInvalidRequestError(
                f"Tier '{tier}' non supporté par OpenAI (pro only)",
                provider="openai",
            )

        client = _get_client()
        model = self._default_model

        # Construit le content multimodal : images d'abord, texte après.
        content: list[dict] = []
        for img in images:
            b64 = base64.b64encode(img.data).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{img.mime_type};base64,{b64}",
                    },
                }
            )
        content.append({"type": "text", "text": prompt})

        messages: list[dict] = []
        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})
        messages.append({"role": "user", "content": content})

        try:
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
                max_tokens=max_output_tokens,
                temperature=0.2,
            )
        except Exception as exc:  # noqa: BLE001
            raise _map_sdk_exception(exc, model=model) from exc

        # Extract text + usage.
        choice = response.choices[0] if response.choices else None
        text = ""
        if choice is not None:
            text = getattr(choice.message, "content", None) or ""

        usage = getattr(response, "usage", None)
        tokens_input = int(getattr(usage, "prompt_tokens", 0) or 0)
        tokens_output = int(getattr(usage, "completion_tokens", 0) or 0)

        input_price, output_price = _OPENAI_PRICES.get(model, (0.0, 0.0))
        cost_usd = round(
            tokens_input * input_price / 1_000_000 + tokens_output * output_price / 1_000_000,
            6,
        )

        log.info(
            "vision.openai.analyze_ok",
            model=model,
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
