"""
NEXYA Couche IA — LlmRouter.

Le `LlmRouter` est le point d'entrée unique de la Couche IA. Il traduit
`expert_id` (ex: "computer", "science", "medicine") en un triplet concret :

    (ChatProvider instance, model_name, ExpertConfig)

C'est lui qui décide du modèle — **jamais** le frontend (règle d'or NEXYA).
Le frontend envoie un `expert_id` ; le backend choisit :

- Le provider primaire et son modèle (Gemini Flash / Pro, OpenAI, etc.)
- La chaîne de fallback ordonnée (pour bascule en cas de 5xx / rate limit)
- Le `system_prompt`, la `temperature`, les disclaimers — via `ExpertConfig`

Le router ne fait PAS de retry ni de circuit breaking : il se contente de
résoudre. Les briques 5 (retry + circuit breaker) consommeront
`build_chain()` pour essayer les fallbacks un par un.

Entrées du router (injectées au constructeur) :
- `chat_providers` : dict {name → ChatProvider}
- `image_providers` : dict {name → ImageProvider}

La factory `build_default_router()` instancie les 4 providers chat
(Gemini réel, OpenAI/Anthropic/Qwen stubs) + Gemini Imagen. À partir du
moment où un provider a une implémentation réelle, il suffit de l'ajouter
à la factory — aucun autre fichier ne change.
"""

from __future__ import annotations

from dataclasses import dataclass

import structlog

from app.ai.experts import EXPERT_REGISTRY, ExpertConfig, get_expert_config
from app.ai.providers import (
    AnthropicChatProvider,
    ChatProvider,
    GeminiChatProvider,
    GeminiImageProvider,
    ImageProvider,
    MockChatProvider,
    OpenAIChatProvider,
    OpenRouterChatProvider,
    QwenChatProvider,
)
from app.config import settings

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# TYPES DE RETOUR
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ChatResolution:
    """Résultat de résolution chat : tout ce qu'il faut pour lancer un appel.

    `config` expose `system_prompt`, `temperature`, `disclaimer`, etc. —
    le caller (QueryEngine) n'a pas à re-lire le registre.
    """

    provider: ChatProvider
    model: str
    config: ExpertConfig


@dataclass(frozen=True, slots=True)
class ImageResolution:
    """Résultat de résolution image (mode Studio uniquement pour l'instant)."""

    provider: ImageProvider
    model: str
    config: ExpertConfig


# ═══════════════════════════════════════════════════════════════════
# EXCEPTIONS DU ROUTER
# ═══════════════════════════════════════════════════════════════════


class RouterError(Exception):
    """Erreur de configuration du router. Levée au démarrage ou à la
    résolution quand AUCUN candidat viable n'existe dans la chaîne."""


# ═══════════════════════════════════════════════════════════════════
# LlmRouter
# ═══════════════════════════════════════════════════════════════════


