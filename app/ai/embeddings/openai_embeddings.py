"""
OpenAI embeddings provider — `text-embedding-3-small` par défaut.

Utilise le SDK `openai>=1.55` déjà présent (installé en B1 pour
`OpenAIChatProvider`). Le SDK accepte nativement le batch :
`client.embeddings.create(model=..., input=list[str])` retourne N
vecteurs en 1 seul appel → 1 facturation.

Discipline :
- Client lazy (première utilisation) pour éviter de payer le cold-start
  SDK au démarrage du serveur (le moderator/modération tourne déjà).
- `timeout=30.0, max_retries=0` : on s'appuie sur le RetryPolicy externe
  de la Couche IA — jamais deux couches de retry qui se marchent dessus.
- Mapping exceptions SDK → `EmbeddingsError` typées (Auth/RateLimit/
  Unavailable/InvalidRequest) pour que le service ait un handling
  uniforme avec les ChatProvider.

Modèles supportés côté OpenAI (v1 API `/v1/embeddings`) :
- `text-embedding-3-small` — 1536 dim, $0.02/1M tokens, **défaut NEXYA**.
- `text-embedding-3-large` — 3072 dim, $0.13/1M tokens (7× plus cher).
- `text-embedding-ada-002` — 1536 dim, $0.10/1M tokens (legacy, à éviter).

Le choix NEXYA v1 = `3-small`. Qualité excellente (MTEB proche de
`3-large`) pour le français, coût minimal, dimension aligned sur notre
colonne `vector(1536)` SQL figée au DDL.
"""

from __future__ import annotations

from typing import Final

import structlog

from app.ai.embeddings.base import (
    EmbeddingsAuthError,
    EmbeddingsError,
    EmbeddingsInvalidRequestError,
    EmbeddingsProvider,
    EmbeddingsRateLimitError,
    EmbeddingsResponse,
    EmbeddingsUnavailableError,
    EmbeddingsUsage,
    EmbeddingVector,
)
from app.config import settings

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# OpenAIEmbeddingsProvider
# ══════════════════════════════════════════════════════════════


