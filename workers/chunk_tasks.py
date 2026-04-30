"""
Worker arq — indexation RAG des chunks de documents (D4).

Pipeline déclenché depuis `FileUploadService.upload` quand un PDF / DOCX /
TXT / MD passe tous les filtres (mime whitelist, magic, scan virus,
extraction texte OK) et que le quota `documents_max_{free,pro}` n'est
pas dépassé.

Étapes strictes (court-circuit dès qu'une condition échoue) :

    1. Parse UUID + log start.
    2. Ouvre AsyncSessionLocal() fraîche.
    3. Charge UploadedFile → skip 'missing'/'deleted'/'already_indexed'/'mime_not_supported'.
    4. Acquiert sémaphore Redis `chunk:sem:{user_id}` (TTL 10 min).
       Si saturé (> max_concurrent_chunking_per_user) → raise arq.Retry(30).
    5. Télécharge le blob depuis ObjectStore via storage_key.
    6. Re-extract texte avec `inject_page_markers=True` (pour page_number).
    7. `clean_extracted_text()` : NFC + dehyphenate + strip headers + collapse.
    8. Vérif pré-chunk : texte final < `documents_pre_clean_min_chars` → skip.
    9. `chunk_text()` avec target_tokens + overlap.
   10. Vérif cap : > `documents_chunks_per_file_max` → tronque + event capped.
   11. Embed par batches de 100 avec **re-check cancel** entre batches
       (si file soft-deleted pendant qu'on embed → stop + release + return).
   12. Bulk INSERT `document_chunks` en 1 transaction.
   13. UPDATE `uploaded_files.chunks_indexed_at = NOW()` + commit.
   14. Log forensic `documents.chunk.completed`.

Retry policy : `max_tries=3`, backoff `[5, 30, 120]` secondes. Définie
comme attribut sur la fonction pour que arq l'applique automatiquement.

Release sémaphore obligatoire : `try/finally` autour des étapes 5→13.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import structlog
from arq import Retry
from sqlalchemy import select, update

from app.ai.embeddings.runtime import get_embeddings_provider
from app.config import settings
from app.core.database.postgres import AsyncSessionLocal
from app.core.database.redis import get_redis
from app.core.storage import extract_text, get_object_store
from app.features.files.chunk_models import DocumentChunk
from app.features.files.chunker import chunk_text
from app.features.files.models import UploadedFile
from app.features.files.text_cleaner import clean_extracted_text

if TYPE_CHECKING:
    from arq.connections import ArqRedis

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

# Mimes supportés pour chunking RAG. Images / audio / vidéo hors-scope.
CHUNKING_SUPPORTED_MIMES: Final[frozenset[str]] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
        "text/markdown",
        "text/x-markdown",
    }
)

# Coût par 1M tokens du modèle par défaut (text-embedding-3-small, $0.02/1M).
# Utilisé uniquement pour le log forensic `documents.chunk.completed`.
_EMBED_COST_PER_1M_TOKENS: Final[float] = 0.02

# Clé Redis sémaphore par user (comptage via INCR / DECR).
_SEMAPHORE_KEY_PREFIX: Final[str] = "chunk:sem:"


# ══════════════════════════════════════════════════════════════
# Pool arq lazy — identique memory_tasks / chat_tasks
# ══════════════════════════════════════════════════════════════

_arq_pool: ArqRedis | None = None


async def _get_arq_pool() -> ArqRedis:
    """Pool arq paresseux — créé une seule fois par process."""
    global _arq_pool
    if _arq_pool is None:
        from arq.connections import RedisSettings, create_pool  # noqa: PLC0415

        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _arq_pool


# ══════════════════════════════════════════════════════════════
# Enqueue — fail-silent depuis le FileUploadService
# ══════════════════════════════════════════════════════════════


async def enqueue_chunking(file_id: UUID) -> None:
    """Enqueue la tâche `index_document_chunks` pour un fichier uploadé.

    Échec silencieux (log warning + return) si Redis est down — l'upload
    ne doit jamais être bloqué par une panne du pool arq. Le cron
    fallback Phase 12 rattrapera via l'index partiel
    `ix_uploaded_files_chunks_pending`.
    """
    try:
        pool = await _get_arq_pool()
        await pool.enqueue_job("index_document_chunks", str(file_id))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "documents.chunk.enqueue_failed",
            file_id=str(file_id),
            error=str(exc),
            error_type=type(exc).__name__,
        )


# ══════════════════════════════════════════════════════════════
# Sémaphore Redis — bornage de concurrence par user
# ══════════════════════════════════════════════════════════════


async def _acquire_semaphore(user_id: UUID) -> bool:
    """Incrémente le compteur sémaphore pour l'user.

    Retourne `True` si le slot est acquis (compteur ≤ max), `False`
    sinon (l'appelant doit différer via arq.Retry).

    Pattern classique INCR + check + (DECR rollback si saturé). Race
    window négligeable et rollback atomique — même discipline que
    BudgetTracker.
    """
    redis = get_redis()
    key = f"{_SEMAPHORE_KEY_PREFIX}{user_id}"
    try:
        new_value = await redis.incrby(key, 1)
        if new_value == 1:
            # Premier incr → poser le TTL.
            await redis.expire(key, settings.documents_chunking_semaphore_ttl_seconds)
        if new_value > settings.max_concurrent_chunking_per_user:
            await redis.decrby(key, 1)
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        # Fail-open : si Redis déraille, on laisse passer. Mieux vaut
        # risquer une micro-saturation qu'empêcher toute indexation.
        log.warning(
            "documents.chunk.semaphore_acquire_error",
            user_id=str(user_id),
            error=str(exc),
        )
        return True


async def _release_semaphore(user_id: UUID) -> None:
    """Décrémente le compteur sémaphore. Fail-safe."""
    redis = get_redis()
    key = f"{_SEMAPHORE_KEY_PREFIX}{user_id}"
    try:
        new_value = await redis.decrby(key, 1)
        # Garde-fou : si le compteur devient négatif (bug théorique), on
        # le reset à 0 pour éviter un quota fantôme infini.
        if new_value < 0:
            await redis.set(key, 0)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "documents.chunk.semaphore_release_error",
            user_id=str(user_id),
            error=str(exc),
        )


# ══════════════════════════════════════════════════════════════
# WORKER — index_document_chunks
# ══════════════════════════════════════════════════════════════


async def index_document_chunks(ctx: dict[str, Any], file_id: str) -> dict[str, Any]:
    """Indexe un UploadedFile en N chunks pgvector.

    Idempotent : double-check de `chunks_indexed_at IS NULL` au load.
    Re-livraison arq après crash → skip 'already_indexed'.

    Retourne un dict forensic :
        {
            "skipped": bool,
            "reason": str (si skipped),
            "n_chunks": int,
            "total_tokens": int,
            "embeddings_cost_usd": float,
            "duration_ms": int,
            "truncated": bool,
            "pages": int | None,
        }
    """
    t0 = time.monotonic()
    file_uuid = UUID(file_id)
    log.info("documents.chunk.job_start", file_id=file_id)

    # ── 1-3. Load + short-circuits ────────────────────────────
    async with AsyncSessionLocal() as db:
        uploaded = await db.get(UploadedFile, file_uuid)
        if uploaded is None:
            log.warning("documents.chunk.file_missing", file_id=file_id)
            return {"skipped": True, "reason": "missing"}
        if uploaded.deleted_at is not None:
            log.info("documents.chunk.skip_deleted", file_id=file_id)
            return {"skipped": True, "reason": "deleted"}
        if uploaded.chunks_indexed_at is not None:
            log.info("documents.chunk.already_indexed", file_id=file_id)
            return {"skipped": True, "reason": "already_indexed"}
        if uploaded.mime_type not in CHUNKING_SUPPORTED_MIMES:
            log.info(
                "documents.chunk.mime_not_supported",
                file_id=file_id,
                mime=uploaded.mime_type,
            )
            return {"skipped": True, "reason": "mime_not_supported"}

        storage_key = uploaded.storage_key
        mime_type = uploaded.mime_type
        user_id = uploaded.user_id

    # ── 4. Acquérir sémaphore Redis ───────────────────────────
    acquired = await _acquire_semaphore(user_id)
    if not acquired:
        log.info(
            "documents.chunk.semaphore_saturated",
            file_id=file_id,
            user_id=str(user_id),
            max_concurrent=settings.max_concurrent_chunking_per_user,
        )
        # arq.Retry avec defer — relivré dans 30 s.
        raise Retry(defer=30)

    # ── Pipeline protégé par try/finally sémaphore ─────────────
    try:
        # ── 5. Download blob MinIO ─────────────────────────────
        store = get_object_store()
        try:
            data = await store.download_bytes(storage_key)
        except FileNotFoundError:
            log.warning(
                "documents.chunk.storage_missing",
                file_id=file_id,
                storage_key=storage_key,
            )
            await _mark_indexed(file_uuid)
            return {"skipped": True, "reason": "storage_missing"}
        except Exception as exc:
            log.warning(
                "documents.chunk.download_failed",
                file_id=file_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise

        # ── 6. Extraction texte avec marqueurs de page ─────────
        extracted = await asyncio.to_thread(
            extract_text,
            data,
            mime_type,
            max_chars=settings.files_extraction_max_chars,
            inject_page_markers=(mime_type == "application/pdf"),
        )

        if extracted.status != "ok" or not extracted.text:
            log.info(
                "documents.chunk.extraction_not_ok",
                file_id=file_id,
                status=extracted.status,
            )
            await _mark_indexed(file_uuid)
            return {
                "skipped": True,
                "reason": f"extraction_{extracted.status}",
            }

        # ── 7. Pre-cleaning ────────────────────────────────────
        cleaned = await asyncio.to_thread(clean_extracted_text, extracted.text)

        # ── 8. Garde-fou texte trop court ──────────────────────
        if len(cleaned) < settings.documents_pre_clean_min_chars:
            log.info(
                "documents.chunk.skipped_empty",
                file_id=file_id,
                cleaned_len=len(cleaned),
                min=settings.documents_pre_clean_min_chars,
            )
            await _mark_indexed(file_uuid)
            return {"skipped": True, "reason": "text_too_short"}

        # ── 9. Chunking ────────────────────────────────────────
        chunks = await asyncio.to_thread(
            chunk_text,
            cleaned,
            target_tokens=settings.documents_chunk_target_tokens,
            overlap_tokens=settings.documents_chunk_overlap_tokens,
        )

        if not chunks:
            log.info("documents.chunk.no_chunks", file_id=file_id)
            await _mark_indexed(file_uuid)
            return {"skipped": True, "reason": "no_chunks"}

        # ── 10. Cap truncation ─────────────────────────────────
        cap = settings.documents_chunks_per_file_max
        truncated = False
        if len(chunks) > cap:
            log.warning(
                "documents.chunk.capped",
                file_id=file_id,
                total=len(chunks),
                cap=cap,
            )
            chunks = chunks[:cap]
            truncated = True

        # ── 11. Embed par batches + re-check cancel ────────────
        provider = get_embeddings_provider()
        embed_model = provider.default_model
        batch_size = settings.documents_embed_batch_size
        all_vectors: list[list[float]] = []
        total_tokens = 0

        for batch_start in range(0, len(chunks), batch_size):
            # Re-check cancel mid-way : si le fichier a été soft-deleted
            # pendant qu'on embed, on stoppe proprement sans insérer.
            if batch_start > 0 and await _is_file_cancelled(file_uuid):
                log.info(
                    "documents.chunk.cancelled_midway",
                    file_id=file_id,
                    processed=batch_start,
                    total=len(chunks),
                )
                return {"skipped": True, "reason": "cancelled_midway"}

            batch = chunks[batch_start : batch_start + batch_size]
            texts = [c.content for c in batch]
            response = await provider.embed(texts)
            if len(response.vectors) != len(batch):
                log.error(
                    "documents.chunk.embed_count_mismatch",
                    file_id=file_id,
                    expected=len(batch),
                    got=len(response.vectors),
                )
                raise RuntimeError("embed returned mismatched vector count")
            for vector in response.vectors:
                all_vectors.append(vector.values)
            total_tokens += response.usage.total_tokens

        # ── 12. Bulk INSERT document_chunks ────────────────────
        now = datetime.now(UTC)
        async with AsyncSessionLocal() as db:
            # Re-check final avant INSERT — évite l'insertion d'un
            # fichier soft-deleted ou déjà indexé.
            uploaded = await db.get(UploadedFile, file_uuid)
            if uploaded is None or uploaded.deleted_at is not None:
                log.info(
                    "documents.chunk.cancelled_before_insert",
                    file_id=file_id,
                )
                return {"skipped": True, "reason": "cancelled_before_insert"}
            if uploaded.chunks_indexed_at is not None:
                log.info(
                    "documents.chunk.raced_already_indexed",
                    file_id=file_id,
                )
                return {"skipped": True, "reason": "already_indexed"}

            rows = [
                DocumentChunk(
                    file_id=file_uuid,
                    user_id=user_id,
                    chunk_index=c.index,
                    content=c.content,
                    token_count=c.token_count,
                    start_char_offset=c.start_char_offset,
                    end_char_offset=c.end_char_offset,
                    page_number=c.page_number,
                    embedding=all_vectors[i],
                    embedding_model=embed_model,
                )
                for i, c in enumerate(chunks)
            ]
            db.add_all(rows)

            # ── 13. Sentinelle + commit ────────────────────────
            await db.execute(
                update(UploadedFile)
                .where(
                    UploadedFile.id == file_uuid,
                    UploadedFile.chunks_indexed_at.is_(None),
                )
                .values(chunks_indexed_at=now, updated_at=now)
            )
            await db.commit()

        # ── 14. Log forensic ───────────────────────────────────
        duration_ms = int((time.monotonic() - t0) * 1000)
        cost_usd = round(total_tokens * _EMBED_COST_PER_1M_TOKENS / 1_000_000, 6)
        pages = max(
            (c.page_number for c in chunks if c.page_number is not None),
            default=None,
        )
        log.info(
            "documents.chunk.completed",
            file_id=file_id,
            user_id=str(user_id),
            n_chunks=len(chunks),
            total_tokens=total_tokens,
            embeddings_cost_usd=cost_usd,
            duration_ms=duration_ms,
            truncated=truncated,
            pages=pages,
            embedding_model=embed_model,
        )
        return {
            "skipped": False,
            "n_chunks": len(chunks),
            "total_tokens": total_tokens,
            "embeddings_cost_usd": cost_usd,
            "duration_ms": duration_ms,
            "truncated": truncated,
            "pages": pages,
        }
    finally:
        await _release_semaphore(user_id)


# Retry policy arq — attribut lu par le runtime quand la fonction est
# appelée. `max_tries=3` couvre les erreurs transitoires réseau /
# Redis / DB. Backoff exponentiel dans `on_job_try` du WorkerSettings
# (voir worker.py) — ou via `Retry(defer=N)` explicite si nécessaire.
index_document_chunks.max_tries = 3  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════
# Helpers privés
# ══════════════════════════════════════════════════════════════


async def _is_file_cancelled(file_uuid: UUID) -> bool:
    """Ré-interroge la DB pour détecter un soft-delete mid-chunking.

    Session éphémère pour isolation complète — on ne tient pas une
    transaction ouverte pendant tout le pipeline d'embeddings.
    """
    async with AsyncSessionLocal() as db:
        row = await db.execute(select(UploadedFile.deleted_at).where(UploadedFile.id == file_uuid))
        deleted_at = row.scalar_one_or_none()
    return deleted_at is not None


async def _mark_indexed(file_uuid: UUID) -> None:
    """Pose `chunks_indexed_at = NOW()` sans insérer de chunks.

    Utilisé pour les skip « texte trop court / pas de chunks /
    extraction failed » — évite qu'un cron fallback re-pick le fichier
    à chaque tour alors qu'il n'y a rien à indexer.
    """
    now = datetime.now(UTC)
    async with AsyncSessionLocal() as db:
        await db.execute(
            update(UploadedFile)
            .where(
                UploadedFile.id == file_uuid,
                UploadedFile.chunks_indexed_at.is_(None),
            )
            .values(chunks_indexed_at=now, updated_at=now)
        )
        await db.commit()
