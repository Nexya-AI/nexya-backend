"""DocumentJobService — CRUD des jobs de génération asynchrone (C4.12).

Sépare la persistance/lecture des jobs (`document_jobs`) du rendu lui-même
(`DocumentGeneratorService`). Consommé par :
    - le router (`generate_or_enqueue` crée le job, `GET .../jobs/{id}` le lit)
    - le worker arq (`generate_document_async` lit + marque processing/done/failed)

Pattern strict NEXYA :
    - 404 IDOR-safe via `_get_owned` (jamais 403, anti-énumération UUID)
    - capture `str(...)` des attributs ORM AVANT tout commit (anti-MissingGreenlet)
    - presigned URL régénérée FRAÎCHE à chaque `to_response` (jamais persistée)
    - fail-safe sur le presign (MinIO down → download_url=None, pas de 500)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors.exceptions import ResourceNotFoundException
from app.features.auth.models import User
from app.features.library.service import LibraryService

from .job_models import DocumentJob
from .schemas import DocumentGenerateRequest, DocumentJobResponse

log = structlog.get_logger(__name__)


class DocumentJobService:
    """CRUD + helpers pour la table `document_jobs` (méthodes statiques)."""

    # ── Lecture IDOR-safe ─────────────────────────────────────────
    @staticmethod
    async def _get_owned(job_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession) -> DocumentJob:
        """Charge un job possédé par l'user. 404 si absent / pas owned /
        soft-deleted (jamais 403 — anti-énumération UUID, pattern NEXYA)."""
        result = await db.execute(
            select(DocumentJob).where(
                DocumentJob.id == job_id,
                DocumentJob.user_id == user_id,
                DocumentJob.deleted_at.is_(None),
            )
        )
        job = result.scalar_one_or_none()
        if job is None:
            raise ResourceNotFoundException("Job de génération")
        return job

    @staticmethod
    async def get_owned_job(job_id: uuid.UUID, user: User, db: AsyncSession) -> DocumentJob:
        """Wrapper public pour le router de polling."""
        return await DocumentJobService._get_owned(job_id, user.id, db)

    # ── Création (router) ─────────────────────────────────────────
    @staticmethod
    async def create_job(
        user: User,
        body: DocumentGenerateRequest,
        db: AsyncSession,
    ) -> DocumentJob:
        """INSERT un job `queued` à partir d'une requête validée.

        Stocke les paramètres de rendu (PAS le markdown source — re-fetch
        par le worker depuis `messages`, anti-tampering).
        """
        # Anti-MissingGreenlet : capture des str AVANT le commit éventuel.
        user_id_str = str(user.id)

        params_json: dict[str, Any] = {
            "options": body.options.model_dump(exclude_none=True),
            "remove_watermark": body.remove_watermark,
        }
        if body.title is not None:
            params_json["title"] = body.title

        job = DocumentJob(
            user_id=user.id,
            conversation_id=body.conversation_id,
            message_id=body.message_id,
            status="queued",
            format=body.format,
            template=body.template,
            params_json=params_json,
        )
        db.add(job)
        await db.commit()
        await db.refresh(job)

        log.info(
            "documents.job.created",
            job_id=str(job.id),
            user_id=user_id_str,
            format=body.format,
            template=body.template,
        )
        return job

    # ── Transitions d'état (worker) ───────────────────────────────
    @staticmethod
    async def mark_processing(job_id: uuid.UUID, db: AsyncSession) -> bool:
        """Pose `processing` UNIQUEMENT si le job est encore `queued`.

        Retourne True si la transition a eu lieu (idempotence stricte :
        une re-livraison arq d'un job déjà `processing`/`done`/`failed`
        ne re-déclenche pas le rendu).
        """
        now = datetime.now(UTC)
        result = await db.execute(
            update(DocumentJob)
            .where(DocumentJob.id == job_id, DocumentJob.status == "queued")
            .values(status="processing", updated_at=now)
        )
        await db.commit()
        return int(result.rowcount or 0) > 0

    @staticmethod
    async def mark_done(
        job_id: uuid.UUID,
        *,
        library_id: uuid.UUID,
        filename: str,
        pages: int,
        size_bytes: int,
        truncated: bool,
        db: AsyncSession,
    ) -> None:
        now = datetime.now(UTC)
        await db.execute(
            update(DocumentJob)
            .where(DocumentJob.id == job_id)
            .values(
                status="done",
                library_id=library_id,
                filename=filename,
                pages=pages,
                size_bytes=size_bytes,
                truncated=truncated,
                completed_at=now,
                updated_at=now,
            )
        )
        await db.commit()

    @staticmethod
    async def mark_failed(
        job_id: uuid.UUID,
        *,
        error_code: str,
        error_message: str,
        db: AsyncSession,
    ) -> None:
        now = datetime.now(UTC)
        await db.execute(
            update(DocumentJob)
            .where(DocumentJob.id == job_id)
            .values(
                status="failed",
                error_code=error_code,
                error_message=error_message[:2000],
                completed_at=now,
                updated_at=now,
            )
        )
        await db.commit()

    # ── Sérialisation (polling) ───────────────────────────────────
    @staticmethod
    async def to_response(job: DocumentJob, user: User, db: AsyncSession) -> DocumentJobResponse:
        """Construit la réponse de polling.

        Si `done` + `library_id`, régénère un presigned URL FRAIS (TTL 30 min)
        à chaque appel (jamais persisté). Fail-safe : MinIO down → URL=None
        plutôt qu'un 500 (le client retentera plus tard).
        """
        download_url: str | None = None
        if job.status == "done" and job.library_id is not None:
            try:
                item = await LibraryService.get(job.library_id, user, db)
                download_url = await LibraryService.presigned_url_for(
                    item,
                    ttl_seconds=settings.documents_generator_presigned_ttl_seconds,
                )
            except Exception as exc:  # noqa: BLE001 — fail-safe presign
                log.warning(
                    "documents.job.presign_failed",
                    job_id=str(job.id),
                    library_id=str(job.library_id),
                    error_type=type(exc).__name__,
                )

        return DocumentJobResponse(
            job_id=job.id,
            status=job.status,  # type: ignore[arg-type]  (CHECK garantit le Literal)
            format=job.format,  # type: ignore[arg-type]
            template=job.template,  # type: ignore[arg-type]
            library_id=job.library_id,
            download_url=download_url,
            filename=job.filename,
            pages=job.pages,
            size_bytes=job.size_bytes,
            truncated=job.truncated,
            error_code=job.error_code,
            created_at=job.created_at,
            completed_at=job.completed_at,
        )
