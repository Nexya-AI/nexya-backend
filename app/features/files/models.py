"""
Modèle ORM Files — `UploadedFile`.

Aligné sur la migration `008_uploaded_files.py`. Utilisé comme **buffer**
entre `POST /files/upload` (pipeline physique) et les features consommatrices
aval (Project Files, Library, Memory Documents).

Discipline :
- Pas de relation `User.uploaded_files` (anti-N+1).
- Pas de relation polymorphe `attached_to` — juste un couple (kind, id)
  info-forensique pour l'audit et le cron cleanup futur.
- `extracted_text` reste sur la row (pas de jointure) pour éviter une
  indirection DB au moment de l'indexation RAG D4.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base, UUIDMixin


class UploadedFile(Base, UUIDMixin):
    """Métadonnée d'un upload utilisateur via `POST /files/upload`.

    Le binaire vit sur MinIO à l'adresse `storage_key`. Le client reçoit
    une presigned URL (TTL 30 min) à la création, re-générable via
    `GET /files/{id}` (non livré en E3 mais prévu).
    """

    __tablename__ = "uploaded_files"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    content_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(128), nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(512))
    extension: Mapped[str | None] = mapped_column(String(16))

    # Virus scan
    virus_scan_status: Mapped[str] = mapped_column(
        String(16),
        server_default="pending",
        default="pending",
        nullable=False,
    )
    virus_scan_signature: Mapped[str | None] = mapped_column(String(128))
    virus_scan_scanner: Mapped[str | None] = mapped_column(String(32))
    virus_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Text extraction
    extraction_status: Mapped[str] = mapped_column(
        String(16),
        server_default="pending",
        default="pending",
        nullable=False,
    )
    extracted_text: Mapped[str | None] = mapped_column(Text)
    extracted_text_length: Mapped[int | None] = mapped_column(Integer)
    page_count: Mapped[int | None] = mapped_column(Integer)
    extraction_truncated: Mapped[bool] = mapped_column(
        Boolean,
        server_default="false",
        default=False,
        nullable=False,
    )
    extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Attachment tracking (forensic)
    attached_to_kind: Mapped[str | None] = mapped_column(String(32))
    attached_to_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    attached_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # D4 — RAG documents : sentinelle one-shot posée par le worker
    # `index_document_chunks` après insertion réussie de tous les chunks.
    # `NULL` = pas encore indexé (éligible au worker), non-NULL = indexé
    # (idempotence garantie, re-livraison arq skip).
    chunks_indexed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Soft-delete
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint("size_bytes >= 0", name="ck_uploaded_files_size_non_negative"),
        CheckConstraint(
            "char_length(content_sha256) = 64",
            name="ck_uploaded_files_sha256_length",
        ),
        CheckConstraint(
            "virus_scan_status IN ('pending','clean','suspicious','failed','skipped')",
            name="ck_uploaded_files_virus_status",
        ),
        CheckConstraint(
            "extraction_status IN ('pending','ok','empty','unsupported','failed','skipped')",
            name="ck_uploaded_files_extraction_status",
        ),
        CheckConstraint(
            "extracted_text_length IS NULL OR extracted_text_length >= 0",
            name="ck_uploaded_files_text_length",
        ),
        CheckConstraint(
            "page_count IS NULL OR page_count >= 0",
            name="ck_uploaded_files_page_count",
        ),
        CheckConstraint(
            "attached_to_kind IS NULL OR attached_to_kind IN ("
            "'project_file','library_item','memory_document')",
            name="ck_uploaded_files_attached_kind",
        ),
        Index(
            "ix_uploaded_files_user_active",
            "user_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
        Index(
            "ix_uploaded_files_user_pending",
            "user_id",
            "created_at",
            postgresql_where=text("attached_at IS NULL AND deleted_at IS NULL"),
        ),
    )
