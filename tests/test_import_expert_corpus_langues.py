"""
Tests unitaires — `scripts/import_expert_corpus_langues.py` (G1).

Valide les briques unitaires du pipeline d'ingestion sans toucher Tatoeba
ni la DB :

- `ParallelPair.content()` format framé + `language_pair()`.
- SHA-256 déterministe sur le content.
- `_embed_with_retry` : succès immédiat, retry sur rate-limit.
- CLI argparse : defaults, `--dry-run`, `--force-reembed`.
"""

from __future__ import annotations

import hashlib

import pytest

from app.ai.embeddings.base import EmbeddingsRateLimitError
from scripts.import_expert_corpus_langues import (
    ParallelPair,
    _embed_with_retry,
    _parse_args,
)

# ══════════════════════════════════════════════════════════════
# ParallelPair
# ══════════════════════════════════════════════════════════════


def test_parallel_pair_content_framed_with_lang_markers() -> None:
    p = ParallelPair(
        src_id=1,
        src_lang="fra",
        src_text="Bonjour",
        tgt_id=2,
        tgt_lang="spa",
        tgt_text="Hola",
    )
    assert p.content() == "[FRA] Bonjour\n[SPA] Hola"


def test_parallel_pair_language_pair_format() -> None:
    p = ParallelPair(1, "fra", "x", 2, "eng", "y")
    assert p.language_pair() == "fra-eng"


def test_parallel_pair_sha256_is_deterministic() -> None:
    p1 = ParallelPair(1, "fra", "Bonjour", 2, "spa", "Hola")
    p2 = ParallelPair(99, "fra", "Bonjour", 999, "spa", "Hola")
    sha1 = hashlib.sha256(p1.content().encode("utf-8")).hexdigest()
    sha2 = hashlib.sha256(p2.content().encode("utf-8")).hexdigest()
    # Même content → même SHA → ON CONFLICT DO NOTHING idempotent.
    assert sha1 == sha2


def test_parallel_pair_sha256_differs_on_content_change() -> None:
    sha1 = hashlib.sha256(
        ParallelPair(1, "fra", "Bonjour", 2, "spa", "Hola").content().encode()
    ).hexdigest()
    sha2 = hashlib.sha256(
        ParallelPair(1, "fra", "Bonsoir", 2, "spa", "Buenas").content().encode()
    ).hexdigest()
    assert sha1 != sha2


# ══════════════════════════════════════════════════════════════
# _embed_with_retry
# ══════════════════════════════════════════════════════════════


class _FakeProvider:
    default_model = "mock-768"

    def __init__(self, *, fails: int = 0) -> None:
        self._fails_remaining = fails
        self.calls = 0

    async def embed(self, texts, *, task_type=None):
        self.calls += 1
        if self._fails_remaining > 0:
            self._fails_remaining -= 1
            raise EmbeddingsRateLimitError("rate limit", provider="mock", retry_after=0.01)

        class _V:
            def __init__(self, t: str) -> None:
                self.values = [float(len(t))] * 768

        class _Resp:
            def __init__(self, ts: list[str]) -> None:
                self.vectors = [_V(t) for t in ts]

        return _Resp(texts)


@pytest.mark.asyncio
async def test_embed_with_retry_success_first_call() -> None:
    provider = _FakeProvider(fails=0)
    vecs = await _embed_with_retry(provider, ["hello", "world"], task_type="RETRIEVAL_DOCUMENT")
    assert len(vecs) == 2
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_embed_with_retry_recovers_after_rate_limit() -> None:
    provider = _FakeProvider(fails=2)
    vecs = await _embed_with_retry(provider, ["hello"], task_type="RETRIEVAL_DOCUMENT")
    assert len(vecs) == 1
    assert provider.calls == 3  # 2 échecs + 1 succès


# ══════════════════════════════════════════════════════════════
# CLI argparse
# ══════════════════════════════════════════════════════════════


def test_cli_defaults() -> None:
    args = _parse_args([])
    assert args.source == "tatoeba"
    assert args.languages == "fra,eng,spa,por"
    assert args.limit is None
    assert args.batch_size == 100
    assert args.dry_run is False
    assert args.force_reembed is False


def test_cli_dry_run_flag() -> None:
    args = _parse_args(["--dry-run"])
    assert args.dry_run is True


def test_cli_force_reembed_flag() -> None:
    args = _parse_args(["--force-reembed"])
    assert args.force_reembed is True


def test_cli_limit_and_batch_size() -> None:
    args = _parse_args(["--limit", "5000", "--batch-size", "50"])
    assert args.limit == 5000
    assert args.batch_size == 50
