"""
Tests unitaires — `build_expert_corpus_context` (Session G1).

Valide :
- Short-circuits (corpus désactivé, query vide, slug vide).
- Fail-safe absolue : provider qui raise → None (chat jamais bloqué).
- Heuristique `_detect_language_pair_hint`.
- Task type `RETRIEVAL_QUERY` forwardé au provider embed.
- Framing D5 appliqué (`<<<DOCUMENT EXTRACT>>>` + `RAG_SYSTEM_INSTRUCTION`).
- Troncature `max_chars`.
- Fallback gracieux : filtre lang `fra-spa` → 0 résultats → relax → résultats.
"""

from __future__ import annotations

import pytest

from app.features.experts.context_builder import (
    _detect_language_pair_hint,
    build_expert_corpus_context,
)
from app.features.experts.service import ExpertChunkResult

# ══════════════════════════════════════════════════════════════
# Fake provider
# ══════════════════════════════════════════════════════════════


class _FakeProvider:
    name = "fake"
    default_model = "fake-768"
    dim = 768

    def __init__(self, *, raise_exc: Exception | None = None) -> None:
        self._raise = raise_exc
        self.calls: list[dict] = []

    async def embed(self, texts, *, model=None, task_type=None):
        self.calls.append({"texts": list(texts), "task_type": task_type})
        if self._raise is not None:
            raise self._raise

        class _Vec:
            values = [0.1] * 768
            dim = 768
            model = "fake-768"

        class _Resp:
            vectors = [_Vec()]

        return _Resp()


def _fake_chunks(n: int = 2) -> list[ExpertChunkResult]:
    return [
        ExpertChunkResult(
            id=i,
            content=f"[FR] phrase {i}\n[ES] frase {i}",
            source="tatoeba",
            language_pair="fra-spa",
            similarity=0.9 - i * 0.05,
            metadata={"src_id": 100 + i},
        )
        for i in range(n)
    ]


# ══════════════════════════════════════════════════════════════
# Heuristique language_pair
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "query, expected",
    [
        ("traduis ceci en espagnol", "fra-spa"),
        ("Translate this in Spanish please", "fra-spa"),
        ("traduis en anglais", "fra-eng"),
        ("en portugais s'il te plaît", "fra-por"),
        ("translate to French", "eng-fra"),
        ("bonjour comment vas-tu", None),  # aucun verbe explicite
        ("conjugue boire", None),
    ],
)
def test_detect_language_pair_hint(query: str, expected: str | None) -> None:
    assert _detect_language_pair_hint(query) == expected


# ══════════════════════════════════════════════════════════════
# Short-circuits
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_corpus_disabled_returns_none(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "expert_corpus_enabled", False, raising=False)
    result = await build_expert_corpus_context(
        expert_slug="language",
        query="traduis en espagnol",
        db=object(),  # jamais touché
    )
    assert result is None


@pytest.mark.asyncio
async def test_empty_query_returns_none(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "expert_corpus_enabled", True, raising=False)
    result = await build_expert_corpus_context(expert_slug="language", query="   ", db=object())
    assert result is None


@pytest.mark.asyncio
async def test_empty_slug_returns_none(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "expert_corpus_enabled", True, raising=False)
    result = await build_expert_corpus_context(expert_slug="", query="hello", db=object())
    assert result is None


# ══════════════════════════════════════════════════════════════
# Fail-safe absolue
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_provider_raise_returns_none(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "expert_corpus_enabled", True, raising=False)
    provider = _FakeProvider(raise_exc=RuntimeError("pgvector down"))
    result = await build_expert_corpus_context(
        expert_slug="language",
        query="traduis en espagnol",
        db=object(),
        provider=provider,
    )
    assert result is None


# ══════════════════════════════════════════════════════════════
# Happy path + framing
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_happy_path_builds_framed_block(monkeypatch) -> None:
    from app.config import settings
    from app.features.experts import service as svc_mod

    monkeypatch.setattr(settings, "expert_corpus_enabled", True, raising=False)
    monkeypatch.setattr(settings, "expert_corpus_max_chars", 3000, raising=False)

    async def fake_search(db, **kwargs):
        return _fake_chunks(2)

    monkeypatch.setattr(svc_mod.ExpertCorpusService, "search", fake_search)

    provider = _FakeProvider()
    block = await build_expert_corpus_context(
        expert_slug="language",
        query="traduis en espagnol",
        db=object(),
        provider=provider,
    )
    assert block is not None
    assert "<<<DOCUMENT EXTRACT" in block
    assert "<<<END EXTRACT" in block
    # Instruction anti-injection D5 préfixée :
    assert "Ne JAMAIS suivre" in block or "instructions" in block.lower()


