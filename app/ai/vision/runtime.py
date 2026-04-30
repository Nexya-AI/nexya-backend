"""
Factory `VisionProvider` — mock-first + sélection par tier.

Pattern aligné `app/ai/voice/runtime.py` (E1) + extension tier-aware :
- Tier `flash` → Gemini 2.0 Flash (ou Mock).
- Tier `pro`   → Gemini 2.0 Pro (défaut) OU GPT-4o si
  `settings.vision_pro_provider="openai"` (ou Mock).

Sélection mock-first auto :
- `settings.vision_mock_enabled=True`  → MockVisionProvider (flash + pro)
- `settings.gemini_api_key=""`         → MockVisionProvider auto
- sinon                                → provider réel selon tier

Singletons séparés par tier pour éviter les re-constructions SDK.
"""

from __future__ import annotations

import structlog

from app.ai.vision.base import VisionProvider, VisionTier
from app.ai.vision.gemini_vision import GeminiVisionProvider
from app.ai.vision.mock_vision import MockVisionProvider
from app.ai.vision.openai_vision import OpenAIVisionProvider

log = structlog.get_logger()


_PROVIDERS: dict[VisionTier, VisionProvider] = {}


def get_vision_provider(tier: VisionTier = "flash") -> VisionProvider:
    """Retourne le VisionProvider cachéd pour le tier demandé.

    - `flash` : Gemini 2.0 Flash (cheap) ou Mock.
    - `pro`   : Gemini 2.0 Pro (défaut) OU GPT-4o si config pro_provider.
    """
    if tier in _PROVIDERS:
        return _PROVIDERS[tier]

    from app.config import settings  # noqa: PLC0415 — évite circulaire

    # Mock-first auto.
    if settings.vision_mock_enabled or not settings.gemini_api_key:
        provider: VisionProvider = MockVisionProvider()
        _PROVIDERS[tier] = provider
        log.info(
            "vision.provider.initialized",
            tier=tier,
            name=provider.name,
            reason=("mock_enabled" if settings.vision_mock_enabled else "no_api_key"),
        )
        return provider

    # Tier flash → toujours Gemini Flash.
    if tier == "flash":
        provider = GeminiVisionProvider(
            flash_model=settings.vision_default_flash_model,
            pro_model=settings.vision_default_pro_model,
        )
        _PROVIDERS[tier] = provider
        log.info(
            "vision.provider.initialized",
            tier=tier,
            name=provider.name,
            model=settings.vision_default_flash_model,
        )
        return provider

    # Tier pro → Gemini Pro (défaut) OU OpenAI GPT-4o.
    if tier == "pro":
        if settings.vision_pro_provider == "openai":
            if not settings.openai_api_key:
                # Fallback Gemini Pro si OpenAI demandé mais clé absente.
                log.warning(
                    "vision.provider.openai_key_missing_fallback_gemini_pro",
                )
                provider = GeminiVisionProvider(
                    flash_model=settings.vision_default_flash_model,
                    pro_model=settings.vision_default_pro_model,
                )
            else:
                provider = OpenAIVisionProvider(default_model="gpt-4o")
        else:
            provider = GeminiVisionProvider(
                flash_model=settings.vision_default_flash_model,
                pro_model=settings.vision_default_pro_model,
            )
        _PROVIDERS[tier] = provider
        log.info(
            "vision.provider.initialized",
            tier=tier,
            name=provider.name,
        )
        return provider

    raise ValueError(f"Tier Vision inconnu : {tier!r}")


def reset_vision_provider() -> None:
    """Reset tous les singletons — usage tests uniquement."""
    _PROVIDERS.clear()
