"""
Pipeline d'ingestion du corpus Expert Juridique (Phase 2, étape 2).

Lit le JSONL produit par `parse_legal_corpus.py` (chunks par article + métadonnées
riches), génère les embeddings (Gemini `gemini-embedding-001`, 768d, via Vertex
ou AI Studio selon la config) et les insère dans `expert_corpus_chunks` (slug
`legal`). Réutilise exactement la tuyauterie de l'ingestion cuisine.

Usage :

    # Validation hors-ligne (aucune DB, aucun embed) : compte + ventile le JSONL
    python scripts/import_expert_corpus_legal.py --dry-run

    # Ingestion réelle (embed + INSERT pgvector) :
    #   prérequis : Docker DB up + (pour Vertex) gcloud auth + GCP project id
    python scripts/import_expert_corpus_legal.py --ingest

    # Re-ingestion complète (purge slug 'legal' puis ré-embed) :
    python scripts/import_expert_corpus_legal.py --ingest --force-reembed

Idempotent : INSERT `ON CONFLICT DO NOTHING` sur `(expert_slug, content_sha256)`.
Un re-run n'insère aucun doublon ; la vague 2 (paquet D) s'ingère par-dessus
sans rien recalculer.

Pour forcer Vertex (et donc facturer sur le crédit GCP 300$) :
    GEMINI_USE_VERTEX=true  GCP_PROJECT_ID=<projet>  GCP_REGION=us-central1
    (+ `gcloud auth application-default login` au préalable)
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import structlog
from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.ai.embeddings import (
    EmbeddingsError,
    EmbeddingsRateLimitError,
    get_embeddings_provider,
)
from app.config import settings
from app.core.database.postgres import AsyncSessionLocal
from app.features.experts.models import ExpertCorpusChunk

log = structlog.get_logger()

# ──────────────────────────────────────────────────────────────────
# Constantes
# ──────────────────────────────────────────────────────────────────

EXPERT_SLUG = "legal"

_REPO_ROOT = Path(__file__).resolve().parent.parent
_DATASET = _REPO_ROOT.parent / "DATAS SETS" / "Expert Juridique"
DEFAULT_JSONL = _DATASET / "_canonical_legal" / "legal_chunks.jsonl"

MAX_RETRIES = 5
INITIAL_BACKOFF = 2.0
PROGRESS_EVERY = 500


# ──────────────────────────────────────────────────────────────────
# Lecture + validation du JSONL
# ──────────────────────────────────────────────────────────────────


def load_chunks(jsonl_path: Path) -> list[dict]:
    """Charge le JSONL et valide la forme de chaque record."""
    if not jsonl_path.exists():
        raise FileNotFoundError(
            f"JSONL introuvable : {jsonl_path}\n"
            f"Lance d'abord : python scripts/parse_legal_corpus.py"
        )
    records: list[dict] = []
    for i, line in enumerate(jsonl_path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Ligne {i} JSON invalide : {exc}") from exc
        for key in ("expert_slug", "source", "content", "metadata"):
            if key not in r:
                raise ValueError(f"Ligne {i} : champ manquant '{key}'")
        if r["expert_slug"] != EXPERT_SLUG:
            raise ValueError(f"Ligne {i} : expert_slug={r['expert_slug']!r} != {EXPERT_SLUG!r}")
        if not r["content"].strip():
            raise ValueError(f"Ligne {i} : content vide")
        # SHA recalculé à la source pour garantir la cohérence content<->sha
        r["content_sha256"] = hashlib.sha256(r["content"].encode("utf-8")).hexdigest()
        records.append(r)
    return records


def dry_run_report(records: list[dict]) -> None:
    """Ventilation hors-ligne (sans DB)."""
    by_source: Counter[str] = Counter()
    by_domain: Counter[str] = Counter()
    by_jur: Counter[str] = Counter()
    max_chars = 0
    shas: set[str] = set()
    for r in records:
        by_source[r["source"]] += 1
        md = r["metadata"]
        by_domain[md.get("domain", "?")] += 1
        by_jur[md.get("jurisdiction", "?")] += 1
        max_chars = max(max_chars, len(r["content"]))
        shas.add(r["content_sha256"])

    print(f"\nOK dry-run legal — {len(records)} chunks")
    print(
        f"  SHA uniques        : {len(shas)} "
        f"({len(records) - len(shas)} doublons absorbés à l'INSERT)"
    )
    print(f"  max content chars  : {max_chars} (cap embed ~2048)")
    print(f"  juridictions       : {dict(by_jur)}")
    print(f"  domaines           : {len(by_domain)}")
    print(f"\n  par source ({len(by_source)} textes) :")
    for src, n in by_source.most_common():
        print(f"    {src:<26} {n:>5}")
    # estimation coût embedding
    total_chars = sum(len(r["content"]) for r in records)
    approx_tokens = total_chars // 4
    print(
        f"\n  ~{total_chars:,} chars -> ~{approx_tokens:,} tokens "
        f"-> embedding ~${approx_tokens / 1_000_000 * 0.15:.2f} (1 passe)"
    )


# ──────────────────────────────────────────────────────────────────
# Embedding (retry exponentiel)
# ──────────────────────────────────────────────────────────────────


async def _embed_with_retry(provider, texts: list[str], *, task_type: str) -> list[list[float]]:
    backoff = INITIAL_BACKOFF
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await provider.embed(texts, task_type=task_type)
            return [v.values for v in response.vectors]
        except EmbeddingsRateLimitError as exc:
            wait = exc.retry_after if exc.retry_after else backoff
            log.warning("legal.embed.rate_limit", attempt=attempt, wait_seconds=wait)
            await asyncio.sleep(wait)
            backoff *= 2
            last_exc = exc
        except EmbeddingsError as exc:
            log.warning("legal.embed.retry", attempt=attempt, error=str(exc), wait_seconds=backoff)
            await asyncio.sleep(backoff)
            backoff *= 2
            last_exc = exc
    raise RuntimeError(f"Embed failed after {MAX_RETRIES} attempts: {last_exc}")


# ──────────────────────────────────────────────────────────────────
# Ingestion DB — INSERT pgvector idempotent
# ──────────────────────────────────────────────────────────────────


async def ingest(records: list[dict], *, batch_size: int, force_reembed: bool) -> dict[str, int]:
    provider = get_embeddings_provider()
    log.info(
        "legal.ingest.start",
        chunks=len(records),
        batch_size=batch_size,
        force_reembed=force_reembed,
        provider=provider.name,
        dim=provider.dim,
        model=provider.default_model,
    )

    if provider.dim != settings.expert_corpus_embedding_dim:
        raise RuntimeError(
            f"Mismatch dim : provider {provider.name} dim={provider.dim} "
            f"mais settings.expert_corpus_embedding_dim={settings.expert_corpus_embedding_dim}."
        )

    if force_reembed:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                delete(ExpertCorpusChunk).where(ExpertCorpusChunk.expert_slug == EXPERT_SLUG)
            )
            await db.commit()
            log.warning("legal.force_reembed.delete_done", rows_deleted=result.rowcount or 0)

    total_seen = 0
    total_inserted = 0
    started = time.monotonic()

    for batch_start in range(0, len(records), batch_size):
        batch = records[batch_start : batch_start + batch_size]
        contents = [r["content"] for r in batch]
        shas = [r["content_sha256"] for r in batch]
        vectors = await _embed_with_retry(provider, contents, task_type="RETRIEVAL_DOCUMENT")
        if len(vectors) != len(contents):
            raise RuntimeError(f"Embed mismatch: {len(vectors)} vecteurs / {len(contents)} textes")

        now = datetime.now(UTC)
        rows = [
            {
                "expert_slug": EXPERT_SLUG,
                "content": r["content"],
                "content_sha256": sha,
                "embedding": vec,
                "embedding_model": provider.default_model,
                "source": r["source"][:64],
                "language_pair": None,
                "metadata_json": r["metadata"],
                "created_at": now,
            }
            for r, sha, vec in zip(batch, shas, vectors, strict=True)
        ]

        async with AsyncSessionLocal() as db:
            before = int(
                (
                    await db.execute(
                        text(
                            "SELECT COUNT(*) FROM expert_corpus_chunks WHERE expert_slug = :s"
                        ).bindparams(s=EXPERT_SLUG)
                    )
                ).scalar_one()
            )
            stmt = pg_insert(ExpertCorpusChunk.__table__).values(rows)
            stmt = stmt.on_conflict_do_nothing(index_elements=["expert_slug", "content_sha256"])
            await db.execute(stmt)
            await db.commit()
            after = int(
                (
                    await db.execute(
                        text(
                            "SELECT COUNT(*) FROM expert_corpus_chunks WHERE expert_slug = :s"
                        ).bindparams(s=EXPERT_SLUG)
                    )
                ).scalar_one()
            )

        total_seen += len(batch)
        total_inserted += max(0, after - before)
        if total_seen % PROGRESS_EVERY < batch_size or total_seen == len(records):
            log.info(
                "legal.ingest.progress",
                seen=total_seen,
                inserted=total_inserted,
                duplicates=total_seen - total_inserted,
                elapsed_s=round(time.monotonic() - started, 1),
            )

    elapsed = round(time.monotonic() - started, 1)
    log.info("legal.ingest.done", seen=total_seen, inserted=total_inserted, elapsed_s=elapsed)
    return {"seen": total_seen, "inserted": total_inserted}


# ──────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────


async def run(args: argparse.Namespace) -> None:
    jsonl = Path(args.jsonl).resolve()
    records = load_chunks(jsonl)
    log.info("legal.loaded", chunks=len(records), jsonl=str(jsonl))

    if args.dry_run:
        dry_run_report(records)
        return

    if args.ingest:
        stats = await ingest(records, batch_size=args.batch_size, force_reembed=args.force_reembed)
        print(
            f"\nOK ingestion legal : {stats['inserted']} insérés / "
            f"{stats['seen']} vus ({stats['seen'] - stats['inserted']} doublons)."
        )
        return

    print("Rien à faire : précise --dry-run ou --ingest.")


def main() -> None:
    p = argparse.ArgumentParser(description="Ingestion corpus Expert Juridique (slug 'legal').")
    p.add_argument("--jsonl", default=str(DEFAULT_JSONL), help="Chemin du legal_chunks.jsonl")
    p.add_argument("--dry-run", action="store_true", help="Validation hors-ligne (sans DB)")
    p.add_argument("--ingest", action="store_true", help="Embed + INSERT pgvector")
    p.add_argument(
        "--force-reembed", action="store_true", help="Purge slug 'legal' avant ingestion"
    )
    p.add_argument("--batch-size", type=int, default=32, help="Taille de batch embedding")
    args = p.parse_args()
    # Windows : psycopg async refuse ProactorEventLoop (défaut Py 3.8+).
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
