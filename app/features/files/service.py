"""
FileUploadService — pipeline strict, sécurisé, mock-first.

Pipeline ordonné (chaque étape court-circuite les suivantes en cas de rejet
pour minimiser le coût d'un payload abusif) :

    1. Vérification MIME annoncé    → 415 FILE_TYPE_NOT_ALLOWED
    2. Lecture streaming + cap      → 413 FILE_TOO_LARGE
    3. SHA-256 pendant la lecture
    4. Détection magic-bytes        → 415 FILE_CONTENT_MISMATCH
    5. Dédup par (user, SHA)        → return existing (idempotent)
    6. Scan virus                   → 415 VIRUS_DETECTED (suspicious)
    7. Upload MinIO                 → 503 STORAGE_UNAVAILABLE
    8. INSERT DB UploadedFile
    9. Extraction texte (thread)    → status 'ok'/'empty'/'failed'
   10. Commit + return

Discipline :
- **Upload AVANT INSERT** : orphelin storage tolérable vs URL cassée côté user.
- **`asyncio.to_thread` pour l'extraction** : pypdf + DOCX parse sont
  CPU-bound, on évite de bloquer l'event loop.
- **Fail-safe extraction** : une extraction ratée n'empêche PAS l'upload de
  réussir — l'user garde son fichier, le status extraction est juste
  `'failed'`. Seul le scan virus `'suspicious'` est bloquant (rejet 415).
- **Fail-open virus failed** : si le scanner est down (`status='failed'`),
  on laisse passer avec log warning. Politique pragmatique pour MVP, on
  pourra durcir en fail-closed via settings plus tard.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import UTC, datetime
from typing import Final, Literal

import structlog
from fastapi import UploadFile
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors.exceptions import (
    DocumentsQuotaExceededException,
    FileContentMismatchException,
    FileTooLargeException,
    FileTypeNotAllowedException,
    ResourceNotFoundException,
    VirusDetectedException,
)
from app.core.storage import (
    ObjectStore,
    detect_mime_type,
    extract_text,
    get_object_store,
    get_virus_scanner,
    mimes_compatible,
)
from app.features.auth.models import User
from app.features.files.models import UploadedFile

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

_READ_CHUNK_SIZE: Final[int] = 8 * 1024  # 8 KB par chunk de lecture
_MAGIC_PROBE_BYTES: Final[int] = 4096  # inspecte les premiers 4 KB pour magic
_TEXT_PREVIEW_MAX: Final[int] = 500  # chars retournés dans la réponse API

# D4 — Mimes éligibles au chunking RAG (docs texte). Les images/audio/video
# sont exclus : pas de sémantique textuelle à indexer.
_CHUNKING_ELIGIBLE_MIMES: Final[frozenset[str]] = frozenset(
    {
        "application/pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "text/plain",
        "text/markdown",
        "text/x-markdown",
    }
)

# Extension dérivée depuis le mime pour la storage_key (lecture humaine).
_MIME_TO_EXT: Final[dict[str, str]] = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "video/mp4": "mp4",
    "audio/mpeg": "mp3",
    "audio/ogg": "ogg",
    "audio/wav": "wav",
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "text/plain": "txt",
    "text/markdown": "md",
    "text/csv": "csv",
}


AttachedKind = Literal["project_file", "library_item", "memory_document"]


def _guess_extension(mime: str) -> str:
    return _MIME_TO_EXT.get(mime.lower(), "bin")


def _build_storage_key(user_id: uuid.UUID, sha: str, mime: str) -> str:
    """Clé MinIO canonique avec sharding 2-char SHA."""
    ext = _guess_extension(mime)
    shard = sha[:2]
    return f"{user_id}/uploads/{shard}/{sha}.{ext}"


# ══════════════════════════════════════════════════════════════
# FileUploadService
# ══════════════════════════════════════════════════════════════


class FileUploadService:
    """Pipeline complet pour `POST /files/upload`."""

    # ── Owner check 404 IDOR-safe ──────────────────────────────
    @staticmethod
    async def get_for_user(
        upload_id: uuid.UUID,
        user: User,
        db: AsyncSession,
    ) -> UploadedFile:
        """Retourne un UploadedFile actif possédé par l'user courant."""
        result = await db.execute(
            select(UploadedFile).where(
                UploadedFile.id == upload_id,
                UploadedFile.user_id == user.id,
                UploadedFile.deleted_at.is_(None),
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise ResourceNotFoundException("Upload")
        return row

    # ── Streaming read avec cap ─────────────────────────────────
    @staticmethod
    async def _read_capped(upload_file: UploadFile, *, max_bytes: int) -> tuple[bytes, str]:
        """Lit le UploadFile par chunks, cumule bytes + SHA, stoppe à max.

        Retourne `(data, sha256_hex)`. Lève `FileTooLargeException` dès que
        le cap est franchi, sans chercher à tout lire (interruption précoce).
        """
        hasher = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await upload_file.read(_READ_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                log.warning(
                    "files.upload.too_large",
                    size_read=total,
                    max_bytes=max_bytes,
                    filename=upload_file.filename or "",
                )
                raise FileTooLargeException(max_mb=max_bytes // (1024 * 1024))
            hasher.update(chunk)
            chunks.append(chunk)
        return b"".join(chunks), hasher.hexdigest()

    # ── Pipeline complet ────────────────────────────────────────
    @staticmethod
    async def upload(
        user: User,
        db: AsyncSession,
        *,
        upload_file: UploadFile,
        extract_text_enabled: bool = True,
        scan_virus: bool = True,
        store: ObjectStore | None = None,
    ) -> UploadedFile:
        """Orchestration du pipeline complet.

        Lève :
        - `FileTypeNotAllowedException` (415) si MIME annoncé hors whitelist.
        - `FileTooLargeException` (413) si > `settings.files_max_upload_bytes`.
        - `FileContentMismatchException` (415) si magic-bytes ≠ MIME annoncé.
        - `VirusDetectedException` (415) si scan → suspicious.
        - `ObjectStoreUnavailableException` (503) si MinIO down.
        """
        # 1. MIME annoncé dans la whitelist.
        announced_mime = (upload_file.content_type or "").lower()
        if announced_mime not in {m.lower() for m in settings.files_allowed_mimes}:
            log.info(
                "files.upload.mime_rejected",
                mime=announced_mime,
                filename=upload_file.filename or "",
                user_id=str(user.id),
            )
            raise FileTypeNotAllowedException(mime_type=announced_mime)

        # 1.bis. D4 — Quota documents RAG (pré-flight, avant lecture bytes).
        # Uniquement pour les mimes chunking-éligibles. Permet à un user
        # Free de rejeter 402 sans même lire son PDF de 100 MB. Les
        # uploads images/audio/vidéo ne sont pas comptés.
        if announced_mime in _CHUNKING_ELIGIBLE_MIMES:
            await FileUploadService._check_documents_quota(user, db)

        # 2+3. Lecture streaming avec cap + SHA-256 calculé au passage.
        data, content_sha256 = await FileUploadService._read_capped(
            upload_file, max_bytes=settings.files_max_upload_bytes
        )

        # 4. Détection magic-bytes + comparaison avec MIME annoncé.
        detected = detect_mime_type(data[:_MAGIC_PROBE_BYTES])
        if detected is None:
            log.info(
                "files.upload.magic_unknown",
                announced=announced_mime,
                sha=content_sha256[:16],
                user_id=str(user.id),
            )
            raise FileContentMismatchException(announced=announced_mime, detected="")
        if not mimes_compatible(announced_mime, detected):
            log.warning(
                "files.upload.mime_mismatch",
                announced=announced_mime,
                detected=detected,
                sha=content_sha256[:16],
                user_id=str(user.id),
            )
            raise FileContentMismatchException(announced=announced_mime, detected=detected)

        # 5. Dédup par (user, SHA) actifs.
        existing = await db.execute(
            select(UploadedFile).where(
                UploadedFile.user_id == user.id,
                UploadedFile.content_sha256 == content_sha256,
                UploadedFile.deleted_at.is_(None),
            )
        )
        dedup_row = existing.scalar_one_or_none()
        if dedup_row is not None:
            log.info(
                "files.upload.dedup_hit",
                upload_id=str(dedup_row.id),
                sha=content_sha256[:16],
                user_id=str(user.id),
            )
            return dedup_row

        # 6. Scan virus.
        virus_status = "skipped"
        virus_signature: str | None = None
        virus_scanner_name: str | None = None
        virus_scanned_at: datetime | None = None
        if scan_virus and settings.virus_scan_enabled:
            scanner = get_virus_scanner()
            result = await scanner.scan(data, filename=upload_file.filename or "")
            virus_status = result.status
            virus_signature = result.signature
            virus_scanner_name = result.scanner
            virus_scanned_at = datetime.now(UTC)
            if result.status == "suspicious":
                log.warning(
                    "files.upload.virus_detected",
                    signature=result.signature,
                    scanner=result.scanner,
                    sha=content_sha256[:16],
                    user_id=str(user.id),
                )
                raise VirusDetectedException(
                    signature=result.signature or "",
                    scanner=result.scanner,
                )
            if result.status == "failed":
                # Fail-open : on log mais on laisse passer. Politique
                # pragmatique pour ne pas bloquer les uploads si ClamAV
                # est temporairement down.
                log.warning(
                    "files.upload.virus_scan_failed_open",
                    scanner=result.scanner,
                    sha=content_sha256[:16],
                )
        # (scan_virus=False OU virus_scan_enabled=False → status='skipped')

        # 7. Upload MinIO (avant INSERT — orphelin storage tolérable).
        store = store if store is not None else get_object_store()
        storage_key = _build_storage_key(user.id, content_sha256, detected)
        await store.upload_bytes(
            storage_key,
            data,
            mime_type=detected,
            metadata={
                "user_id": str(user.id),
                "mime_detected": detected,
                "original_filename": (upload_file.filename or "")[:128],
            },
        )

        # 8. INSERT DB.
        extension = _guess_extension(detected)
        row = UploadedFile(
            user_id=user.id,
            storage_key=storage_key,
            content_sha256=content_sha256,
            size_bytes=len(data),
            mime_type=detected,
            original_filename=upload_file.filename,
            extension=extension,
            virus_scan_status=virus_status,
            virus_scan_signature=virus_signature,
            virus_scan_scanner=virus_scanner_name,
            virus_scanned_at=virus_scanned_at,
            extraction_status="pending",
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

        # 9. Extraction texte — fail-safe, en thread pour le CPU-bound.
        # On met à jour la row en DB ET sur l'objet Python en parallèle :
        # la cohérence est garantie même si `db.refresh` est mocké ou
        # n'est pas appelé (contexte tests + défensif).
        now = datetime.now(UTC)
        if extract_text_enabled:
            try:
                extracted = await asyncio.to_thread(
                    extract_text,
                    data,
                    detected,
                    max_chars=settings.files_extraction_max_chars,
                )
                row.extraction_status = extracted.status
                row.extracted_text = extracted.text or None
                row.extracted_text_length = len(extracted.text) or None
                row.page_count = extracted.page_count
                row.extraction_truncated = extracted.truncated
                row.extracted_at = now
                await db.execute(
                    update(UploadedFile)
                    .where(UploadedFile.id == row.id)
                    .values(
                        extraction_status=row.extraction_status,
                        extracted_text=row.extracted_text,
                        extracted_text_length=row.extracted_text_length,
                        page_count=row.page_count,
                        extraction_truncated=row.extraction_truncated,
                        extracted_at=row.extracted_at,
                    )
                )
                await db.commit()
            except Exception as exc:  # noqa: BLE001
                # Fail-safe : on log, on marque failed, upload reste réussi.
                log.warning(
                    "files.upload.extract_unexpected_error",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    upload_id=str(row.id),
                )
                row.extraction_status = "failed"
                row.extracted_at = now
                await db.execute(
                    update(UploadedFile)
                    .where(UploadedFile.id == row.id)
                    .values(
                        extraction_status="failed",
                        extracted_at=now,
                    )
                )
                await db.commit()
        else:
            # Extraction désactivée explicitement → status 'skipped'.
            row.extraction_status = "skipped"
            await db.execute(
                update(UploadedFile)
                .where(UploadedFile.id == row.id)
                .values(extraction_status="skipped")
            )
            await db.commit()

        log.info(
            "files.upload.completed",
            upload_id=str(row.id),
            user_id=str(user.id),
            sha=content_sha256[:16],
            size_bytes=len(data),
            mime=detected,
            virus_status=virus_status,
            extraction_status=row.extraction_status,
        )

        # D4 — Enqueue du worker chunking RAG (fail-silent, asynchrone).
        # Uniquement pour les mimes textuels éligibles. L'upload réussit
        # indépendamment : un pool arq down ne bloque PAS la réponse 201.
        if detected in _CHUNKING_ELIGIBLE_MIMES:
            # Import local pour éviter la dépendance circulaire
            # service ↔ worker ↔ service (le worker importe
            # UploadedFile depuis ce module).
            from workers.chunk_tasks import (  # noqa: PLC0415
                enqueue_chunking,
            )

            await enqueue_chunking(row.id)

        return row

    # ── D4 : quota documents RAG ────────────────────────────────
    @staticmethod
    async def _check_documents_quota(user: User, db: AsyncSession) -> None:
        """Vérifie que l'user n'a pas atteint son plafond de documents.

        Le plafond dépend du plan (`documents_max_free` vs
        `documents_max_pro`). Compte uniquement les fichiers actifs
        (non soft-deleted) dont le mime est chunking-éligible.
        """
        plan_is_pro = bool(getattr(user, "is_pro", False))
        maximum = settings.documents_max_pro if plan_is_pro else settings.documents_max_free
        plan_label = "pro" if plan_is_pro else "free"

        count_stmt = (
            select(func.count())
            .select_from(UploadedFile)
            .where(
                UploadedFile.user_id == user.id,
                UploadedFile.deleted_at.is_(None),
                UploadedFile.mime_type.in_(list(_CHUNKING_ELIGIBLE_MIMES)),
            )
        )
        current_raw = await db.execute(count_stmt)
        current = int(current_raw.scalar_one() or 0)

        if current >= maximum:
            log.info(
                "files.upload.documents_quota_exceeded",
                user_id=str(user.id),
                current=current,
                maximum=maximum,
                plan=plan_label,
            )
            raise DocumentsQuotaExceededException(current=current, maximum=maximum, plan=plan_label)

    # ── Marquage d'attachement (info forensic) ─────────────────
    @staticmethod
    async def mark_attached(
        upload_id: uuid.UUID,
        user_id: uuid.UUID,
        *,
        kind: AttachedKind,
        target_id: uuid.UUID,
        db: AsyncSession,
    ) -> None:
        """Renseigne `(attached_to_kind, attached_to_id, attached_at)` sur
        une row UploadedFile — usage après copie des métadonnées vers
        une feature consommatrice (project_files, library_items, etc.).

        Idempotent : un 2ᵉ appel écrase les champs (utile si on ré-attache
        un upload à une row différente, ex: move entre projets).
        """
        await db.execute(
            update(UploadedFile)
            .where(
                UploadedFile.id == upload_id,
                UploadedFile.user_id == user_id,
                UploadedFile.deleted_at.is_(None),
            )
            .values(
                attached_to_kind=kind,
                attached_to_id=target_id,
                attached_at=datetime.now(UTC),
            )
        )
        await db.commit()

    # ── Helper : presigned URL pour un upload ──────────────────
    @staticmethod
    async def presigned_url_for(
        upload: UploadedFile,
        *,
        ttl_seconds: int | None = None,
        store: ObjectStore | None = None,
    ) -> str:
        store = store if store is not None else get_object_store()
        ttl = ttl_seconds or settings.files_presigned_ttl_seconds
        return await store.generate_presigned_url(upload.storage_key, ttl_seconds=ttl, method="GET")


# ══════════════════════════════════════════════════════════════
# Helper router — build preview
# ══════════════════════════════════════════════════════════════


def build_text_preview(text: str | None) -> str | None:
    """Renvoie les 500 premiers chars du texte extrait (ou None si absent)."""
    if not text:
        return None
    return text[:_TEXT_PREVIEW_MAX]