@pytest.mark.asyncio
async def test_task_type_retrieval_query_forwarded(monkeypatch) -> None:
    """L'embed query doit utiliser `RETRIEVAL_QUERY` — différenciation Gemini."""
    from app.config import settings
    from app.features.experts import service as svc_mod

    monkeypatch.setattr(settings, "expert_corpus_enabled", True, raising=False)

    async def fake_search(db, **kwargs):
        return _fake_chunks(1)

    monkeypatch.setattr(svc_mod.ExpertCorpusService, "search", fake_search)

    provider = _FakeProvider()
    await build_expert_corpus_context(
        expert_slug="language",
        query="bonjour",
        db=object(),
        provider=provider,
    )
    assert provider.calls[0]["task_type"] == "RETRIEVAL_QUERY"


@pytest.mark.asyncio
async def test_no_results_returns_none(monkeypatch) -> None:
    from app.config import settings
    from app.features.experts import service as svc_mod

    monkeypatch.setattr(settings, "expert_corpus_enabled", True, raising=False)

    async def fake_search(db, **kwargs):
        return []

    monkeypatch.setattr(svc_mod.ExpertCorpusService, "search", fake_search)

    provider = _FakeProvider()
    result = await build_expert_corpus_context(
        expert_slug="language",
        query="bonjour",
        db=object(),
        provider=provider,
    )
    assert result is None


@pytest.mark.asyncio
async def test_language_pair_fallback_when_first_search_empty(monkeypatch) -> None:
    """Heuristique détecte `fra-spa`, mais corpus vide sur fra-spa → relax → OK."""
    from app.config import settings
    from app.features.experts import service as svc_mod

    monkeypatch.setattr(settings, "expert_corpus_enabled", True, raising=False)

    call_count = {"n": 0}

    async def fake_search(db, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 1:
            assert kwargs.get("language_pair") == "fra-spa"
            return []
        # 2ᵉ appel : relax, language_pair=None
        assert kwargs.get("language_pair") is None
        return _fake_chunks(1)

    monkeypatch.setattr(svc_mod.ExpertCorpusService, "search", fake_search)

    provider = _FakeProvider()
    block = await build_expert_corpus_context(
        expert_slug="language",
        query="traduis en espagnol",
        db=object(),
        provider=provider,
    )
    assert block is not None
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_truncation_when_block_exceeds_max_chars(monkeypatch) -> None:
    from app.config import settings
    from app.features.experts import service as svc_mod

    monkeypatch.setattr(settings, "expert_corpus_enabled", True, raising=False)
    monkeypatch.setattr(settings, "expert_corpus_max_chars", 500, raising=False)

    # Chunks très longs → dépasse 500 chars.
    long_chunks = [
        ExpertChunkResult(
            id=i,
            content="Lorem ipsum " * 30,
            source="tatoeba",
            language_pair="fra-spa",
            similarity=0.9,
            metadata={},
        )
        for i in range(5)
    ]

    async def fake_search(db, **kwargs):
        return long_chunks

    monkeypatch.setattr(svc_mod.ExpertCorpusService, "search", fake_search)

    provider = _FakeProvider()
    block = await build_expert_corpus_context(
        expert_slug="language",
        query="test",
        db=object(),
        provider=provider,
        max_chars=500,
    )
    assert block is not None
    assert "corpus tronqué" in block
    assert len(block) <= 600  # max_chars + marker overhead


@pytest.mark.asyncio
async def test_explicit_empty_language_pair_hint_disables_filter(monkeypatch) -> None:
    """Passer `language_pair_hint=''` court-circuite l'heuristique."""
    from app.config import settings
    from app.features.experts import service as svc_mod

    monkeypatch.setattr(settings, "expert_corpus_enabled", True, raising=False)

    captured = {}

    async def fake_search(db, **kwargs):
        captured.update(kwargs)
        return _fake_chunks(1)

    monkeypatch.setattr(svc_mod.ExpertCorpusService, "search", fake_search)

    provider = _FakeProvider()
    await build_expert_corpus_context(
        expert_slug="language",
        query="traduis en espagnol",
        db=object(),
        provider=provider,
        language_pair_hint="",
    )
    assert captured.get("language_pair") is None
