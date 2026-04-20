"""
NEXYA Couche IA — Contrat abstrait des providers.

Un "provider" = un adaptateur vers un fournisseur d'IA (Google, OpenAI, Anthropic…).
Tous les providers exposent la même interface ; la Couche IA ne connaît jamais
le SDK sous-jacent. Ajouter un nouveau fournisseur = écrire une sous-classe
de `ChatProvider` ou `ImageProvider`, rien d'autre à toucher.

Ce fichier contient :
- Les types de données partagés (`ChatMessage`, `ChatChunk`, `ChatUsage`,
  `ImageGenerationRequest`, `GeneratedImage`).
- Les erreurs typées (`ProviderError` et ses variantes).
- Les interfaces abstraites (`ChatProvider`, `ImageProvider`).

Ce fichier ne dépend d'aucun SDK externe : on pourrait tester la Couche IA
sans jamais toucher à Google ou OpenAI.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Literal


# ═══════════════════════════════════════════════════════════════════
# TYPES — MESSAGES & CHUNKS
# ═══════════════════════════════════════════════════════════════════

ChatRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True, slots=True)
class ChatMessage:
    """Un message dans une conversation. Format neutre, partagé par tous les providers."""

    role: ChatRole
    content: str


@dataclass(frozen=True, slots=True)
class ChatUsage:
    """Consommation de tokens pour un appel — utilisée par `CostTracker`."""

    prompt_tokens: int
    completion_tokens: int
    total_tokens: int


class FinishReason(StrEnum):
    """Raison pour laquelle le provider a arrêté de générer."""

    STOP = "stop"                 # fin naturelle (le modèle a choisi de s'arrêter)
    LENGTH = "length"             # max_tokens atteint
    CONTENT_FILTER = "content_filter"  # le filtre de sécurité a coupé la sortie
    ERROR = "error"               # erreur technique côté provider
    CANCELLED = "cancelled"       # l'utilisateur a annulé le stream


@dataclass(frozen=True, slots=True)
class ChatChunk:
    """Fragment de réponse en streaming.

    - `delta` : le morceau de texte à concaténer côté client
    - `finish_reason` : non-nul uniquement sur le dernier chunk
    - `usage` : renseigné uniquement sur le dernier chunk si le provider le donne
    """

    delta: str = ""
    finish_reason: FinishReason | None = None
    usage: ChatUsage | None = None


# ═══════════════════════════════════════════════════════════════════
# TYPES — REQUÊTES
# ═══════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class ChatCompletionRequest:
    """Requête normalisée de chat, indépendante du provider.

    Le `LlmRouter` construit cette requête à partir du contexte (expert, user,
    historique) et la passe au provider sélectionné. Le provider traduit ces
    champs vers son SDK natif.
    """

    messages: Sequence[ChatMessage]
    system_prompt: str | None = None
    model: str | None = None          # Nom du modèle spécifique (ex: "gemini-2.5-flash")
    temperature: float = 0.7
    max_tokens: int | None = None
    stop_sequences: list[str] = field(default_factory=list)

    # Métadonnées d'observabilité (non envoyées au LLM — utilisées pour les logs)
    user_id: str | None = None
    trace_id: str | None = None
    expert_id: str | None = None

    # Extensions libres pour des features spécifiques d'un provider
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ImageGenerationRequest:
    """Requête normalisée de génération d'images."""

    prompt: str
    count: int = 1                    # 1 à 4 images par appel
    aspect_ratio: str = "1:1"         # "1:1", "16:9", "9:16", "4:3", "3:4"
    negative_prompt: str | None = None

    user_id: str | None = None
    trace_id: str | None = None
    expert_id: str | None = None

    extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class GeneratedImage:
    """Une image générée par un provider. Base64 pour transit JSON."""

    base64_data: str
    mime_type: str = "image/jpeg"


# ═══════════════════════════════════════════════════════════════════
# ERREURS — HIÉRARCHIE TYPÉE
# ═══════════════════════════════════════════════════════════════════


class ProviderError(Exception):
    """Erreur de base remontée par un provider. Jamais levée directement —
    toujours utiliser une sous-classe pour que le router puisse réagir.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str | None = None,
        retryable: bool = False,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.provider = provider
        self.model = model
        self.retryable = retryable
        self.status_code = status_code

    def __repr__(self) -> str:
        return (
            f"{self.__class__.__name__}("
            f"provider={self.provider!r}, model={self.model!r}, "
            f"retryable={self.retryable}, status_code={self.status_code}, "
            f"message={self.message!r})"
        )


class ProviderUnavailableError(ProviderError):
    """Le provider est injoignable ou répond en 5xx.

    Par défaut `retryable=True` : le circuit breaker et le retry layer
    peuvent réessayer ou basculer sur un fallback.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(
            message,
            provider=provider,
            model=model,
            retryable=True,
            status_code=status_code,
        )


