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
    OpenAIChatProvider,
    QwenChatProvider,
)

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
            raise RouterError(
                "LlmRouter requiert au moins un ChatProvider enregistré."
            )
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
        return [
            ChatResolution(provider=p, model=m, config=config) for p, m in chain
        ]

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

    def _build_chain(
        self, config: ExpertConfig
    ) -> list[tuple[ChatProvider, str]]:
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

    Fonctionnement :
    - `GeminiChatProvider` et `GeminiImageProvider` : implémentations réelles.
    - `OpenAIChatProvider`, `AnthropicChatProvider`, `QwenChatProvider` :
      stubs qui lèvent `ProviderUnavailableError` à l'appel. Ils sont
      enregistrés dès maintenant pour que les chaînes de fallback qui les
      mentionnent ne soient pas filtrées silencieusement par le router.

    Quand une clé API devient disponible, il suffit de remplacer le stub
    par l'implémentation réelle dans cette factory — aucun autre fichier
    n'est impacté.
    """
    chat_providers: dict[str, ChatProvider] = {
        "gemini": GeminiChatProvider(),
        "openai": OpenAIChatProvider(),
        "anthropic": AnthropicChatProvider(),
        "qwen": QwenChatProvider(),
    }
    image_providers: dict[str, ImageProvider] = {
        "gemini-imagen": GeminiImageProvider(),
    }
    return LlmRouter(
        chat_providers=chat_providers,
        image_providers=image_providers,
    )
