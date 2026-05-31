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
from app.core.errors.exceptions import PlanRequiredException, ResourceNotFoundException
from app.features.auth.models import User
from app.features.chat.models import Conversation, Message
from app.features.images.c2pa import (
    C2PAError,
    C2PASignRequest,
    C2PASignResult,
    get_manifest_provider,
)
from app.features.library.service import LibraryService

from .branding import (
    BRANDING_VERSION,
    build_branding_context,
    generate_intelligent_filename,
)
from .docx_renderer import render_markdown_to_docx
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
from .watermark_assets import WATERMARK_VERSION
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
        # 0. C4.7d Gate Pro AVANT render (économie WeasyPrint+pikepdf si Free
        # tente remove_watermark=True). Pattern strict aligné `/image/generate`
        # E4 — paywall pre-flight pour ne JAMAIS facturer une feature Pro à un
        # Free qui n'a pas le plan.
        if body.remove_watermark and not user.is_pro:
            log.info(
                "documents.remove_watermark.plan_required",
                user_id=str(user.id),
                template=body.template,
                format=body.format,
            )
            raise PlanRequiredException(feature="Document sans watermark")

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

        # C4.7d : calcul de l'intent watermark (kill-switch backend ET
        # remove_watermark user). True = on tente l'application (le succès
        # final dépend de la dispo de l'asset PNG, fail-safe absolu côté
        # renderer si OOM/Pillow crash).
        apply_watermark = (
            settings.documents_generator_watermark_enabled
            and not body.remove_watermark
        )

        # C4.8 + C4.9 : construit le BrandingContext UNE FOIS pour toute
        # la requête (cohérence cross-canal : PDF info, XMP, DOCX core_props,
        # HTML marker, filename portent tous la même date/template).
        # Kill-switch global → branding_context=None → tous les helpers
        # skip silencieusement (fail-safe absolu via Jinja2 `{% if %}` +
        # checks Python None dans renderers).
        branding_context = None
        if settings.documents_generator_branding_enabled:
            branding_context = build_branding_context(
                template=body.template,
                title=body.title,
                locale="fr",  # V1 FR-only ; V2 lira `user.preferences.locale`
            )

        # 3. Rendu format-spécifique (dispatch PDF vs DOCX)
        if body.format == "pdf":
            # PDF pipeline (C4.7a + C4.7d watermark + C4.8 branding) :
            # HTML Jinja2 + @page background-image base64 + @page top-left
            # header + @page bottom-center footer + invisible marker
            # → WeasyPrint → pikepdf (compress + native metadata XMP).
            html_content = render_document_html(
                template_name=body.template,
                title=body.title,
                markdown_source=markdown_source,
                options=body.options,
                apply_watermark=apply_watermark,
                branding_context=branding_context,
            )
            rendered_pdf = await render_html_to_pdf(
                html_content,
                timeout_seconds=settings.documents_generator_render_timeout_seconds,
                max_pages=settings.documents_generator_max_pages,
                branding_context=branding_context,
            )
            output_bytes = rendered_pdf.pdf_bytes
            output_pages = rendered_pdf.pages
            output_truncated = rendered_pdf.truncated
            output_size = rendered_pdf.size_bytes
            file_extension = "pdf"
            mime_type = "application/pdf"
            provider_name = "weasyprint"
            file_type_for_library: str = "pdf"
            # C4.7d : pour le PDF, watermark_applied est dérivé directement
            # de l'intent + dispo asset (le template Jinja2 skip silencieusement
            # via `{% if watermark_data_url %}`). On pré-calcule ici via
            # `get_watermark_data_url()` qui est cached singleton.
            from .watermark_assets import get_watermark_data_url

            watermark_applied = apply_watermark and (
                get_watermark_data_url() is not None
            )
        else:  # body.format == "docx"
            # DOCX pipeline (C4.7b + C4.7d watermark footer + C4.8 + C4.9
            # branding) : markdown-it AST → python-docx natif + footer
            # logo + texte « Généré par NEXYA AI » via _apply_docx_watermark_footer
            # + header [NEXYA AI] + footer page counter PAGE/NUMPAGES +
            # core_properties + marker invisible (fail-safe absolu sur tout).
            rendered_docx = await render_markdown_to_docx(
                template_name=body.template,
                title=body.title,
                markdown_source=markdown_source,
                options=body.options,
                timeout_seconds=settings.documents_generator_render_timeout_seconds,
                max_pages=settings.documents_generator_max_pages,
                apply_watermark=apply_watermark,
                branding_context=branding_context,
            )
            output_bytes = rendered_docx.docx_bytes
            output_pages = rendered_docx.pages
            output_truncated = rendered_docx.truncated
            output_size = rendered_docx.size_bytes
            file_extension = "docx"
            mime_type = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            provider_name = "python-docx"
            file_type_for_library = "docx"
            # C4.7d : flag remonté par le renderer (True si footer appliqué OK,
            # False si fail-safe Pillow/OOM ou asset PNG introuvable).
            watermark_applied = rendered_docx.watermark_applied

        # 3.5 C4.7d — Signature C2PA AI Act (PDF uniquement V1).
        # DOCX différé V2 — c2pa-rs ne supporte pas OOXML natif, skip
        # silencieux avec `c2pa_skip_reason='unsupported_format_docx'`.
        # Fail-safe absolu : exception lib c2pa-python / clés X.509 invalides
        # → c2pa_applied=False + skip_reason='sign_error', le document
        # est retourné au user SANS signature (jamais bloquer).
        c2pa_applied = False
        c2pa_manifest_id: str | None = None
        c2pa_signed_at: datetime | None = None
        c2pa_skip_reason: str | None = None

        if body.format == "docx":
            c2pa_skip_reason = "unsupported_format_docx"
            log.debug(
                "documents.c2pa.skipped_docx",
                reason=c2pa_skip_reason,
                user_id=str(user.id),
            )
        else:
            try:
                manifest_provider = get_manifest_provider()
                c2pa_request = C2PASignRequest(
                    prompt=f"NEXYA document template={body.template}",
                    provider=provider_name,
                    model=f"template_{body.template}",
                    generation_timestamp=datetime.now(timezone.utc),
                    watermark_applied=watermark_applied,
                    watermark_version=(
                        WATERMARK_VERSION if watermark_applied else None
                    ),
                )
                c2pa_result: C2PASignResult = await manifest_provider.sign_image(
                    image_bytes=output_bytes,
                    mime_type=mime_type,
                    request=c2pa_request,
                )
                if c2pa_result.applied:
                    # Remplace output_bytes par la version signée (manifest
                    # C2PA embarqué dans les métadonnées XMP du PDF).
                    output_bytes = c2pa_result.image_bytes
                    output_size = len(output_bytes)
                    c2pa_applied = True
                    c2pa_manifest_id = c2pa_result.manifest_id
                    c2pa_signed_at = c2pa_result.signed_at
                else:
                    c2pa_skip_reason = c2pa_result.skip_reason or "unknown"
                    log.info(
                        "documents.c2pa.skip_provider",
                        reason=c2pa_skip_reason,
                        user_id=str(user.id),
                    )
            except C2PAError as exc:
                c2pa_skip_reason = "sign_error"
                log.warning(
                    "documents.c2pa.sign_failed_typed",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    user_id=str(user.id),
                )
            except Exception as exc:  # noqa: BLE001 — fail-safe absolu
                c2pa_skip_reason = "sign_error"
                log.warning(
                    "documents.c2pa.sign_failed_unexpected",
                    error=str(exc),
                    error_type=type(exc).__name__,
                    user_id=str(user.id),
                )

        # 4. Génération filename — C4.8 utilise le filename intelligent
        # `nexya_<template>_<title-slug>_<YYYY-MM-DD>.<ext>` quand
        # branding actif, sinon fallback legacy C4.7a.
        # `safe_basename` reste calculé dans les 2 branches car utilisé
        # comme fallback pour `library_item.title` plus bas.
        title_for_filename = body.title or (
            f"document_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"
        )
        safe_basename = _sanitize_filename(
            title_for_filename, fallback="document"
        )
        if branding_context is not None:
            filename = generate_intelligent_filename(
                branding_context, extension=file_extension
            )
        else:
            filename = f"{safe_basename}.{file_extension}"

        # 5. Persistance Library (MinIO upload + DB INSERT)
        try:
            library_item = await LibraryService.create_from_bytes(
                user,
                db,
                type_="document",
                file_type=file_type_for_library,
                title=body.title or safe_basename,
                data=output_bytes,
                mime_type=mime_type,
                source="generated",
                provider=provider_name,
                model=f"template_{body.template}",
                prompt=None,  # Pas de prompt LLM ici, source = message direct
                source_conversation_id=body.conversation_id,
                source_message_id=body.message_id,
                metadata_json={
                    "template": body.template,
                    "pages": output_pages,
                    "truncated": output_truncated,
                    "format": body.format,
                    "generator_version": "c48-v1",  # bumped from c47d/c47b/c47a
                    "options": body.options.model_dump(exclude_none=True),
                    # C4.7d — watermark tracking
                    "has_watermark": watermark_applied,
                    "watermark_version": (
                        WATERMARK_VERSION if watermark_applied else None
                    ),
                    # Tracé pour future facturation différentielle wallet V2
                    # (pattern aligné E4 image — Pro qui retire le watermark
                    # paiera +50% via wallet v2 selon `no_watermark_price_multiplier`).
                    "no_watermark_was_requested": bool(
                        body.remove_watermark and user.is_pro
                    ),
                    # C4.7d — C2PA AI Act tracking
                    "has_c2pa": c2pa_applied,
                    "c2pa_manifest_id": c2pa_manifest_id,
                    "c2pa_signed_at": (
                        c2pa_signed_at.isoformat() if c2pa_signed_at else None
                    ),
                    "c2pa_skip_reason": c2pa_skip_reason,
                    # C4.8 + C4.9 — Branding NEXYA tracking
                    "branding_version": (
                        BRANDING_VERSION if branding_context is not None else None
                    ),
                    "has_branding": branding_context is not None,
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

        # 6. Génération presigned URL TTL 30 min
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
                "Document généré mais URL de téléchargement temporairement indisponible."
            ) from exc

        # 7. Construction response
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
            format=body.format,
            pages=output_pages,
            size_bytes=output_size,
            truncated=output_truncated,
            source_chars=len(markdown_source),
            # C4.7d watermark + C2PA forensic logging
            watermark_applied=watermark_applied,
            c2pa_applied=c2pa_applied,
            c2pa_skip_reason=c2pa_skip_reason,
            remove_watermark_requested=body.remove_watermark,
            # C4.8 branding forensic logging
            branding_applied=branding_context is not None,
            branding_version=(
                BRANDING_VERSION if branding_context is not None else None
            ),
            filename=filename,
        )

        return DocumentGenerateResponse(
            library_id=library_item.id,
            download_url=presigned_url,
            filename=filename,
            size_bytes=output_size,
            pages=output_pages,
            truncated=output_truncated,
            expires_at=expires_at,
            generated_at=now,
            # C4.7d — Watermark + C2PA enrichissement réponse client
            watermark_applied=watermark_applied,
            watermark_version=(
                WATERMARK_VERSION if watermark_applied else None
            ),
            c2pa_applied=c2pa_applied,
            c2pa_manifest_id=c2pa_manifest_id,
            c2pa_skip_reason=c2pa_skip_reason,
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