class ProviderRateLimitError(ProviderError):
    """Quota atteint côté provider (429). Backoff obligatoire avant retry."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str | None = None,
        retry_after_seconds: float | None = None,
    ) -> None:
        super().__init__(
            message,
            provider=provider,
            model=model,
            retryable=True,
            status_code=429,
        )
        self.retry_after_seconds = retry_after_seconds


class ProviderAuthError(ProviderError):
    """Clé API invalide ou révoquée (401/403). Jamais retryable — il faut
    corriger la configuration."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str | None = None,
    ) -> None:
        super().__init__(
            message,
            provider=provider,
            model=model,
            retryable=False,
            status_code=401,
        )


class ProviderContentFilteredError(ProviderError):
    """Le provider a refusé de répondre pour raisons de sécurité / modération.
    Jamais retryable — c'est un signal à remonter à l'utilisateur."""

    def __init__(
        self,
        message: str = "La réponse a été bloquée par le filtre de sécurité.",
        *,
        provider: str,
        model: str | None = None,
    ) -> None:
        super().__init__(
            message,
            provider=provider,
            model=model,
            retryable=False,
            status_code=400,
        )


class ProviderInvalidRequestError(ProviderError):
    """Requête mal formée (400). Jamais retryable — bug côté NEXYA."""

    def __init__(
        self,
        message: str,
        *,
        provider: str,
        model: str | None = None,
    ) -> None:
        super().__init__(
            message,
            provider=provider,
            model=model,
            retryable=False,
            status_code=400,
        )


# ═══════════════════════════════════════════════════════════════════
# CAPABILITÉS — DÉCLARATION DES FONCTIONNALITÉS
# ═══════════════════════════════════════════════════════════════════


class ProviderCapability(StrEnum):
    """Capacités qu'un provider peut supporter. Déclarées pour que le
    LlmRouter puisse filtrer les candidats éligibles à une requête.
    """

    TEXT_CHAT = "text_chat"
    IMAGE_GENERATION = "image_generation"
    VISION = "vision"                 # input = image + text
    AUDIO_TRANSCRIPTION = "audio_transcription"
    TEXT_TO_SPEECH = "text_to_speech"
    FUNCTION_CALLING = "function_calling"
    JSON_MODE = "json_mode"


# ═══════════════════════════════════════════════════════════════════
# INTERFACE — ChatProvider
# ═══════════════════════════════════════════════════════════════════


class ChatProvider(ABC):
    """Contrat qu'un provider de chat texte doit remplir.

    Sous-classe obligatoire pour :
    - `name` : identifiant stable (ex: "gemini", "openai")
    - `default_model` : modèle utilisé si `ChatCompletionRequest.model` est None
    - `supported_models` : liste des modèles que ce provider sait router
    - `stream_chat()` : génère un flux de `ChatChunk`

    Optionnel :
    - `health_check()` : permet au circuit breaker de valider la disponibilité
    """

    # Identité — à override dans chaque sous-classe
    name: str = ""
    default_model: str = ""
    supported_models: frozenset[str] = frozenset()
    capabilities: frozenset[ProviderCapability] = frozenset({ProviderCapability.TEXT_CHAT})

    # Fenêtre de contexte (en tokens). Utilisé par le ContextBuilder pour tronquer
    # l'historique avant envoi.
    max_context_tokens: int = 8_192

    @abstractmethod
    async def stream_chat(
        self, request: ChatCompletionRequest
    ) -> AsyncIterator[ChatChunk]:
        """Génère la réponse en streaming.

        Contrat :
        - Yield au moins un `ChatChunk` — jamais rien.
        - Le dernier chunk porte `finish_reason` non-nul.
        - Tout appel KO doit lever une `ProviderError` — jamais yield d'erreur.
        - Si l'appelant annule (task.cancel()), propager `asyncio.CancelledError`.
        """
        raise NotImplementedError  # pragma: no cover
        yield  # pragma: no cover  (fait de cette fonction un async generator)

    async def health_check(self) -> bool:
        """Vérifie que le provider répond. Utilisé par le circuit breaker.

        L'implémentation par défaut retourne True : on suppose disponible tant
        qu'aucun appel n'a échoué. Les providers qui exposent un endpoint `/ping`
        ou `/health` peuvent override pour un check plus fin.
        """
        return True

    def supports_model(self, model: str) -> bool:
        return model in self.supported_models

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r}, default_model={self.default_model!r})"


# ═══════════════════════════════════════════════════════════════════
# INTERFACE — ImageProvider
# ═══════════════════════════════════════════════════════════════════


class ImageProvider(ABC):
    """Contrat qu'un provider de génération d'images doit remplir."""

    name: str = ""
    default_model: str = ""
    supported_models: frozenset[str] = frozenset()
    max_images_per_call: int = 4

    @abstractmethod
    async def generate_images(
        self, request: ImageGenerationRequest
    ) -> list[GeneratedImage]:
        """Génère `request.count` images. Peut retourner moins que demandé si
        certaines ont été filtrées par la modération — c'est au service appelant
        de signaler ce cas à l'utilisateur."""
        raise NotImplementedError

    async def health_check(self) -> bool:
        return True

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r}, default_model={self.default_model!r})"