class LlmRouter:
    """Résout `expert_id` → provider + modèle + config.

    Immuable après construction : les dicts injectés sont copiés pour que
    le caller ne puisse pas muter la table de routage en cours de route.
    """

    def __init__(
        self,
        *,
        chat_providers: dict[str, ChatProvider],
        image_providers: dict[str, ImageProvider] | None = None,
    ) -> None:
        if not chat_providers:
            raise RouterError("LlmRouter requiert au moins un ChatProvider enregistré.")
        self._chat: dict[str, ChatProvider] = dict(chat_providers)
        self._image: dict[str, ImageProvider] = dict(image_providers or {})

        log.info(
            "ai.router.initialized",
            chat_providers=sorted(self._chat.keys()),
            image_providers=sorted(self._image.keys()),
            experts=len(EXPERT_REGISTRY),
        )

    # ─── Chat ────────────────────────────────────────────────────────

    def resolve(self, expert_id: str | None) -> ChatResolution:
        """Renvoie la résolution PRIMAIRE pour un expert.

        Résolution "primaire" = première entrée viable de la chaîne
        (`primary_provider/primary_model` → fallbacks). Un candidat est
        viable si :
        - Son `provider_name` est enregistré dans `chat_providers`.
        - Le modèle est dans `provider.supported_models` (sinon log de
          warning — le provider aurait levé `ProviderInvalidRequest` à
          l'appel).

        Lève `RouterError` si AUCUN candidat de la chaîne n'est viable
        (cas de mauvaise configuration serveur — jamais d'un input user).
        """
        config = get_expert_config(expert_id)
        chain = self._build_chain(config)
        if not chain:
            raise RouterError(
                f"Aucun provider viable pour l'expert '{config.expert_id}'. "
                f"Chaîne configurée : {list(config.full_chain)}."
            )
        provider, model = chain[0]
        return ChatResolution(provider=provider, model=model, config=config)

    def build_chain(self, expert_id: str | None) -> list[ChatResolution]:
        """Renvoie la chaîne COMPLÈTE et viable (primaire puis fallbacks).

        Consommé par la brique 5 (retry + circuit breaker) : elle essaie
        le premier, si `ProviderError.retryable` → passe au suivant.

        Garantie : toutes les entrées sont appelables — un provider dont
        le nom n'est pas enregistré est filtré à ce niveau (pas d'erreur
        au moment du `stream_chat`).
        """
        config = get_expert_config(expert_id)
        chain = self._build_chain(config)
        return [ChatResolution(provider=p, model=m, config=config) for p, m in chain]

    # ─── Image ───────────────────────────────────────────────────────

    def resolve_image(self, expert_id: str | None) -> ImageResolution:
        """Résout un expert image (actuellement : uniquement "studio").

        Lève `RouterError` si l'expert n'a pas de provider image ou si
        celui-ci n'est pas enregistré.
        """
        config = get_expert_config(expert_id)
        provider_name = config.primary_provider
        provider = self._image.get(provider_name)
        if provider is None:
            raise RouterError(
                f"Aucun ImageProvider enregistré pour '{provider_name}' "
                f"(expert '{config.expert_id}')."
            )
        return ImageResolution(
            provider=provider,
            model=config.primary_model,
            config=config,
        )

    # ─── Introspection ───────────────────────────────────────────────

    def has_chat_provider(self, name: str) -> bool:
        return name in self._chat

    def has_image_provider(self, name: str) -> bool:
        return name in self._image

    def chat_provider_names(self) -> list[str]:
        return sorted(self._chat.keys())

    def image_provider_names(self) -> list[str]:
        return sorted(self._image.keys())

    # ─── Interne ─────────────────────────────────────────────────────

    def _build_chain(self, config: ExpertConfig) -> list[tuple[ChatProvider, str]]:
        """Itère `config.full_chain` et ne garde que les entrées viables.

        Règles de filtrage :
        - Si le `provider_name` n'est pas enregistré → skip + warning
          (cas d'une chaîne configurée pour un provider pas encore déployé).
        - Si le modèle n'est pas dans `supported_models` → on garde quand
          même mais on loggue un warning. Raison : la liste
          `supported_models` est indicative ; un provider réel peut
          accepter un modèle plus récent que celui inscrit au code.
        """
        result: list[tuple[ChatProvider, str]] = []
        for provider_name, model in config.full_chain:
            provider = self._chat.get(provider_name)
            if provider is None:
                log.warning(
                    "ai.router.skip_unregistered_provider",
                    expert_id=config.expert_id,
                    provider=provider_name,
                    model=model,
                )
                continue
            if model not in provider.supported_models:
                log.warning(
                    "ai.router.model_not_in_supported_set",
                    expert_id=config.expert_id,
                    provider=provider_name,
                    model=model,
                    supported=sorted(provider.supported_models),
                )
            result.append((provider, model))
        return result


# ═══════════════════════════════════════════════════════════════════
# FACTORY — câblage par défaut de tous les providers
# ═══════════════════════════════════════════════════════════════════


