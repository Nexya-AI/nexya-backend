"""
Tests unitaires — RAG juridique (expert `legal`, activation 2026-06-09).

Couvre les 4 briques ajoutées pour rendre le retrieval « conscient du domaine »
(le filtre par domaine évite que les grosses compilations CGI / Code civil ne
noient les codes spécialisés comme le travail ou CIMA) :

1. `_detect_legal_domain` — heuristique de domaine (haute précision + priorité).
2. `ExpertCorpusService.search(..., domain=...)` — clause SQL conditionnelle.
3. `build_expert_corpus_context` — passe bien le domaine détecté à la recherche.
4. `ExpertConfig` legal — réglages RAG per-expert (k / min_sim / max_chars).

100 % en mock (aucun appel Vertex, aucune vraie DB), à l'image de
`test_expert_corpus_service.py` et `test_expert_context_builder.py`.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.embeddings.base import EmbeddingsResponse, EmbeddingsUsage, EmbeddingVector
from app.ai.experts import EXPERT_REGISTRY, get_expert_config
from app.features.experts.context_builder import _detect_legal_domain
from app.features.experts.service import ExpertCorpusService

# ══════════════════════════════════════════════════════════════
# 1. Heuristique de détection du domaine juridique
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("query", "expected_domain"),
    [
        ("Quel est le délai de préavis en cas de licenciement ?", "travail"),
        ("Obligations de l'assureur et indemnisation du sinistre", "assurances"),
        ("Comment créer une SARL en droit OHADA ?", "ohada-societes"),
        ("Quelles sont les peines pour vol ?", "penal"),
        ("Comment obtenir un titre foncier au Cameroun ?", "foncier"),
        ("Quelles sanctions pour la cybercriminalité ?", "cyber"),
        ("Le droit d'auteur protège-t-il une œuvre musicale ?", "propriete-intellectuelle"),
        ("Responsabilité civile et réparation du dommage", "civil"),
        ("Garde à vue et rôle du juge d'instruction", "procedure-penale"),
        ("Règles de certification semencière", "commerce-agricole"),
        ("Inscription au registre du commerce et fonds de commerce", "ohada-commercial"),
    ],
)
def test_detect_legal_domain_maps_query_to_code(query: str, expected_domain: str) -> None:
    assert _detect_legal_domain(query) == expected_domain


def test_detect_legal_domain_priority_fiscal_over_societes() -> None:
    """« impôt sur les sociétés » doit aller au fiscal (CGI), pas à OHADA
    sociétés — le fiscal est testé avant pour résoudre l'ambiguïté."""
    assert _detect_legal_domain("Quel est le taux de l'impôt sur les sociétés ?") == "fiscal"


@pytest.mark.parametrize(
    "query",
    [
        "Bonjour, comment vas-tu aujourd'hui ?",
        "Quelle est la météo demain à Douala ?",
        "Raconte-moi une histoire drôle",
    ],
)
def test_detect_legal_domain_none_when_no_legal_signal(query: str) -> None:
    """Aucun signal juridique fort → None → recherche non filtrée (sûr)."""
    assert _detect_legal_domain(query) is None


# ══════════════════════════════════════════════════════════════
# 2. Filtre SQL `domain` dans ExpertCorpusService.search
# ══════════════════════════════════════════════════════════════


def _make_db() -> MagicMock:
    mappings_mock = MagicMock()
    mappings_mock.all.return_value = []
    result_mock = MagicMock()
    result_mock.mappings.return_value = mappings_mock
    db = MagicMock()
    db.execute = AsyncMock(return_value=result_mock)
    return db


@pytest.mark.asyncio
async def test_search_domain_clause_absent_by_default() -> None:
    db = _make_db()
    await ExpertCorpusService.search(db, expert_slug="legal", query_embedding=[0.0] * 4)
    sql = str(db.execute.await_args_list[0].args[0])
    assert "metadata_json->>'domain'" not in sql


@pytest.mark.asyncio
async def test_search_domain_clause_present_when_domain_passed() -> None:
    db = _make_db()
    await ExpertCorpusService.search(
        db, expert_slug="legal", query_embedding=[0.0] * 4, domain="travail"
    )
    call = db.execute.await_args_list[0].args[0]
    sql = str(call)
    assert "metadata_json->>'domain' = :domain" in sql
    assert call.compile().params.get("domain") == "travail"


@pytest.mark.asyncio
async def test_search_domain_and_language_pair_independent() -> None:
    """Les deux filtres optionnels coexistent sans interférence."""
    db = _make_db()
    await ExpertCorpusService.search(
        db,
        expert_slug="language",
        query_embedding=[0.0] * 4,
        language_pair="fra-spa",
        domain="travail",
    )
    sql = str(db.execute.await_args_list[0].args[0])
    assert "language_pair = :lang" in sql
    assert "metadata_json->>'domain' = :domain" in sql