class OpenAIEmbeddingsProvider(EmbeddingsProvider):
    """Embeddings OpenAI via le SDK officiel async.

    Initialisé avec la clé `settings.openai_api_key`. Si vide, lève
    `EmbeddingsAuthError` dès le premier `embed()` — la factory
    `get_embeddings_provider()` doit avoir détecté l'absence de clé
    et retourné le Mock avant même d'instancier cette classe (pattern
    aligné B1 OpenAIChatProvider).

    `supported_models` listé pour traçabilité + validation côté service
    (on rejette un override `model=` vers un modèle non supporté — le
    SDK OpenAI retournerait un 400, autant filtrer côté backend).
    """

    name: Final[str] = "openai"
    default_model: Final[str] = "text-embedding-3-small"
    dim: Final[int] = 1536

    supported_models: Final[dict[str, int]] = {
        "text-embedding-3-small": 1536,
        "text-embedding-3-large": 3072,
        "text-embedding-ada-002": 1536,
    }

    def __init__(
        self,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
        default_model: str | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else settings.openai_api_key
        self._base_url = base_url  # None = défaut OpenAI api.openai.com/v1
        self._timeout = timeout
        if default_model:
            # Permet à un test ou une config prod de forcer `3-large`.
            object.__setattr__(self, "default_model", default_model)
        self._client = None  # lazy

    # ── Client lazy ─────────────────────────────────────────────

    def _get_client(self):
        """Initialise le client OpenAI au premier appel. Lève si clé vide."""
        if not self._api_key:
            raise EmbeddingsAuthError(
                "OPENAI_API_KEY absente — impossible d'initialiser OpenAIEmbeddingsProvider. "
                "Laisser settings.openai_api_key vide force le MockEmbeddingsProvider via "
                "la factory get_embeddings_provider().",
                provider=self.name,
            )
        if self._client is not None:
            return self._client

        # Import local — évite de charger aiohttp au démarrage si seul
        # le Mock est utilisé (même discipline que B1).
        from openai import AsyncOpenAI  # noqa: PLC0415

        kwargs: dict[str, object] = {
            "api_key": self._api_key,
            "timeout": self._timeout,
            "max_retries": 0,  # RetryPolicy externe
        }
        if self._base_url:
            kwargs["base_url"] = self._base_url
        self._client = AsyncOpenAI(**kwargs)  # type: ignore[arg-type]
        log.info(
            "embeddings.openai.client_initialized",
            model=self.default_model,
            dim=self.dim,
            base_url=self._base_url or "default",
        )
        return self._client

    # ── Mapping SDK exceptions → types NEXYA ────────────────────

    def _map_sdk_exception(self, exc: Exception):
        """Convertit une exception du SDK openai en EmbeddingsError typée.

        Miroir direct du mapping fait dans `OpenAIChatProvider._map_sdk_exception`
        (B1) — garder les deux synchrones pour un handling uniforme côté
        service.
        """
        # Imports locaux : le SDK n'est chargé qu'à la première erreur.
        try:
            from openai import (  # noqa: PLC0415
                APIConnectionError,
                APITimeoutError,
                AuthenticationError,
                BadRequestError,
                NotFoundError,
                PermissionDeniedError,
                RateLimitError,
            )
        except ImportError:  # pragma: no cover — openai toujours présent
            return EmbeddingsUnavailableError(
                f"SDK openai indisponible : {exc}", provider=self.name
            )

        if isinstance(exc, AuthenticationError):
            return EmbeddingsAuthError("Clé OpenAI invalide ou révoquée.", provider=self.name)
        if isinstance(exc, PermissionDeniedError):
            return EmbeddingsAuthError(
                "Accès refusé par OpenAI (quota compte, bloc géographique).",
                provider=self.name,
            )
        if isinstance(exc, RateLimitError):
            retry_after: float | None = None
            response = getattr(exc, "response", None)
            if response is not None:
                hdr = response.headers.get("retry-after") if hasattr(response, "headers") else None
                if hdr:
                    try:
                        retry_after = float(hdr)
                    except ValueError:
                        retry_after = None
            return EmbeddingsRateLimitError(
                "Rate limit OpenAI embeddings atteint.",
                provider=self.name,
                retry_after=retry_after,
            )
        if isinstance(exc, NotFoundError):
            return EmbeddingsInvalidRequestError(
                f"Modèle OpenAI embeddings inconnu : {exc}", provider=self.name
            )
        if isinstance(exc, BadRequestError):
            return EmbeddingsInvalidRequestError(
                f"Requête embeddings OpenAI invalide : {exc}", provider=self.name
            )
        if isinstance(exc, (APIConnectionError, APITimeoutError)):
            return EmbeddingsUnavailableError(
                f"Connexion OpenAI embeddings échouée : {exc}", provider=self.name
            )
        return EmbeddingsUnavailableError(
            f"Erreur OpenAI embeddings non catégorisée : {exc}", provider=self.name
        )

    # ── API publique ────────────────────────────────────────────

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        task_type: str | None = None,  # ignoré par OpenAI (spécifique Gemini)
    ) -> EmbeddingsResponse:
        """Encode `texts` via OpenAI en un seul appel batch.

        Validations :
        - `texts` non-vide.
        - Chaque texte ≤ 8192 chars (limite OpenAI, au-delà 400).
        - `model` (si override) ∈ `supported_models`.
        """
        if not texts:
            raise EmbeddingsInvalidRequestError(
                "Liste de textes vide pour embed().", provider=self.name
            )
        effective_model = model or self.default_model
        if effective_model not in self.supported_models:
            raise EmbeddingsInvalidRequestError(
                f"Modèle '{effective_model}' non supporté. Modèles : {list(self.supported_models)}",
                provider=self.name,
            )
        for idx, text in enumerate(texts):
            if len(text) > 8192:
                raise EmbeddingsInvalidRequestError(
                    f"Texte #{idx} dépasse 8192 chars (cap OpenAI API).",
                    provider=self.name,
                )

        client = self._get_client()
        try:
            response = await client.embeddings.create(
                model=effective_model,
                input=texts,
            )
        except EmbeddingsError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise self._map_sdk_exception(exc) from exc

        expected_dim = self.supported_models[effective_model]
        vectors = [
            EmbeddingVector(
                values=list(item.embedding),
                dim=expected_dim,
                model=effective_model,
            )
            for item in response.data
        ]
        usage = EmbeddingsUsage(
            prompt_tokens=response.usage.prompt_tokens,
            total_tokens=response.usage.total_tokens,
        )
        log.info(
            "embeddings.openai.done",
            model=effective_model,
            count=len(vectors),
            total_tokens=usage.total_tokens,
        )
        return EmbeddingsResponse(vectors=vectors, usage=usage)
