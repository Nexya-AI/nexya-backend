"""AiModelsService — aggregation runtime de l'inventaire modèles IA.

Session N1 — 2026-04-27.

Source de vérité = providers initialisés via `get_ai_router()`. Pas de
table DB séparée. Itère sur `chat_providers` + `image_providers` et
expose les `supported_models` + métadonnées.

Les providers `voice` (E1) et `vision` (E2) ne sont **pas** inclus dans
V1 — ils ne mappent pas vers `EXPERT_REGISTRY` (ce sont des features
séparées). V2 si Ivan veut un inventaire vraiment exhaustif.

Filtrage Mock : en mode prod (`settings.is_production`), les providers
Mock-usurpants sont exclus de la sortie. En dev, ils restent visibles
pour debug avec `is_available=False`.
"""

from __future__ import annotations

from typing import cast

import structlog

from app.ai.experts import EXPERT_REGISTRY
from app.ai.providers import ChatProvider, ImageProvider, ProviderCapability
from app.ai.providers.mock import MockChatProvider
from app.ai.runtime import get_ai_router
from app.config import settings
from app.features.ai_models.schemas import (
    ModelCapability,
    ModelInfo,
    ModelsListResponse,
    ModelTier,
)

log = structlog.get_logger(__name__)


# ── Mapping display names — sinon fallback `model_id.title()` ─────
_MODEL_DISPLAY_NAMES: dict[str, str] = {
    # Gemini
    "gemini-2.5-flash": "Gemini 2.5 Flash",
    "gemini-2.5-pro": "Gemini 2.5 Pro",
    "gemini-2.0-flash": "Gemini 2.0 Flash",
    "gemini-2.0-pro": "Gemini 2.0 Pro",
    # OpenAI chat
    "gpt-4o": "GPT-4o",
    "gpt-4o-mini": "GPT-4o Mini",
    "gpt-4-turbo": "GPT-4 Turbo",
    "o1": "OpenAI o1",
    "o1-mini": "OpenAI o1 Mini",
    # Anthropic Claude
    "claude-opus-4-6": "Claude Opus 4.6",
    "claude-sonnet-4-6": "Claude Sonnet 4.6",
    "claude-haiku-4-5": "Claude Haiku 4.5",
    # Qwen
    "qwen2.5-72b-instruct": "Qwen 2.5 72B",
    "qwen2.5-32b-instruct": "Qwen 2.5 32B",
    "qwen2.5-14b-instruct": "Qwen 2.5 14B",
    "qwen2.5-7b-instruct": "Qwen 2.5 7B",
    "qwen-max": "Qwen Max",
    # OpenRouter
    "anthropic/claude-3.5-sonnet": "Claude 3.5 Sonnet (OpenRouter)",
    "meta-llama/llama-3.1-70b-instruct": "Llama 3.1 70B (OpenRouter)",
    "mistralai/mistral-large": "Mistral Large (OpenRouter)",
    "deepseek/deepseek-chat": "DeepSeek Chat (OpenRouter)",
    "qwen/qwen-2.5-72b-instruct": "Qwen 2.5 72B (OpenRouter)",
    # Image
    "imagen-3.0-generate-002": "Imagen 3.0",
    # Mock
    "mock-default": "Mock (dev)",
}


def _display_name_for(model_id: str) -> str:
    """Lookup dans le mapping ; fallback `model_id.replace("-"," ").title()`."""
    if model_id in _MODEL_DISPLAY_NAMES:
        return _MODEL_DISPLAY_NAMES[model_id]
    return model_id.replace("-", " ").replace("/", " / ").title()


def _tier_for(max_context_tokens: int) -> ModelTier:
    """Catégorise un modèle selon sa fenêtre de contexte.

    - `< 32 768`  → flash (modèles légers/rapides)
    - `< 1 000 000` → pro (modèles standards production)
    - `>= 1 000 000` → ultra (Gemini 1M, Opus 4.7 [1M])
    """
    if max_context_tokens < 32_768:
        return "flash"
    if max_context_tokens < 1_000_000:
        return "pro"
    return "ultra"


def _capabilities_to_strings(
    caps: frozenset[ProviderCapability],
) -> list[ModelCapability]:
    """Convertit `frozenset[ProviderCapability]` en `list[str]` ordonnée."""
    # ProviderCapability est un StrEnum — `.value` est la string.
    return cast("list[ModelCapability]", sorted(c.value for c in caps))


def _is_mock(provider: ChatProvider | ImageProvider) -> bool:
    """True si le provider est un Mock-usurpant (clé absente)."""
    return isinstance(provider, MockChatProvider)


class AiModelsService:
    """Aggregation runtime de l'inventaire modèles IA."""

    @staticmethod
    def list_models() -> ModelsListResponse:
        """Construit la liste complète des modèles disponibles.

        Pas d'argument `db` — l'aggregation se fait en mémoire à partir
        des providers initialisés au lifespan.
        """
        router = get_ai_router()
        chat_providers: dict[str, ChatProvider] = router._chat  # noqa: SLF001
        image_providers: dict[str, ImageProvider] = router._image  # noqa: SLF001

        models: list[ModelInfo] = []
        is_prod = settings.is_production

        # ── Chat providers ──────────────────────────────────────
        for provider_name, provider in chat_providers.items():
            is_mock = _is_mock(provider)
            # En prod, on cache les Mock pour ne pas exposer l'état
            # provider à un attaquant.
            if is_prod and is_mock:
                continue
            for model_id in sorted(provider.supported_models):
                models.append(
                    ModelInfo(
                        provider=provider_name,
                        model_id=model_id,
                        display_name=_display_name_for(model_id),
                        tier=_tier_for(provider.max_context_tokens),
                        capabilities=_capabilities_to_strings(provider.capabilities),
                        max_context_tokens=provider.max_context_tokens,
                        is_default_for=[
                            eid
                            for eid, cfg in EXPERT_REGISTRY.items()
                            if cfg.primary_provider == provider_name
                            and cfg.primary_model == model_id
                        ],
                        is_available=not is_mock,
                    )
                )

        # ── Image providers ─────────────────────────────────────
        for provider_name, provider in image_providers.items():
            is_mock = isinstance(provider, MockChatProvider)  # cohérence
            if is_prod and is_mock:
                continue
            for model_id in sorted(provider.supported_models):
                # Les image providers ne sont jamais le `primary_model`
                # d'un expert chat — `is_default_for` reste vide.
                # En revanche, ils sont utilisés via `resolve_image()`
                # quand un expert demande de la génération d'image.
                models.append(
                    ModelInfo(
                        provider=provider_name,
                        model_id=model_id,
                        display_name=_display_name_for(model_id),
                        tier="pro",  # Imagen 3.0 = qualité prod
                        capabilities=[ProviderCapability.IMAGE_GENERATION.value],
                        max_context_tokens=getattr(provider, "max_context_tokens", 0),
                        is_default_for=[],
                        is_available=not is_mock,
                    )
                )

        # ── Experts routing : `expert_id → primary_model_id` ────
        experts_routing = {eid: cfg.primary_model for eid, cfg in EXPERT_REGISTRY.items()}

        log.debug(
            "ai_models.list",
            count=len(models),
            chat_providers=len(chat_providers),
            image_providers=len(image_providers),
            is_prod=is_prod,
        )
        return ModelsListResponse(models=models, experts_routing=experts_routing)
