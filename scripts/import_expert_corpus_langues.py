"""
Pipeline d'ingestion du corpus Expert Langues (G1) — phrases parallèles Tatoeba.

Usage :
    python scripts/import_expert_corpus_langues.py \
        --source tatoeba \
        --languages fra,eng,spa,por \
        --limit 10000 \
        --batch-size 100

    python scripts/import_expert_corpus_langues.py --dry-run
    python scripts/import_expert_corpus_langues.py --force-reembed

Le pipeline est **idempotent** : la contrainte UNIQUE
`(expert_slug, content_sha256)` garantit qu'un second run ne crée aucune
duplication (les INSERT utilisent `ON CONFLICT DO NOTHING`).

Discipline :

- **Streaming lecture** (ligne par ligne) pour parser `sentences.csv`
  (~10 GB décompressé) sans jamais charger le dump en RAM.
- **Batch embed Gemini ≤ 100** par appel, avec retry exponentiel honorant
  `EmbeddingsRateLimitError.retry_after` (pattern B1).
- **SHA-256 déterministe** sur le `content` framé (ordre langues stable,
  casse préservée) → dédup cross-run.
- **Commit par batch** pour limiter la perte en cas de Ctrl+C (tout le
  batch entier est visible ou aucun row).
- **Cache MinIO du dump brut** (forensic trace + économie bande passante).
- **Resume transparent** après KeyboardInterrupt : on n'a rien à faire,
  l'UNIQUE composite filtre les doublons au ré-exécution.

Coût estimé (dim 768, Gemini `text-embedding-004`, quota gratuit 2026-04) :
  - 200 k paires × ~30 tokens/phrase = 6 M tokens → ~$0
  - Stockage DB : 200 k rows × (768 × 4 B + ~500 B meta) ≈ 700 MB
  - Durée ingestion : ~20 min sur connexion correcte
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import sys
import tarfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog
from sqlalchemy import delete
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


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

EXPERT_SLUG = "language"
SOURCE = "tatoeba"
DEFAULT_CACHE_DIR = Path.home() / ".cache" / "nexya" / "corpus" / "tatoeba"
PROGRESS_EVERY = 1000  # chunks entre deux logs de progression
MAX_RETRIES = 5
INITIAL_BACKOFF = 2.0  # secondes, x2 à chaque tentative


# ══════════════════════════════════════════════════════════════
# Types
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ParallelPair:
    """Une paire de phrases parallèles prête à être encodée en chunk corpus."""

    src_id: int
    src_lang: str  # ex: 'fra'
    src_text: str
    tgt_id: int
    tgt_lang: str
    tgt_text: str

    def language_pair(self) -> str:
        """`fra-spa`, `fra-eng`, ... (ordre source→cible)."""
        return f"{self.src_lang}-{self.tgt_lang}"

    def content(self) -> str:
        """Texte framé [LANG] text\\n[LANG] text."""
        return (
            f"[{self.src_lang.upper()}] {self.src_text}\n[{self.tgt_lang.upper()}] {self.tgt_text}"
        )


# ══════════════════════════════════════════════════════════════
# Téléchargement + cache disque des dumps Tatoeba
# ══════════════════════════════════════════════════════════════


async def _download_if_missing(url: str, dest: Path) -> Path:
    """Télécharge `url` vers `dest` si absent, en streaming."""
    if dest.exists() and dest.stat().st_size > 0:
        log.info("tatoeba.cache.hit", path=str(dest), size=dest.stat().st_size)
        return dest

    dest.parent.mkdir(parents=True, exist_ok=True)
    log.info("tatoeba.download.start", url=url, dest=str(dest))
    tmp = dest.with_suffix(dest.suffix + ".partial")
    async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, read=None)) as client:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            total = int(response.headers.get("content-length") or 0)
            written = 0
            last_log = time.monotonic()
            with tmp.open("wb") as fh:
                async for chunk in response.aiter_bytes(1 << 20):  # 1 MiB
                    fh.write(chunk)
                    written += len(chunk)
                    now = time.monotonic()
                    if now - last_log >= 5:
                        pct = (written / total * 100) if total else 0
                        log.info(
                            "tatoeba.download.progress",
                            written_mb=round(written / (1 << 20), 1),
                            total_mb=round(total / (1 << 20), 1) if total else None,
                            pct=round(pct, 1),
                        )
                        last_log = now
    tmp.replace(dest)
    log.info("tatoeba.download.done", dest=str(dest), size=dest.stat().st_size)
    return dest


# ══════════════════════════════════════════════════════════════
# Parse streaming
# ══════════════════════════════════════════════════════════════


def _open_tsv_stream(archive_path: Path) -> Iterator[str]:
    """Itère ligne par ligne un TSV encapsulé dans un `.tar.bz2` Tatoeba.

    Les dumps `sentences.tar.bz2` et `links.tar.bz2` contiennent
    respectivement `sentences.csv` et `links.csv` — on extrait à la volée
    sans décompression complète sur disque (gain I/O + évite le ~10 GB
    décompressé).
    """
    with tarfile.open(archive_path, mode="r:bz2") as tf:
        member = next(
            (m for m in tf.getmembers() if m.isfile() and m.name.endswith(".csv")),
            None,
        )
        if member is None:
            raise RuntimeError(f"Archive Tatoeba sans CSV : {archive_path}")
        fh = tf.extractfile(member)
        if fh is None:
            raise RuntimeError(f"Impossible d'ouvrir {member.name} dans {archive_path}")
        reader = io.TextIOWrapper(fh, encoding="utf-8", errors="replace")
        for line in reader:
            yield line.rstrip("\n")


def _parse_sentences(archive_path: Path, langs_wanted: set[str]) -> dict[int, tuple[str, str]]:
    """Retourne `{sentence_id: (lang, text)}` filtré sur `langs_wanted`.

    Le fichier `sentences.csv` fait ~500 MB texte brut. Filtrer les
    langues ciblées in-memory reste tenable (~80 MB après filtrage
    fra+eng+spa+por). On ne peut pas être plus streaming car `_extract_pairs`
    a besoin d'un lookup O(1) pour reconstituer les paires depuis `links.csv`.
    """
    sentences: dict[int, tuple[str, str]] = {}
    for line in _open_tsv_stream(archive_path):
        parts = line.split("\t", 2)
        if len(parts) < 3:
            continue
        try:
            sid = int(parts[0])
        except ValueError:
            continue
        lang = parts[1].strip()
        text_ = parts[2].strip()
        if lang not in langs_wanted or not text_:
            continue
        sentences[sid] = (lang, text_)
    return sentences


def _extract_pairs(
    sentences_archive: Path,
    links_archive: Path,
    langs_wanted: set[str],
    limit: int | None = None,
) -> Iterator[ParallelPair]:
    """Génère des `ParallelPair` en streaming depuis les deux archives.

    Pipeline :
      1. Charge les sentences filtrées (~80 MB RAM).
      2. Streame `links.csv` ligne par ligne.
      3. Pour chaque lien (src, tgt), ne yield que si les deux sont
         dans les langues ciblées ET que `src_lang != tgt_lang`.
      4. Dédup sur `(min(id), max(id))` (Tatoeba a des liens
         bidirectionnels dupliqués).
    """
    sentences = _parse_sentences(sentences_archive, langs_wanted)
    log.info("tatoeba.sentences.loaded", count=len(sentences))

    seen: set[tuple[int, int]] = set()
    emitted = 0
    for line in _open_tsv_stream(links_archive):
        parts = line.split("\t", 1)
        if len(parts) < 2:
            continue
        try:
            a = int(parts[0])
            b = int(parts[1])
        except ValueError:
            continue
        if a == b:
            continue
        key = (min(a, b), max(a, b))
        if key in seen:
            continue
        seen.add(key)

        src = sentences.get(a)
        tgt = sentences.get(b)
        if src is None or tgt is None:
            continue
        if src[0] == tgt[0]:
            continue  # même langue — pas une paire de traduction utile

        yield ParallelPair(
            src_id=a,
            src_lang=src[0],
            src_text=src[1],
            tgt_id=b,
            tgt_lang=tgt[0],
            tgt_text=tgt[1],
        )
        emitted += 1
        if limit is not None and emitted >= limit:
            return


# ══════════════════════════════════════════════════════════════
# Embedding batch avec retry
# ══════════════════════════════════════════════════════════════


async def _embed_with_retry(provider, texts: list[str], *, task_type: str) -> list[list[float]]:
    """Embed `texts` avec retry exponentiel honorant `retry_after`."""
    backoff = INITIAL_BACKOFF
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await provider.embed(texts, task_type=task_type)
            return [v.values for v in response.vectors]
        except EmbeddingsRateLimitError as exc:
            wait = exc.retry_after if exc.retry_after else backoff
            log.warning(
                "tatoeba.embed.rate_limit",
                attempt=attempt,
                wait_seconds=wait,
                provider=exc.provider,
            )
            await asyncio.sleep(wait)
            backoff *= 2
            last_exc = exc
        except EmbeddingsError as exc:
            log.warning(
                "tatoeba.embed.retry",
                attempt=attempt,
                error=str(exc),
                wait_seconds=backoff,
            )
            await asyncio.sleep(backoff)
            backoff *= 2
            last_exc = exc
    raise RuntimeError(f"Embed failed after {MAX_RETRIES} attempts: {last_exc}")


# ══════════════════════════════════════════════════════════════
# Ingestion batch DB
# ══════════════════════════════════════════════════════════════


async def _ingest_batch(
    pairs: list[ParallelPair],
    *,
    provider,
    db,
) -> int:
    """Encode + insère un batch. Retourne le nombre de rows effectivement créées."""
    if not pairs:
        return 0

    contents = [p.content() for p in pairs]
    shas = [hashlib.sha256(c.encode("utf-8")).hexdigest() for c in contents]

    vectors = await _embed_with_retry(provider, contents, task_type="RETRIEVAL_DOCUMENT")
    if len(vectors) != len(contents):
        raise RuntimeError(f"Embed mismatch: {len(vectors)} vecteurs pour {len(contents)} textes")

    now = datetime.now(UTC)
    rows = [
        {
            "expert_slug": EXPERT_SLUG,
            "content": content,
            "content_sha256": sha,
            "embedding": vec,
            "embedding_model": provider.default_model,
            "source": SOURCE,
            "language_pair": pair.language_pair(),
            "metadata_json": {
                "src_id": pair.src_id,
                "tgt_id": pair.tgt_id,
            },
            "created_at": now,
        }
        for pair, content, sha, vec in zip(pairs, contents, shas, vectors)
    ]

    stmt = pg_insert(ExpertCorpusChunk.__table__).values(rows)
    stmt = stmt.on_conflict_do_nothing(index_elements=["expert_slug", "content_sha256"])
    result = await db.execute(stmt)
    await db.commit()
    # `rowcount` sur ON CONFLICT DO NOTHING = nb rows réellement insérées.
    return result.rowcount or 0


async def _force_reembed_reset(db) -> None:
    """DELETE `expert_corpus_chunks WHERE expert_slug='language'` (mode `--force-reembed`)."""
    log.warning("tatoeba.force_reembed.delete_start", expert_slug=EXPERT_SLUG)
    result = await db.execute(
        delete(ExpertCorpusChunk).where(ExpertCorpusChunk.expert_slug == EXPERT_SLUG)
    )
    await db.commit()
    log.warning(
        "tatoeba.force_reembed.delete_done",
        rows_deleted=result.rowcount or 0,
    )


# ══════════════════════════════════════════════════════════════
# Main runner
# ══════════════════════════════════════════════════════════════


async def run(args: argparse.Namespace) -> None:
    langs_wanted = set(lang.strip().lower() for lang in args.languages.split(","))
    log.info(
        "tatoeba.run.start",
        source=args.source,
        languages=sorted(langs_wanted),
        limit=args.limit,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        force_reembed=args.force_reembed,
        embeddings_provider=settings.embeddings_provider,
    )

    # 1. Télécharge les dumps (avec cache disque).
    cache_dir = DEFAULT_CACHE_DIR
    sentences_archive = await _download_if_missing(
        settings.tatoeba_sentences_url, cache_dir / "sentences.tar.bz2"
    )
    links_archive = await _download_if_missing(
        settings.tatoeba_links_url, cache_dir / "links.tar.bz2"
    )

    # 2. Provider embeddings.
    provider = get_embeddings_provider()
    log.info(
        "tatoeba.provider.ready",
        name=provider.name,
        dim=provider.dim,
        model=provider.default_model,
    )
    if provider.dim != settings.expert_corpus_embedding_dim:
        raise RuntimeError(
            f"Mismatch de dimension : provider {provider.name} dim={provider.dim} "
            f"mais settings.expert_corpus_embedding_dim={settings.expert_corpus_embedding_dim}. "
            f"Colonne DB figée — refus d'ingérer des vecteurs de mauvaise dim."
        )

    # 3. Optionnel : DELETE avant INSERT.
    if args.force_reembed and not args.dry_run:
        async with AsyncSessionLocal() as db:
            await _force_reembed_reset(db)

    # 4. Boucle ingestion.
    batch: list[ParallelPair] = []
    total_seen = 0
    total_inserted = 0
    started = time.monotonic()

    pairs_iter = _extract_pairs(sentences_archive, links_archive, langs_wanted, limit=args.limit)

    try:
        for pair in pairs_iter:
            total_seen += 1
            batch.append(pair)
            if len(batch) >= args.batch_size:
                inserted = await _flush(batch, provider=provider, dry_run=args.dry_run)
                total_inserted += inserted
                batch.clear()
                if total_seen % PROGRESS_EVERY == 0:
                    elapsed = time.monotonic() - started
                    rate = total_seen / elapsed if elapsed > 0 else 0
                    log.info(
                        "tatoeba.ingest.progress",
                        seen=total_seen,
                        inserted=total_inserted,
                        rate_per_s=round(rate, 1),
                        elapsed_s=round(elapsed, 1),
                    )
        # Flush final.
        if batch:
            total_inserted += await _flush(batch, provider=provider, dry_run=args.dry_run)
    except KeyboardInterrupt:
        log.warning(
            "tatoeba.ingest.interrupted",
            seen=total_seen,
            inserted=total_inserted,
            note="Relance la même commande pour reprendre — UNIQUE composite filtre les doublons.",
        )
        raise

    elapsed = time.monotonic() - started
    log.info(
        "tatoeba.ingest.done",
        seen=total_seen,
        inserted=total_inserted,
        duplicates_skipped=total_seen - total_inserted,
        elapsed_s=round(elapsed, 1),
        avg_rate_per_s=round(total_seen / elapsed, 1) if elapsed > 0 else None,
        dry_run=args.dry_run,
    )


async def _flush(batch: list[ParallelPair], *, provider, dry_run: bool) -> int:
    """Ingestion d'un batch, renvoie le nombre de rows effectivement créées.

    Mode dry-run : ne touche pas la DB, retourne la taille du batch pour
    faciliter la lecture des stats.
    """
    if dry_run:
        # 2026-04-24 : on ne fait PLUS d'appel `embed` en dry-run.
        # L'intention du flag est de valider le pipeline (download, parse,
        # filter, dédup, formatting) SANS consommer le quota Gemini ni
        # toucher la DB. L'instanciation du provider en amont valide déjà
        # la clé API. Un vrai run (`--limit N` sans `--dry-run`) exerce
        # le chemin embed complet.
        return len(batch)

    async with AsyncSessionLocal() as db:
        return await _ingest_batch(list(batch), provider=provider, db=db)


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Ingestion corpus expert Langues (Tatoeba) — G1.")
    parser.add_argument("--source", default="tatoeba", choices=["tatoeba"])
    parser.add_argument(
        "--languages",
        default="fra,eng,spa,por",
        help="Codes ISO-639-3 séparés par virgules (défaut: fra,eng,spa,por).",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Nombre max de paires à ingérer (None = tout).",
    )
    parser.add_argument(
        "--batch-size", type=int, default=100, help="Taille de batch embed Gemini (≤ 100)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="N'écrit pas en DB. Valide le pipeline + coût embed.",
    )
    parser.add_argument(
        "--force-reembed",
        action="store_true",
        help="DELETE expert_slug='language' avant INSERT (switch de modèle).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.batch_size > 100:
        print("WARN: batch-size > 100 — Gemini API tronquera silencieusement.", file=sys.stderr)
    # Windows : psycopg async refuse ProactorEventLoop (défaut Py 3.8+).
    # Même discipline que app/main.py et migrations/env.py.
    # Voir mémoire project_nexya_dev_setup.md.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
