"""
Embeddings — Factory singleton lazy, mock-first, multi-provider.

Pattern aligné `app/ai/runtime.py` (ChatRouter B1) et
`app/core/storage/object_store.py` (C3) :

- Premier appel `get_embeddings_provider()` décide du backend selon la
  config actuelle (clés dispos, override manuel, flag mock forcé).
- Instance cachée pour la durée du process. Un second appel retourne la
  même instance (économie cold-start + garantie de cohérence).
- `reset_embeddings_provider()` pour les tests (ré-évaluation après
  monkey-patch des settings).

Stratégie de sélection (G1 — 2026-04-26) :

    Force explicite via `settings.embeddings_provider` :
        "mock"    → MockEmbeddingsProvider(dim=expert_corpus_embedding_dim)
        "openai"  → OpenAIEmbeddingsProvider (requiert openai_api_key)
        "gemini"  → GeminiEmbeddingsProvider (requiert gemini_api_key)
        "auto"    → auto-détection (défaut) :
            1. `embeddings_mock_enabled=True` → Mock (kill-switch CI/tests).
            2. `GEMINI_API_KEY` renseignée ET `OPENAI_API_KEY` vide → Gemini
               (cas actuel au 2026-04-26 : seule clé dispo).
            3. `OPENAI_API_KEY` renseignée → OpenAI (préservation compat D1
               pre-G1 : si Ivan récupère une clé OpenAI, on repasse dessus).
            4. Sinon → Mock avec warning unique au boot.

Garantie pour Ivan : tant qu'aucune clé embeddings n'est dispo, toute
la Couche Mémoire + le corpus Experts tournent en mock avec des vecteurs
déterministes. Le jour où une clé est renseignée, tout bascule vers le
provider réel au prochain boot — zéro ligne applicative modifiée.

**Alerte switch de dim** — la colonne `expert_corpus_chunks.embedding` est
figée au DDL à `vector(768)` (dim Gemini `text-embedding-004`). Basculer
en OpenAI 1536 impose une migration : `DROP INDEX HNSW`, `ALTER COLUMN
TYPE vector(1536)`, re-ingestion complète via
`scripts/import_expert_corpus_langues.py --force-reembed`, recréation
HNSW. Estimé ~20 min. Idem pour `memories`/`document_chunks` (figés à
1536, bascule vers Gemini 768 = même procédure inverse).
"""

from __future__ import annotations

import structlog

from app.ai.embeddings.base import EmbeddingsProvider
from app.ai.embeddings.gemini_embeddings import GeminiEmbeddingsProvider
from app.ai.embeddings.mock_embeddings import MockEmbeddingsProvider
from app.ai.embeddings.openai_embeddings import OpenAIEmbeddingsProvider

log = structlog.get_logger()


_PROVIDER: EmbeddingsProvider | None = None


def get_embeddings_provider() -> EmbeddingsProvider:
    """Retourne le singleton EmbeddingsProvider selon la config effective.

    Ordre de décision — voir docstring module pour les détails.
    """
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER

    from app.config import settings  # noqa: PLC0415 — évite import circulaire

    mode = (settings.embeddings_provider or "auto").lower()

    # ── Override forcé ────────────────────────────────────────────
    if mode == "mock":
        _PROVIDER = MockEmbeddingsProvider(dim=settings.expert_corpus_embedding_dim)
        log.info(
            "embeddings.provider.initialized",
            name=_PROVIDER.name,
            dim=_PROVIDER.dim,
            reason="override_mock",
        )
        return _PROVIDER

    if mode == "openai":
        _PROVIDER = OpenAIEmbeddingsProvider(default_model=settings.openai_embedding_model)
        log.info(
            "embeddings.provider.initialized",
            name=_PROVIDER.name,
            dim=_PROVIDER.dim,
            model=_PROVIDER.default_model,
            reason="override_openai",
        )
        return _PROVIDER

    if mode == "gemini":
        _PROVIDER = GeminiEmbeddingsProvider(default_model=settings.gemini_embedding_model)
        log.info(
            "embeddings.provider.initialized",
            name=_PROVIDER.name,
            dim=_PROVIDER.dim,
            model=_PROVIDER.default_model,
            reason="override_gemini",
        )
        return _PROVIDER

    # ── Auto-détection ────────────────────────────────────────────
    if settings.embeddings_mock_enabled:
        _PROVIDER = MockEmbeddingsProvider(dim=settings.expert_corpus_embedding_dim)
        log.info(
            "embeddings.provider.initialized",
            name=_PROVIDER.name,
            dim=_PROVIDER.dim,
            reason="mock_enabled",
        )
        return _PROVIDER

    has_gemini = bool(settings.gemini_api_key)
    has_openai = bool(settings.openai_api_key)

    if has_gemini and not has_openai:
        _PROVIDER = GeminiEmbeddingsProvider(default_model=settings.gemini_embedding_model)
        log.info(
            "embeddings.provider.initialized",
            name=_PROVIDER.name,
            dim=_PROVIDER.dim,
            model=_PROVIDER.default_model,
            reason="gemini_api_key_only",
        )
        return _PROVIDER

    if has_openai:
        _PROVIDER = OpenAIEmbeddingsProvider(default_model=settings.openai_embedding_model)
        log.info(
            "embeddings.provider.initialized",
            name=_PROVIDER.name,
            dim=_PROVIDER.dim,
            model=_PROVIDER.default_model,
            reason="openai_api_key_present",
        )
        return _PROVIDER

    # Aucune clé → fallback Mock avec warning unique.
    _PROVIDER = MockEmbeddingsProvider(dim=settings.expert_corpus_embedding_dim)
    log.warning(
        "embeddings.provider.initialized",
        name=_PROVIDER.name,
        dim=_PROVIDER.dim,
        reason="no_api_key_fallback_mock",
    )
    return _PROVIDER


def reset_embeddings_provider() -> None:
    """Réinitialise le singleton — usage tests uniquement."""
    global _PROVIDER
    _PROVIDER = None


# Alias explicite demandé par la spec G1 pour les tests.
reset_embeddings_provider_for_tests = reset_embeddings_provider
