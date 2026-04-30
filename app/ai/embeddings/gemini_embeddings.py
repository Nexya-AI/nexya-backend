"""
Gemini embeddings provider — `text-embedding-004` (768 dim), via `google-genai`.

Session G1 (2026-04-26) — socle RAG Experts. Alternatif à OpenAI
`text-embedding-3-small` (1536 dim) quand `OPENAI_API_KEY` est vide mais
`GEMINI_API_KEY` renseignée. Dimension inférieure (768) mais qualité
retrieval quasi-équivalente sur les langues européennes courantes
(FR/EN/ES/PT) — le corpus expert Langues s'y plie nativement.

Différence Google-specific : **task_type asymétrique**.
- `RETRIEVAL_DOCUMENT` pour embed des chunks indexés (corpus Tatoeba).
- `RETRIEVAL_QUERY` pour embed la question user à runtime.
Gemini produit deux projections vectorielles distinctes alignées sur
l'espace latent de recherche — gain recall@k mesurable vs `task_type`
neutre. OpenAI et Mock ignorent ce paramètre, c'est propre au SDK Google.

Discipline :
- Client `genai.Client(api_key=…)` lazy (pattern B1 OpenAI) pour éviter
  le cold-start SDK au démarrage si seul le Mock est utilisé.
- `google-genai` API embeddings n'accepte pas de batch > 100 items par
  appel. On splitte et on agrège côté backend pour garder le contrat
  `EmbeddingsProvider.embed(texts)` batch-natif unique.
- Mapping exceptions SDK Google → `EmbeddingsError` typées miroir OpenAI.
- Cap texte 2048 chars par item (limite pratique Gemini embedding — au-delà
  le serveur tronque silencieusement, on préfère un 4xx explicite côté
  backend pour forcer la bonne segmentation amont).

Modèles supportés côté Gemini :
- `text-embedding-004` — 768 dim, **défaut NEXYA G1**. Gratuit dans le
  quota standard Gemini (quotas publics AI Studio 2026-04).
- `embedding-001` — 768 dim, legacy (v1beta), à éviter pour nouvelles
  ingestions.
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


# Cap batch Gemini embeddings (API publique — au-delà, 400).
_GEMINI_EMBED_BATCH_CAP: Final[int] = 100
# Cap caractères par item (Gemini tronque silencieusement au-delà).
_GEMINI_EMBED_TEXT_CAP: Final[int] = 2048


class GeminiEmbeddingsProvider(EmbeddingsProvider):
    """Embeddings Gemini via `google-genai` async — `text-embedding-004`.

    Initialisé avec la clé `settings.gemini_api_key`. Si vide, lève
    `EmbeddingsAuthError` dès le premier `embed()` — la factory
    `get_embeddings_provider()` doit avoir détecté l'absence de clé et
    retourné le Mock avant même d'instancier cette classe (pattern
    aligné `OpenAIEmbeddingsProvider`).

    `supported_models` listé pour traçabilité + validation : on rejette
    un override `model=` vers un modèle non supporté — Gemini retournerait
    un 400, autant filtrer côté backend avec un message actionnable.
    """

    name: Final[str] = "gemini"
    # 2026-04-24 : Google a renommé `text-embedding-004` en `gemini-embedding-001`
    # (le nom historique renvoie 404 NOT_FOUND sur v1beta). Le nouveau modèle
    # conserve 768 dim (compatible avec la migration 016 `vector(768)`).
    default_model: Final[str] = "gemini-embedding-001"
    dim: Final[int] = 768

    supported_models: Final[dict[str, int]] = {
        "gemini-embedding-001": 768,
        # "text-embedding-004" ancien nom — retourne 404 NOT_FOUND sur v1beta
        # depuis le rebranding Gemini 2026. Conservé pour référence.
    }

    def __init__(
        self,
        *,
        api_key: str | None = None,
        default_model: str | None = None,
        use_vertex: bool | None = None,
        gcp_project: str | None = None,
        gcp_region: str | None = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else settings.gemini_api_key
        # Bascule Vertex AI / AI Studio. Si `use_vertex=True`, on ignore
        # `api_key` et on s'appuie sur Application Default Credentials (ADC)
        # via `gcloud auth application-default login`. Le project + region
        # sont obligatoires en mode Vertex.
        self._use_vertex = use_vertex if use_vertex is not None else settings.gemini_use_vertex
        self._gcp_project = gcp_project if gcp_project is not None else settings.gcp_project_id
        self._gcp_region = gcp_region if gcp_region is not None else settings.gcp_region
        if default_model:
            # Permet à un test ou une config prod de forcer un autre modèle.
            object.__setattr__(self, "default_model", default_model)
        self._client = None  # lazy

    # ── Client lazy ─────────────────────────────────────────────

    def _get_client(self):
        """Initialise le client `genai` au premier appel.

        Deux modes d'authentification :
        - **Vertex AI** (`use_vertex=True`) : utilise les Application Default
          Credentials (ADC) + project + region. Pas d'api_key. Nécessite
          `gcloud auth application-default login` + projet GCP actif.
        - **AI Studio** (défaut) : utilise `GEMINI_API_KEY`. Le plus simple
          mais quota/billing séparés de Google Cloud.
        """
        if self._client is not None:
            return self._client

        # Import local — évite de charger `google-genai` au démarrage si
        # seul le Mock est utilisé (même discipline que OpenAI).
        from google import genai  # noqa: PLC0415

        if self._use_vertex:
            if not self._gcp_project:
                raise EmbeddingsAuthError(
                    "GCP_PROJECT_ID absent — impossible d'initialiser "
                    "GeminiEmbeddingsProvider en mode Vertex AI. "
                    "Renseigner GCP_PROJECT_ID dans .env ou basculer sur "
                    "AI Studio en mettant GEMINI_USE_VERTEX=false.",
                    provider=self.name,
                )
            self._client = genai.Client(
                vertexai=True,
                project=self._gcp_project,
                location=self._gcp_region,
            )
            log.info(
                "embeddings.gemini.client_initialized",
                model=self.default_model,
                dim=self.dim,
                mode="vertex",
                project=self._gcp_project,
                region=self._gcp_region,
            )
        else:
            if not self._api_key:
                raise EmbeddingsAuthError(
                    "GEMINI_API_KEY absente — impossible d'initialiser "
                    "GeminiEmbeddingsProvider en mode AI Studio. Laisser "
                    "settings.gemini_api_key vide force le "
                    "MockEmbeddingsProvider via get_embeddings_provider(). "
                    "Alternative : activer GEMINI_USE_VERTEX=true avec "
                    "GCP_PROJECT_ID renseigné.",
                    provider=self.name,
                )
            self._client = genai.Client(api_key=self._api_key)
            log.info(
                "embeddings.gemini.client_initialized",
                model=self.default_model,
                dim=self.dim,
                mode="ai_studio",
            )
        return self._client

    # ── Mapping SDK exceptions → types NEXYA ────────────────────

    def _map_sdk_exception(self, exc: Exception):
        """Convertit une exception du SDK `google-genai` en `EmbeddingsError` typée.

        Le SDK `google-genai` expose une hiérarchie `APIError` avec un
        attribut `code` qui mappe les status HTTP (401, 403, 429, 5xx).
        On inspecte le code pour router vers le bon type NEXYA. Les
        versions plus anciennes exposaient `google.api_core.exceptions.*`
        (Vertex AI classique) — on tolère les deux via duck-typing sur
        le nom de classe pour ne pas durcir la dépendance.
        """
        exc_name = type(exc).__name__
        msg = str(exc)
        code = getattr(exc, "code", None) or getattr(exc, "status_code", None)

        # Tentative 1 : code HTTP explicite.
        if code in (401, 403):
            return EmbeddingsAuthError(
                f"Authentification Gemini refusée ({code}) : {msg}",
                provider=self.name,
            )
        if code == 429:
            retry_after: float | None = None
            # Certains SDKs exposent `retry_after` ou un header Retry-After
            # via `exc.response.headers`.
            hdr = None
            response = getattr(exc, "response", None)
            if response is not None:
                headers = getattr(response, "headers", None)
                if headers is not None:
                    hdr = headers.get("retry-after") or headers.get("Retry-After")
            if hdr:
                try:
                    retry_after = float(hdr)
                except (TypeError, ValueError):
                    retry_after = None
            return EmbeddingsRateLimitError(
                f"Rate limit Gemini embeddings atteint : {msg}",
                provider=self.name,
                retry_after=retry_after,
            )
        if code in (400, 404):
            return EmbeddingsInvalidRequestError(
                f"Requête Gemini embeddings invalide ({code}) : {msg}",
                provider=self.name,
            )
        if code and 500 <= code < 600:
            return EmbeddingsUnavailableError(
                f"Gemini embeddings indisponible ({code}) : {msg}",
                provider=self.name,
            )

        # Tentative 2 : duck-type sur le nom de classe (compat google.api_core).
        if "Unauthenticated" in exc_name or "PermissionDenied" in exc_name:
            return EmbeddingsAuthError(
                f"Authentification Gemini refusée : {msg}", provider=self.name
            )
        if "ResourceExhausted" in exc_name or "TooManyRequests" in exc_name:
            return EmbeddingsRateLimitError(
                f"Rate limit Gemini embeddings atteint : {msg}",
                provider=self.name,
            )
        if "InvalidArgument" in exc_name or "NotFound" in exc_name:
            return EmbeddingsInvalidRequestError(
                f"Requête Gemini embeddings invalide : {msg}", provider=self.name
            )
        if (
            "ServiceUnavailable" in exc_name
            or "InternalServerError" in exc_name
            or "DeadlineExceeded" in exc_name
        ):
            return EmbeddingsUnavailableError(
                f"Gemini embeddings indisponible : {msg}", provider=self.name
            )

        return EmbeddingsUnavailableError(
            f"Erreur Gemini embeddings non catégorisée ({exc_name}) : {msg}",
            provider=self.name,
        )

    # ── API publique ────────────────────────────────────────────

    async def embed(
        self,
        texts: list[str],
        *,
        model: str | None = None,
        task_type: str | None = None,
    ) -> EmbeddingsResponse:
        """Encode `texts` via Gemini. Batch splitté à `_GEMINI_EMBED_BATCH_CAP`.

        `task_type` :
        - `RETRIEVAL_DOCUMENT` (défaut) : ingestion d'un corpus (phrases
          Tatoeba, chunks doc). Recommandé pour tout ce qui est indexé.
        - `RETRIEVAL_QUERY` : embed d'une requête user à runtime.
        - `SEMANTIC_SIMILARITY` / `CLASSIFICATION` / `CLUSTERING` : valides
          mais hors scope G1.

        Validations :
        - `texts` non-vide.
        - Chaque texte ≤ 2048 chars.
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
            if len(text) > _GEMINI_EMBED_TEXT_CAP:
                raise EmbeddingsInvalidRequestError(
                    f"Texte #{idx} dépasse {_GEMINI_EMBED_TEXT_CAP} chars "
                    f"(cap pratique Gemini text-embedding-004).",
                    provider=self.name,
                )

        effective_task_type = task_type or "RETRIEVAL_DOCUMENT"

        client = self._get_client()
        expected_dim = self.supported_models[effective_model]

        # Splittage en sous-batches ≤ 100 pour respecter le cap API.
        all_values: list[list[float]] = []
        total_tokens = 0
        for start in range(0, len(texts), _GEMINI_EMBED_BATCH_CAP):
            sub_batch = texts[start : start + _GEMINI_EMBED_BATCH_CAP]
            try:
                # `google-genai` expose `client.aio.models.embed_content` async.
                # `config=EmbedContentConfig(task_type=...)` côté SDK récent,
                # avec fallback sur kwarg `task_type=` direct pour les
                # versions plus anciennes.
                response = await self._embed_sub_batch(
                    client=client,
                    model=effective_model,
                    texts=sub_batch,
                    task_type=effective_task_type,
                )
            except EmbeddingsError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise self._map_sdk_exception(exc) from exc

            # Le SDK `google-genai` retourne `response.embeddings` (liste
            # de `ContentEmbedding{values: list[float]}`).
            embeddings = getattr(response, "embeddings", None)
            if embeddings is None:
                # Fallback legacy : champ `embedding` singulier pour un
                # seul input, ou structure dict brute.
                embedding = getattr(response, "embedding", None)
                if embedding is not None:
                    values = getattr(embedding, "values", embedding)
                    all_values.append(list(values))
                    total_tokens += sum(max(1, len(t) // 4) for t in sub_batch)
                    continue
                raise EmbeddingsUnavailableError(
                    "Réponse Gemini embeddings malformée (ni `.embeddings` ni `.embedding`).",
                    provider=self.name,
                )
            for item in embeddings:
                values = getattr(item, "values", None)
                if values is None and isinstance(item, dict):
                    values = item.get("values")
                if values is None:
                    raise EmbeddingsUnavailableError(
                        "Embedding Gemini sans champ `values`.",
                        provider=self.name,
                    )
                all_values.append(list(values))

            # Facturation : Gemini ne remonte pas toujours un `usage` dans
            # la réponse embeddings (contrairement à chat). Heuristique
            # tokens ≈ len/4, cohérente avec MockEmbeddingsProvider.
            total_tokens += sum(max(1, len(t) // 4) for t in sub_batch)

        if len(all_values) != len(texts):
            raise EmbeddingsUnavailableError(
                f"Gemini a retourné {len(all_values)} vecteurs pour "
                f"{len(texts)} textes — mismatch.",
                provider=self.name,
            )

        vectors = [
            EmbeddingVector(values=vals, dim=expected_dim, model=effective_model)
            for vals in all_values
        ]
        usage = EmbeddingsUsage(prompt_tokens=total_tokens, total_tokens=total_tokens)
        log.info(
            "embeddings.gemini.done",
            model=effective_model,
            count=len(vectors),
            task_type=effective_task_type,
            total_tokens=total_tokens,
        )
        return EmbeddingsResponse(vectors=vectors, usage=usage)

    async def _embed_sub_batch(
        self,
        *,
        client,
        model: str,
        texts: list[str],
        task_type: str,
    ):
        """Appel unitaire SDK pour un sous-batch ≤ 100 items.

        Isolé pour faciliter le monkey-patch côté tests (on patche cette
        méthode plutôt que `client.aio.models.embed_content` qu'on doit
        simuler de trois façons différentes selon les versions SDK).

        Stratégie tolérante aux versions :
        1. Tente `config=types.EmbedContentConfig(task_type=...)` (SDK
           `google-genai >= 1.0`).
        2. Sinon fallback sur kwarg `task_type=` direct (SDK plus ancien
           ou wrapper de compat).
        """
        aio_models = client.aio.models  # type: ignore[attr-defined]

        # Tentative 1 : `EmbedContentConfig` avec output_dimensionality.
        # Vertex AI retourne 3072 dim par défaut pour `gemini-embedding-001`,
        # alors que AI Studio retourne 768. On force 768 via
        # `output_dimensionality` pour garantir compat avec la table
        # `expert_corpus_chunks.embedding vector(768)` quel que soit le mode.
        try:
            from google.genai import types as genai_types  # noqa: PLC0415

            config = genai_types.EmbedContentConfig(
                task_type=task_type,
                output_dimensionality=self.dim,
            )
            return await aio_models.embed_content(
                model=model,
                contents=texts,
                config=config,
            )
        except (AttributeError, ImportError, TypeError):
            # Tentative 2 : kwarg direct (compat ancien SDK / stubs test).
            # Pas de output_dimensionality en kwarg — seulement via config.
            # Acceptable car ce fallback sert principalement les tests qui
            # mockent le SDK et contrôlent la dim retournée eux-mêmes.
            return await aio_models.embed_content(
                model=model,
                contents=texts,
                task_type=task_type,
            )