def build_default_router() -> LlmRouter:
    """Instancie un `LlmRouter` avec tous les providers NEXYA connus.

    Sélection **mock-first** par clé API :
    - Pour chaque provider (openai / anthropic / qwen / gemini), si la clé
      correspondante est vide dans `settings`, on instancie un
      `MockChatProvider` qui porte le même `name` / `default_model` / liste
      de modèles supportés que le provider réel. Les chaînes de fallback
      définies dans `experts.py` continuent donc de résoudre, et le stream
      SSE remonte un texte factice prévisible au lieu d'un 500.
    - Dès qu'Ivan remplit une clé dans `.env` et redémarre uvicorn, le
      provider réel est câblé automatiquement — aucun autre fichier à
      modifier.
    - `Gemini` : la clé est toujours disponible au 2026-04-22, donc le
      provider réel est systématiquement instancié. Un fallback Mock est
      quand même cascadé si jamais `gemini_api_key` devient vide (prod
      Safety Net).
    """
    real = _build_real_chat_providers()
    mocks = _build_mock_chat_providers()

    chat_providers: dict[str, ChatProvider] = {}
    for name in ("gemini", "openai", "anthropic", "qwen", "openrouter"):
        chat_providers[name] = real.get(name) or mocks[name]

    image_providers: dict[str, ImageProvider] = {}
    if settings.gemini_api_key:
        image_providers["gemini-imagen"] = GeminiImageProvider()
    else:
        log.warning(
            "ai.router.image_provider_disabled",
            reason="GEMINI_API_KEY vide — Imagen désactivé",
        )

    for name, provider in chat_providers.items():
        log.info(
            "ai.router.chat_provider_selected",
            name=name,
            kind=type(provider).__name__,
            default_model=provider.default_model,
        )

    return LlmRouter(
        chat_providers=chat_providers,
        image_providers=image_providers,
    )


def _build_real_chat_providers() -> dict[str, ChatProvider]:
    """Instancie les providers réels dont la clé est non vide."""
    real: dict[str, ChatProvider] = {}
    if settings.gemini_api_key:
        real["gemini"] = GeminiChatProvider()
    if settings.openai_api_key:
        real["openai"] = OpenAIChatProvider()
    if settings.anthropic_api_key:
        real["anthropic"] = AnthropicChatProvider()
    if settings.qwen_api_key:
        real["qwen"] = QwenChatProvider()
    if settings.openrouter_api_key:
        real["openrouter"] = OpenRouterChatProvider()
    return real


def _build_mock_chat_providers() -> dict[str, ChatProvider]:
    """Fabrique un `MockChatProvider` usurpant l'identité de chaque provider
    pour couvrir les cas de clé absente. Chaque mock accepte exactement
    les mêmes modèles que son provider réel — les chaînes de fallback
    dans `experts.py` passent sans warning `model_not_in_supported_set`.
    """
    return {
        "gemini": MockChatProvider(
            name="gemini",
            default_model=GeminiChatProvider.default_model,
            supported_models=GeminiChatProvider.supported_models,
            max_context_tokens=GeminiChatProvider.max_context_tokens,
        ),
        "openai": MockChatProvider(
            name="openai",
            default_model=OpenAIChatProvider.default_model,
            supported_models=OpenAIChatProvider.supported_models,
            max_context_tokens=OpenAIChatProvider.max_context_tokens,
        ),
        "anthropic": MockChatProvider(
            name="anthropic",
            default_model=AnthropicChatProvider.default_model,
            supported_models=AnthropicChatProvider.supported_models,
            max_context_tokens=AnthropicChatProvider.max_context_tokens,
        ),
        "qwen": MockChatProvider(
            name="qwen",
            default_model=QwenChatProvider.default_model,
            supported_models=QwenChatProvider.supported_models,
            max_context_tokens=QwenChatProvider.max_context_tokens,
        ),
        "openrouter": MockChatProvider(
            name="openrouter",
            default_model=OpenRouterChatProvider.default_model,
            supported_models=OpenRouterChatProvider.supported_models,
            max_context_tokens=OpenRouterChatProvider.max_context_tokens,
        ),
    }
