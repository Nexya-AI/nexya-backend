"""DocumentGeneratorService — orchestre rendu + LibraryService (C4.7a).

Pipeline complet (`generate` méthode publique unique) :
    1. Validation : récupération message + check ownership IDOR-safe
       (JOIN messages × conversations, 404 si mismatch)
    2. Validation cap source : len(content) ≤ documents_generator_max_source_chars
    3. Rendu HTML via `render_document_html` (Jinja2 sandbox + markdown-it)
    4. Rendu PDF via `render_html_to_pdf` (WeasyPrint timeout 30s + pikepdf)
    5. Persistance via `LibraryService.create_from_bytes` (MinIO upload + DB)
    6. Génération presigned URL TTL 30 min
    7. Retour `DocumentGenerateResponse` complet

Sécurité :
    - Anti IDOR : JOIN messages × conversations vérifie ownership
    - Anti tampering : source markdown récupéré depuis la DB (l'user ne
      peut pas modifier le contenu IA avant rendu)
    - Anti path traversal : template_name borné par Literal Pydantic
    - Anti CPU exhaust : timeout 30s sur le rendu WeasyPrint
    - Anti DDOS : rate limit géré côté router (60/h Free, 100/h Pro)

Fail-safe :
    - DocumentSourceTooLongError (413) si content > cap
    - DocumentRenderFailedError (503) sur timeout/crash WeasyPrint
    - DocumentStorageUnavailableError (503) sur MinIO failure
    - ResourceNotFoundException (404) sur ownership KO
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors.exceptions import ResourceNotFoundException
from app.features.auth.models import User
from app.features.chat.models import Conversation, Message
from app.features.library.service import LibraryService

from .exceptions import (
    DocumentSourceTooLongError,
    DocumentStorageUnavailableError,
    DocumentTruncatedError,  # exporté pour info, pas raise
)
from .schemas import (
    DocumentGenerateOptions,
    DocumentGenerateRequest,
    DocumentGenerateResponse,
    DocumentTemplate,
)
from .template_loader import render_document_html
from .weasyprint_renderer import render_html_to_pdf

log = structlog.get_logger(__name__)

# Suppress F401 — utilisé dans le pipeline indirect via library
_ = DocumentTruncatedError

# ── Constantes filename sanitization ─────────────────────────────────

_FILENAME_SAFE_PATTERN: Final[re.Pattern[str]] = re.compile(r"[^a-zA-Z0-9_\-\.]+")
"""Caractères autorisés dans un filename (FS-safe + ASCII strict)."""


def _sanitize_filename(raw: str, fallback: str) -> str:
    """Sanitize une string pour usage en filename FS-safe.

    Args:
        raw: String brute (titre user-controlled possible).
        fallback: Fallback si raw est vide ou tout est sanitisé out.

    Returns:
        Filename ≤ 100 chars, sans chars FS-unsafe.
    """
    if not raw:
        return fallback
    cleaned = _FILENAME_SAFE_PATTERN.sub("_", raw.strip())
    cleaned = cleaned.strip("._")  # Pas de leading/trailing dots/underscores
    if not cleaned:
        return fallback
    return cleaned[:100]


# ── Service principal ────────────────────────────────────────────────


class DocumentGeneratorService:
    """Orchestrateur génération PDF (méthodes statiques, pattern NEXYA).

    Aucun état mutable injecté : tout passe par les paramètres `user`
    + `db` + `body`. Singleton via instanciation directe `()` côté
    router (cost négligeable, pas de cache utile).
    """

    @staticmethod
    async def _get_owned_message_content(
        conversation_id: uuid.UUID,
        message_id: uuid.UUID,
        user_id: uuid.UUID,
        db: AsyncSession,
    ) -> str:
        """Récupère le content markdown d'un message possédé par l'user.

        Pattern strict aligné `ReportService._get_owned_message` C2.

        Owner-check via JOIN en 1 SELECT — un user qui forge un UUID
        d'un message d'un autre user reçoit un 404 (jamais 403, anti-
        énumération UUID alignée pattern NEXYA).

        Args:
            conversation_id: UUID conv (double-check supplémentaire).
            message_id: UUID message dont le content est la source.
            user_id: UUID user courant (depuis JWT).
            db: AsyncSession.

        Returns:
            String content markdown du message assistant.

        Raises:
            ResourceNotFoundException: 404 si message inexistant, soft-
                deleted, ou pas owned par user.
        """
        stmt = (
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Message.conversation_id == conversation_id,
                Message.deleted_at.is_(None),
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
        result = await db.execute(stmt)
        message = result.scalar_one_or_none()

        if message is None:
            raise ResourceNotFoundException("Message")

        if not message.content:
            # Message vide (placeholder streaming, error, cancelled) →
            # pas de source à rendre.
            raise ResourceNotFoundException("Message")

        return message.content

    @staticmethod
    async def generate(
        user: User,
        body: DocumentGenerateRequest,
        db: AsyncSession,
    ) -> DocumentGenerateResponse:
        """Génère un PDF à partir d'un message + template, persiste en
        Library, retourne le presigned URL.

        Pipeline complet (cf. docstring module).

        Args:
            user: User courant (auth via JWT).
            body: DocumentGenerateRequest Pydantic validé.
            db: AsyncSession.

        Returns:
            DocumentGenerateResponse avec library_id + download_url +
            metadata (pages, size, truncated, expires_at).

        Raises:
            ResourceNotFoundException: 404 si message/conv KO.
            DocumentSourceTooLongError: 413 si content > cap.
            DocumentRenderFailedError: 503 si WeasyPrint timeout/crash.
            DocumentStorageUnavailableError: 503 si MinIO fail.
        """
        # 1. Récupération source markdown (avec owner-check IDOR-safe)
        markdown_source = await DocumentGeneratorService._get_owned_message_content(
            conversation_id=body.conversation_id,
            message_id=body.message_id,
            user_id=user.id,
            db=db,
        )

        # 2. Cap source chars (anti CPU exhaust + anti PDF géant)
        max_chars = settings.documents_generator_max_source_chars
        if len(markdown_source) > max_chars:
            log.warning(
                "documents.source_too_long",
                source_chars=len(markdown_source),
                max_chars=max_chars,
                user_id=str(user.id),
            )
            raise DocumentSourceTooLongError(
                f"Le contenu source dépasse {max_chars} caractères "
                f"(actuel : {len(markdown_source)}). Scinde le document "
                "en plusieurs parties plus courtes."
            )

        # 3. Rendu HTML via template Jinja2
        html_content = render_document_html(
            template_name=body.template,
            title=body.title,
            markdown_source=markdown_source,
            options=body.options,
        )

        # 4. Rendu PDF via WeasyPrint + pikepdf
        rendered = await render_html_to_pdf(
            html_content,
            timeout_seconds=settings.documents_generator_render_timeout_seconds,
            max_pages=settings.documents_generator_max_pages,
        )

        # 5. Génération filename FS-safe
        title_for_filename = body.title or f"document_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        safe_basename = _sanitize_filename(title_for_filename, fallback="document")
        filename = f"{safe_basename}.pdf"

        # 6. Persistance Library (MinIO upload + DB INSERT)
        try:
            library_item = await LibraryService.create_from_bytes(
                user,
                db,
                type_="document",
                file_type="pdf",
                title=body.title or safe_basename,
                data=rendered.pdf_bytes,
                mime_type="application/pdf",
                source="generated",
                provider="weasyprint",
                model=f"template_{body.template}",
                prompt=None,  # Pas de prompt LLM ici, source = message direct
                source_conversation_id=body.conversation_id,
                source_message_id=body.message_id,
                metadata_json={
                    "template": body.template,
                    "pages": rendered.pages,
                    "truncated": rendered.truncated,
                    "format": body.format,
                    "generator_version": "c47a-v1",
                    "options": body.options.model_dump(exclude_none=True),
                },
            )
        except Exception as exc:
            # FileTooLargeException / LibraryQuotaExceededException /
            # ObjectStoreUnavailableException remontent du service Library.
            # On les laisse passer pour que le router les map vers leur
            # propre HTTP code (413 / 402 / 503).
            log.warning(
                "documents.storage_failed",
                error_type=type(exc).__name__,
                user_id=str(user.id),
            )
            # On re-raise tel quel — le router mappera les exceptions
            # connues. Les inconnues seront catchées par le handler
            # global et retourneront 500.
            raise

        # 7. Génération presigned URL TTL 30 min
        try:
            presigned_url = await LibraryService.presigned_url_for(
                library_item,
                ttl_seconds=settings.documents_generator_presigned_ttl_seconds,
            )
        except Exception as exc:
            log.warning(
                "documents.presigned_failed",
                error_type=type(exc).__name__,
                library_id=str(library_item.id),
            )
            raise DocumentStorageUnavailableError(
                "PDF généré mais URL de téléchargement temporairement indisponible."
            ) from exc

        # 8. Construction response
        now = datetime.now(timezone.utc)
        expires_at = datetime.fromtimestamp(
            now.timestamp() + settings.documents_generator_presigned_ttl_seconds,
            tz=timezone.utc,
        )

        log.info(
            "documents.generate.success",
            user_id=str(user.id),
            library_id=str(library_item.id),
            template=body.template,
            pages=rendered.pages,
            size_bytes=rendered.size_bytes,
            truncated=rendered.truncated,
            source_chars=len(markdown_source),
        )

        return DocumentGenerateResponse(
            library_id=library_item.id,
            download_url=presigned_url,
            filename=filename,
            size_bytes=rendered.size_bytes,
            pages=rendered.pages,
            truncated=rendered.truncated,
            expires_at=expires_at,
            generated_at=now,
        )


# Aliases pour test/mock
__all__ = [
    "DocumentGeneratorService",
    "DocumentGenerateRequest",
    "DocumentGenerateResponse",
    "DocumentGenerateOptions",
    "DocumentTemplate",
    "_sanitize_filename",
]