# ══════════════════════════════════════════════════════════════
# 3. Câblage build_expert_corpus_context → domaine
# ══════════════════════════════════════════════════════════════


class _FakeProvider:
    name = "fake"
    dim = 768
    default_model = "fake-768"

    async def embed(self, texts, *, model=None, task_type=None):  # noqa: ANN001
        return EmbeddingsResponse(
            vectors=[EmbeddingVector(values=[0.1] * 768, dim=768, model="fake-768")],
            usage=EmbeddingsUsage(prompt_tokens=1, total_tokens=1),
        )


@pytest.mark.asyncio
async def test_context_builder_passes_detected_domain_for_legal(monkeypatch) -> None:
    """Expert legal + question travail → search appelé avec domain='travail'."""
    from app.config import settings
    from app.features.experts import context_builder as cb

    monkeypatch.setattr(settings, "expert_corpus_enabled", True, raising=False)
    calls: list[dict] = []

    async def fake_search(db, **kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return []

    monkeypatch.setattr(cb.ExpertCorpusService, "search", staticmethod(fake_search))

    await cb.build_expert_corpus_context(
        expert_slug="legal",
        query="Quel est le délai de préavis en cas de licenciement ?",
        db=object(),
        provider=_FakeProvider(),
    )
    # Le 1er essai porte le domaine détecté (le 2e est le fallback domain=None).
    assert calls[0].get("domain") == "travail"


@pytest.mark.asyncio
async def test_context_builder_no_domain_for_non_legal_expert(monkeypatch) -> None:
    """Expert non-legal (cooking) → jamais de filtre domaine juridique."""
    from app.config import settings
    from app.features.experts import context_builder as cb

    monkeypatch.setattr(settings, "expert_corpus_enabled", True, raising=False)
    captured: dict = {}

    async def fake_search(db, **kwargs):  # noqa: ANN001
        captured.update(kwargs)
        return []

    monkeypatch.setattr(cb.ExpertCorpusService, "search", staticmethod(fake_search))

    await cb.build_expert_corpus_context(
        expert_slug="cooking",
        query="Comment préparer un ndolé avec préavis de licenciement ?",
        db=object(),
        provider=_FakeProvider(),
    )
    assert captured.get("domain") is None


@pytest.mark.asyncio
async def test_context_builder_falls_back_when_domain_yields_nothing(monkeypatch) -> None:
    """Si le filtre domaine ne ramène rien, on relâche (2e search domain=None)."""
    from app.config import settings
    from app.features.experts import context_builder as cb

    monkeypatch.setattr(settings, "expert_corpus_enabled", True, raising=False)
    calls: list[dict] = []

    async def fake_search(db, **kwargs):  # noqa: ANN001
        calls.append(kwargs)
        return []  # toujours vide → déclenche le fallback

    monkeypatch.setattr(cb.ExpertCorpusService, "search", staticmethod(fake_search))

    await cb.build_expert_corpus_context(
        expert_slug="legal",
        query="Quelles sont les peines pour vol ?",
        db=object(),
        provider=_FakeProvider(),
    )
    assert len(calls) == 2
    assert calls[0].get("domain") == "penal"
    assert calls[1].get("domain") is None


# ══════════════════════════════════════════════════════════════
# 4. Réglages RAG per-expert (ExpertConfig)
# ══════════════════════════════════════════════════════════════


def test_legal_expert_corpus_activated_and_tuned() -> None:
    cfg = get_expert_config("legal")
    assert cfg.corpus_enabled is True
    assert cfg.corpus_k == 6
    assert cfg.corpus_min_similarity == pytest.approx(0.55)
    assert cfg.corpus_max_chars == 8000
    # garde-fous safety-critical préservés
    assert cfg.disclaimer is not None
    assert cfg.temperature <= 0.2


def test_cooking_corpus_params_remain_default_none() -> None:
    """Cuisine non touchée : fallback sur les settings globaux (None)."""
    cfg = get_expert_config("cooking")
    assert cfg.corpus_enabled is True
    assert cfg.corpus_k is None
    assert cfg.corpus_min_similarity is None
    assert cfg.corpus_max_chars is None


def test_legal_is_corpus_enabled_in_registry() -> None:
    enabled = {eid for eid, cfg in EXPERT_REGISTRY.items() if cfg.corpus_enabled}
    assert "legal" in enabled
    assert "cooking" in enabled
