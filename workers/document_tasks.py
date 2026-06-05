"""Worker arq — génération asynchrone de documents lourds (C4.12).

Déclenché par `DocumentGeneratorService.generate_or_enqueue` quand un document
dépasse `documents_generator_async_threshold_chars` (rendu WeasyPrint/docx
estimé > ~10s). Le rendu est déporté ici pour ne pas bloquer la requête HTTP.

Pipeline strict (toutes les étapes fail-safe absolu — aucune exception ne
remonte au runtime arq, sinon retry infini sur un échec déterministe) :

    1. Parse UUID + ouvre AsyncSessionLocal() fraîche.
    2. Load job → skip si absent / soft-deleted.
    3. `mark_processing` (transition `queued → processing`) idempotente :
       une re-livraison arq d'un job déjà processing/done/failed → skip.
    4. Re-load User (skip si purgé RGPD) + reconstruit DocumentGenerateRequest
       depuis `params_json` (le markdown source est re-fetché par `generate`
       depuis `messages`, anti-tampering).
    5. `DocumentGeneratorService.generate(...)` (méthode C4.7 inchangée).
    6. Succès → `mark_done` + push FCM « 📄 Ton document est prêt » + deep
       link `nexya://chat/{conversation_id}`.
    7. Échec → `mark_failed` + push FCM d'échec différencié.

Pattern enqueue aligné `chat_tasks.enqueue_title_generation` /
`chunk_tasks.enqueue_chunking` (lazy-pool fail-silent).
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog

from app.config import settings
from app.core.database.postgres import AsyncSessionLocal
from app.features.auth.models import User
from app.features.document_generator.exceptions import DocumentGeneratorError
from app.features.document_generator.job_service import DocumentJobService
from app.features.document_generator.schemas import (
    DocumentGenerateOptions,
    DocumentGenerateRequest,
)
from app.features.document_generator.service import DocumentGeneratorService
from app.features.notifications.service import NotificationDispatcher

if TYPE_CHECKING:
    from arq.connections import ArqRedis

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Pool arq lazy — identique chat_tasks / chunk_tasks
# ══════════════════════════════════════════════════════════════

_arq_pool: ArqRedis | None = None


async def _get_arq_pool() -> ArqRedis:
    """Pool arq paresseux — créé une seule fois par process."""
    global _arq_pool
    if _arq_pool is None:
        from arq.connections import RedisSettings, create_pool  # noqa: PLC0415

        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _arq_pool


async def enqueue_document_generation(job_id: UUID) -> None:
    """Enqueue la tâche `generate_document_async` pour un job.

    Échec silencieux (log warning + return) si Redis est down — l'appelant
    a déjà créé la row `document_jobs` (status `queued`). Si l'enqueue rate,
    le job reste `queued` et un futur cron-recovery (index partiel
    `ix_document_jobs_pending`) pourra le rattraper.
    """
    try:
        pool = await _get_arq_pool()
        await pool.enqueue_job("generate_document_async", str(job_id))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "documents.async.enqueue_failed",
            job_id=str(job_id),
            error=str(exc),
            error_type=type(exc).__name__,
        )


# ══════════════════════════════════════════════════════════════
# WORKER — generate_document_async
# ══════════════════════════════════════════════════════════════


async def generate_document_async(ctx: dict[str, Any], job_id: str) -> dict[str, Any]:
    """Rend un document en background + push FCM au résultat.

    Idempotent (double-livraison arq → skip via `mark_processing`).
    Fail-safe absolu : aucune exception ne remonte (pas de retry arq sur
    un échec déterministe de rendu).
    """
    t0 = time.monotonic()
    job_uuid = UUID(job_id)
    log.info("documents.async.job_start", job_id=job_id)

    async with AsyncSessionLocal() as db:
        # ── 1-2. Load job ─────────────────────────────────────
        try:
            from app.features.document_generator.job_models import DocumentJob

            job = await db.get(DocumentJob, job_uuid)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "documents.async.load_failed",
                job_id=job_id,
                error_type=type(exc).__name__,
            )
            return {"skipped": True, "reason": "load_failed"}

        if job is None:
            log.info("documents.async.job_missing", job_id=job_id)
            return {"skipped": True, "reason": "missing"}
        if job.deleted_at is not None:
            log.info("documents.async.job_deleted", job_id=job_id)
            return {"skipped": True, "reason": "deleted"}

        # ── 3. mark_processing idempotent ─────────────────────
        took = await DocumentJobService.mark_processing(job_uuid, db)
        if not took:
            log.info(
                "documents.async.already_taken",
                job_id=job_id,
                current_status=job.status,
            )
            return {"skipped": True, "reason": "not_queued"}

        # Snapshot des champs nécessaires (capturés AVANT tout commit
        # ultérieur — anti-MissingGreenlet).
        user_id = job.user_id
        conversation_id = job.conversation_id
        message_id = job.message_id
        fmt = job.format
        template = job.template
        params = dict(job.params_json or {})

        # ── 4. Re-load User + reconstruct body ────────────────
        user = await db.get(User, user_id)
        if user is None:
            log.warning("documents.async.user_missing", job_id=job_id)
            await DocumentJobService.mark_failed(
                job_uuid,
                error_code="USER_NOT_FOUND",
                error_message="Utilisateur introuvable (purgé ?).",
                db=db,
            )
            return {"skipped": True, "reason": "user_missing"}

        try:
            options = DocumentGenerateOptions(**dict(params.get("options") or {}))
            body = DocumentGenerateRequest(
                conversation_id=conversation_id,
                message_id=message_id,
                format=fmt,  # type: ignore[arg-type]
                template=template,  # type: ignore[arg-type]
                options=options,
                title=params.get("title"),
                remove_watermark=bool(params.get("remove_watermark", False)),
            )
        except Exception as exc:  # noqa: BLE001 — params_json corrompu
            log.error(
                "documents.async.params_invalid",
                job_id=job_id,
                error_type=type(exc).__name__,
            )
            await DocumentJobService.mark_failed(
                job_uuid,
                error_code="VALIDATION_ERROR",
                error_message=f"Paramètres de job invalides : {exc}",
                db=db,
            )
            return {"skipped": True, "reason": "params_invalid"}

        # ── 5. Rendu (méthode C4.7 inchangée) ─────────────────
        try:
            result = await DocumentGeneratorService.generate(user, body, db)
        except DocumentGeneratorError as exc:
            await DocumentJobService.mark_failed(
                job_uuid,
                error_code=exc.code,
                error_message=str(exc),
                db=db,
            )
            await _dispatch_failure(user, conversation_id, exc.code, db)
            log.warning(
                "documents.async.failed",
                job_id=job_id,
                error_code=exc.code,
                error_type=type(exc).__name__,
            )
            return {"skipped": False, "status": "failed", "error_code": exc.code}
        except Exception as exc:  # noqa: BLE001 — fail-safe absolu
            await DocumentJobService.mark_failed(
                job_uuid,
                error_code="DOCUMENT_GENERATION_FAILED",
                error_message=str(exc),
                db=db,
            )
            await _dispatch_failure(
                user, conversation_id, "DOCUMENT_GENERATION_FAILED", db
            )
            log.error(
                "documents.async.unexpected_error",
                job_id=job_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return {"skipped": False, "status": "failed", "error_code": "DOCUMENT_GENERATION_FAILED"}

        # ── 6. Succès → mark_done + push FCM ──────────────────
        await DocumentJobService.mark_done(
            job_uuid,
            library_id=result.library_id,
            filename=result.filename,
            pages=result.pages,
            size_bytes=result.size_bytes,
            truncated=result.truncated,
            db=db,
        )
        await _dispatch_success(
            user,
            conversation_id=conversation_id,
            library_id=result.library_id,
            filename=result.filename,
            fmt=fmt,
            db=db,
        )

        duration_ms = int((time.monotonic() - t0) * 1000)
        log.info(
            "documents.async.completed",
            job_id=job_id,
            user_id=str(user_id),
            library_id=str(result.library_id),
            pages=result.pages,
            size_bytes=result.size_bytes,
            truncated=result.truncated,
            duration_ms=duration_ms,
        )
        return {
            "skipped": False,
            "status": "done",
            "library_id": str(result.library_id),
            "pages": result.pages,
            "duration_ms": duration_ms,
        }


# Retry policy arq — 1 seule tentative. Un échec de rendu est déterministe
# (template KO, layout pathologique) — re-tenter ne change rien. Le worker
# catche déjà tout et marque le job `failed` proprement.
generate_document_async.max_tries = 1  # type: ignore[attr-defined]


# ══════════════════════════════════════════════════════════════
# Helpers dispatch FCM
# ══════════════════════════════════════════════════════════════


async def _dispatch_success(
    user: User,
    *,
    conversation_id: UUID,
    library_id: UUID,
    filename: str,
    fmt: str,
    db,  # noqa: ANN001 — AsyncSession, évite l'import lourd ici
) -> None:
    """Push FCM « 📄 doc prêt » + deep link vers la conversation.

    Le dispatcher est fail-safe absolu (ne lève jamais) — on n'enveloppe pas.
    """
    label = "PDF" if fmt == "pdf" else "Word"
    body = filename[: settings.fcm_body_preview_max_chars]
    await NotificationDispatcher.dispatch(
        user=user,
        category="documents",
        title=f"📄 Ton {label} est prêt",
        body=body or "Ton document est prêt à télécharger.",
        data={
            "deep_link": f"nexya://chat/{conversation_id}",
            "library_id": str(library_id),
            "conversation_id": str(conversation_id),
            "subtype": "document_ready",
            "filename": filename,
        },
        source_kind="document_generator",
        db=db,
    )


async def _dispatch_failure(
    user: User,
    conversation_id: UUID,
    error_code: str,
    db,  # noqa: ANN001
) -> None:
    """Push FCM d'échec différencié (l'user peut re-déclencher depuis la conv)."""
    await NotificationDispatcher.dispatch(
        user=user,
        category="documents",
        title="Génération du document échouée",
        body="On n'a pas pu générer ton document. Réessaie depuis la conversation.",
        data={
            "deep_link": f"nexya://chat/{conversation_id}",
            "conversation_id": str(conversation_id),
            "subtype": "document_failed",
            "error_code": error_code,
        },
        source_kind="document_generator",
        db=db,
    )
