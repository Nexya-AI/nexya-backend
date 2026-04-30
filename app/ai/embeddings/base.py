"""
NEXYA Couche IA — Embeddings — Contrat abstrait.

Un `EmbeddingsProvider` = adaptateur vers un fournisseur d'embeddings
(OpenAI, Anthropic, Qwen, bge-m3 local, etc.). Tous les providers exposent
la même interface ; la logique métier (MemoryStore, RAG) ne connaît
jamais le SDK sous-jacent. Ajouter un nouveau fournisseur = écrire une
sous-classe de `EmbeddingsProvider`, rien d'autre à toucher.

Hiérarchie d'erreurs alignée sur `app/ai/providers/base.py` (ChatProvider)
pour que le caller puisse uniformiser son handling HTTP (401/429/503/400).

Discipline :
- Aucune dépendance SDK externe ici. On peut tester la Couche Embeddings
  sans jamais toucher OpenAI ou un modèle local.
- `embed(texts)` est **batch natif** : le SDK OpenAI accepte nativement
  une liste d'inputs en un seul appel, 1 facturation pour N vecteurs.
  Le contrat force le batch pour qu'on ne soit pas tenté de faire N
  appels séquentiels coûteux.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass

# ══════════════════════════════════════════════════════════════
# TYPES — Vecteurs + réponses
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class EmbeddingVector:
    """Un vecteur d'embedding retourné par un provider.

    - `values` : liste de floats de longueur `dim`.
    - `dim` : dimension vectorielle (1536 pour `text-embedding-3-small`).
    - `model` : nom du modèle qui a produit le vecteur (traçabilité DB).

    On garde `values` en `list[float]` plutôt qu'un `numpy.ndarray` : le
    service convertit en liste Python avant persistance en DB, et
    SQLAlchemy + pgvector acceptent nativement `list[float]`. Pas de
    dépendance numpy dans ce contrat.
    """

    values: list[float]
    dim: int
    model: str


@dataclass(frozen=True, slots=True)
class EmbeddingsUsage:
    """Facturation retournée par l'API provider.

    `total_tokens` est facturé par OpenAI (pas le nombre de vecteurs).
    `prompt_tokens` est toujours égal à `total_tokens` pour les
    embeddings (pas de completion tokens), on le garde pour la symétrie
    avec `ChatUsage` et éviter des edge cases dans `CostTracker`.
    """

    prompt_tokens: int
    total_tokens: int


@dataclass(frozen=True, slots=True)
class EmbeddingsResponse:
    """Réponse complète d'un appel `embed(texts)`.

    `vectors` est garanti **dans le même ordre** que les textes passés
    en entrée. Si le caller a passé 3 textes, il reçoit 3 vecteurs dans
    le même ordre. C'est un contrat strict — aucun provider n'a le
    droit de réordonner.
    """

    vectors: list[EmbeddingVector]
    usage: EmbeddingsUsage


# ══════════════════════════════════════════════════════════════
# ERREURS TYPÉES — miroir de `providers/base.py`
# ══════════════════════════════════════════════════════════════


class EmbeddingsError(Exception):
    """Erreur générique d'un provider embeddings.

    Porte le nom du provider en `provider` pour que les logs + metrics
    puissent segmenter par fournisseur sans parser le message.
    """

    def __init__(self, message: str, *, provider: str = "unknown") -> None:
        super().__init__(message)
        self.provider = provider


class EmbeddingsAuthError(EmbeddingsError):
    """Clé API invalide ou absente."""


class EmbeddingsRateLimitError(EmbeddingsError):
    """Limite d'appels (RPS / RPM) côté provider dépassée.

    `retry_after` : secondes avant retry suggéré par le provider (Header
    `Retry-After`). None si non fourni — le caller applique son propre
    backoff par défaut.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str = "unknown",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.retry_after = retry_after


class EmbeddingsUnavailableError(EmbeddingsError):
    """Provider injoignable (réseau, 5xx, timeout)."""


class EmbeddingsInvalidRequestError(EmbeddingsError):
    """Requête mal formée côté provider (texte trop long, modèle inconnu,
    quota dépassé côté compte facturé).

    Distinct de `EmbeddingsAuthError` et `EmbeddingsRateLimitError` pour
    permettre au caller de décider : retry avec backoff (rate limit),
    refus client 4xx (invalid), refus 503 (unavailable), refus 401/403
    (auth).
    """


# ══════════════════════════════════════════════════════════════
# INTERFACE ABSTRAITE
# ══════════════════════════════════════════════════════════════


class EmbeddingsProvider(ABC):
    """Contrat minimal pour un fournisseur d'embeddings.

    Chaque impl expose :
    - `name` : identifiant unique ("openai", "mock", "bge-m3", ...).
    - `default_model` : modèle utilisé par défaut si `model=None`.
    - `dim` : dimension vectorielle (figée par modèle).

    Méthode `embed(texts, *, model=None) -> EmbeddingsResponse` :
    - `texts` : liste non-vide de textes à encoder.
    - `model` : override du modèle (optionnel).
    - Lève une `EmbeddingsError` typée en cas de problème.
    - Retourne les vecteurs **dans le même ordre** que les textes.
    """

    name: str
    default_model: str
    dim: int

    @abstractmethod
    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        task_type: str | None = None,
    ) -> EmbeddingsResponse:
        """Encode une liste de textes en vecteurs.

        `task_type` (G1) : hint provider-specific sur l'usage downstream du
        vecteur. Google Gemini exploite `RETRIEVAL_DOCUMENT` (ingestion
        corpus) vs `RETRIEVAL_QUERY` (embed d'une question user) pour
        produire des projections vectorielles asymétriques optimisées pour
        la recherche sémantique — gain qualité retrieval non négligeable.
        OpenAI et Mock ignorent le paramètre silencieusement (rétro-compat).

        Implémentations :
        - Gemini : `task_type` consommé par `client.models.embed_content`.
        - OpenAI : 1 appel HTTP batch (API accepte `input: str | list[str]`).
        - Mock : calcul local déterministe, pas de réseau.
        """
