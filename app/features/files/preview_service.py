"""PreviewService — Génération PDF preview des fichiers uploadés user (C4.10).

Pipeline strict 7 étapes pour `GET /files/{upload_id}/preview` :

    1. get_for_user(upload_id, user) → 404 IDOR-safe
    2. Check MIME ∈ {pdf, docx} → 415 FILE_TYPE_NOT_PREVIEWABLE sinon
    3. Cache MinIO check : object_exists(`previews/{sha256}.pdf`)
       Hit → download_bytes + return (latence ~50ms)
    4. Cache miss : download_bytes(storage_key) original
    5. Si PDF natif → bypass conversion (retourne tel quel + cache pour next)
    6. Si DOCX → mammoth.convert_to_html → weasyprint.write_pdf (timeout 15s)
       → pikepdf cap 50 pages
    7. upload_bytes(`previews/{sha}.pdf`) cache 30j (fire-and-forget)

CONTRAINTE TRANSVERSALE RGPD STRICT (cf. ROADMAP_FUTURE_FEATURES.md ligne 2757) :
**Ne JAMAIS toucher aux fichiers UPLOADÉS par l'user (RGPD + droit d'auteur).**

Le pipeline preview C4.10 :
- ❌ NE PAS appliquer le watermark NEXYA C4.7d
- ❌ NE PAS appliquer le branding NEXYA C4.8 (apply_pdf_native_metadata, apply_docx_*)
- ❌ NE PAS signer C2PA C4.9 (les fichiers user ne sont pas générés par IA)
- ❌ NE PAS modifier les métadonnées XMP de l'original
- ✅ Lire download_bytes original sans modification
- ✅ Pour DOCX : créer un PDF dérivé MINIMAL (rendu basique mammoth+weasyprint)
- ✅ Pour PDF natif : passer tel quel (pas de re-render)

Le branding NEXYA + C2PA + watermark sont RÉSERVÉS aux fichiers GÉNÉRÉS PAR
NEXYA (Document Generator C4.7a-d + C4.8 + C4.9) via `/generate/document`.

Fail-safe multi-niveaux :
- Mammoth crash (DOCX exotique) → fallback texte brut extracted_text (E3)
  → 1 page WeasyPrint minimaliste « Document non rendu visuellement, texte
  extrait : … ». Préserve l'UX (l'user voit AU MOINS le texte).
- WeasyPrint timeout 15s → FilePreviewUnavailableError (snackbar Retry CTA)
- Pikepdf parse error → FilePreviewUnavailableError
- ObjectStore.download_bytes throw FileNotFoundError → FilePreviewUnavailableError
  (orphan original storage rare mais possible)

Architecture cache MinIO :
- Bucket même que les uploads (`settings.s3_bucket_name`, défaut `nexya-media`)
- Préfixe `previews/{content_sha256}.pdf` — pas de sharding (le hash distribue
  déjà uniformément les écritures dans MinIO, sharding inutile sur prefix court)
- Cache hit = retour immédiat ~50ms (ObjectStore HEAD + GET)
- Cache miss = génération + write fire-and-forget (l'user reçoit le PDF
  sans attendre le cache write)
- TTL 30j non géré côté MinIO V1 (pas de lifecycle policy bucket) — V2 via
  bucket lifecycle rules ou cron cleanup manuel. Acceptable V1 : les
  previews orphelins (fichier original soft-deleted) restent en cache jusqu'à
  ce que quelqu'un les efface ou que le bucket soit purgé. Coût stockage
  négligeable (~50 KB/preview × 10k previews = 500 MB).
"""

from __future__ import annotations

import asyncio
import io
import uuid
from dataclasses import dataclass
from typing import Final

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors.exceptions import (
    FilePreviewNotPreviewableException,
    FilePreviewUnavailableException,
)
from app.core.storage import ObjectStore, get_object_store
from app.features.auth.models import User
from app.features.files.service import FileUploadService

log = structlog.get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

_PREVIEW_BUCKET_PREFIX: Final[str] = "previews/"
"""Préfixe MinIO bucket pour les previews — cohabite avec `{user_id}/uploads/`
sans collision possible."""

_PDF_MIME: Final[str] = "application/pdf"
_DOCX_MIME: Final[str] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

_PREVIEWABLE_MIMES: Final[frozenset[str]] = frozenset({_PDF_MIME, _DOCX_MIME})
"""MIMEs supportés pour génération preview V1. XLSX/PPTX/TXT/MD différés V2
(XLSX = openpyxl + structure tableau complexe, PPTX = python-pptx + slides
images, TXT/MD = trop simple pour justifier un preview vs juste afficher
le texte brut dans la carte fichier)."""

_HTML_WRAPPER_TEMPLATE: Final[str] = """<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="utf-8">
    <title>Document</title>
    <style>
        @page {{
            size: A4 portrait;
            margin: 2cm;
        }}
        body {{
            font-family: 'Helvetica', 'Arial', sans-serif;
            font-size: 11pt;
            line-height: 1.6;
            color: #1a1a1a;
        }}
        h1, h2, h3, h4, h5, h6 {{
            font-weight: 600;
            line-height: 1.3;
            margin-top: 1.2em;
            margin-bottom: 0.5em;
            page-break-after: avoid;
        }}
        h1 {{ font-size: 22pt; }}
        h2 {{ font-size: 16pt; }}
        h3 {{ font-size: 13pt; }}
        p {{ margin: 0.6em 0; }}
        ul, ol {{
            margin: 0.6em 0;
            padding-left: 1.5em;
        }}
        li {{ margin: 0.2em 0; }}
        table {{
            border-collapse: collapse;
            width: 100%;
            margin: 0.8em 0;
        }}
        th, td {{
            border: 1px solid #ddd;
            padding: 0.4em 0.6em;
            text-align: left;
        }}
        th {{ background: #f0f0f0; font-weight: 600; }}
    </style>
</head>
<body>
{body_html}
</body>
</html>
"""
"""Wrapper HTML minimaliste pour le rendu mammoth → weasyprint. AUCUN
branding NEXYA (pas de logo, pas de header [NEXYA AI], pas de footer Nexyalabs).
RGPD strict — le PDF preview est un dérivé brut du DOCX user, neutre."""


_FALLBACK_TEXT_HTML_TEMPLATE: Final[str] = """<!DOCTYPE html>
<html lang="fr">
<head>
    <meta charset="utf-8">
    <title>Document</title>
    <style>
        @page {{ size: A4 portrait; margin: 2cm; }}
        body {{
            font-family: 'Helvetica', 'Arial', sans-serif;
            font-size: 11pt;
            line-height: 1.6;
            color: #1a1a1a;
        }}
        .notice {{
            font-style: italic;
            color: #888;
            font-size: 10pt;
            border-bottom: 1px solid #e0e0e0;
            padding-bottom: 0.8em;
            margin-bottom: 1.2em;
        }}
        .extracted {{
            white-space: pre-wrap;
            word-break: break-word;
            font-family: 'Helvetica', 'Arial', sans-serif;
        }}
    </style>
</head>
<body>
    <div class="notice">{notice}</div>
    <div class="extracted">{text}</div>
</body>
</html>
"""
"""Wrapper HTML fallback texte brut — utilisé quand mammoth crash sur DOCX
exotique. L'user voit AU MOINS le texte extracted_text (E3) au lieu d'une
erreur 503."""


# ══════════════════════════════════════════════════════════════
# Result dataclass
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class PreviewResult:
    """Résultat d'une génération preview.

    Attributes:
        pdf_bytes: Contenu PDF binaire prêt à streamer côté HTTP.
        from_cache: True si récupéré du cache MinIO, False si généré.
        truncated: True si le DOCX a été tronqué au cap pages.
        size_bytes: len(pdf_bytes) — pour log + header Content-Length.
    """

    pdf_bytes: bytes
    from_cache: bool
    truncated: bool
    size_bytes: int


# ══════════════════════════════════════════════════════════════
# PreviewService — méthodes statiques pattern NEXYA
# ══════════════════════════════════════════════════════════════


class PreviewService:
    """Orchestrateur de génération preview pour fichiers uploadés user.

    Pattern strict aligné `FileUploadService` E3 + `DocumentGeneratorService`
    C4.7a : méthodes statiques, dépendances injectables (store), fail-safe
    multi-niveaux, kill-switch settings.
    """

    @staticmethod
    def _preview_key(content_sha256: str) -> str:
        """Clé MinIO canonique pour un preview cached.

        Format : `previews/{sha256}.pdf` — pas de sharding (hash distribue
        déjà uniformément). Cohabite avec `{user_id}/uploads/{shard}/{sha}.{ext}`
        sans collision.
        """
        return f"{_PREVIEW_BUCKET_PREFIX}{content_sha256}.pdf"

    @staticmethod
    async def get_cached_or_generate(
        upload_id: uuid.UUID,
        user: User,
        db: AsyncSession,
        *,
        store: ObjectStore | None = None,
    ) -> PreviewResult:
        """Orchestre cache-first → MinIO check → générer si miss → cache async.

        Args:
            upload_id: UUID de l'UploadedFile cible.
            user: User courant (auth via JWT).
            db: AsyncSession SQLAlchemy.
            store: ObjectStore injectable (tests). None → singleton factory.

        Returns:
            PreviewResult avec pdf_bytes + from_cache + truncated + size.

        Raises:
            ResourceNotFoundException: 404 IDOR-safe via FileUploadService.get_for_user.
            FilePreviewNotPreviewableException: 415 si MIME hors {pdf, docx}.
            FilePreviewUnavailableException: 503 si pipeline crash + fallback KO.
        """
        # Étape 1. Owner check 404 IDOR-safe (réutilise pattern E3).
        upload = await FileUploadService.get_for_user(upload_id, user, db)

        # Étape 2. Check MIME previewable.
        mime = (upload.mime_type or "").lower()
        if mime not in _PREVIEWABLE_MIMES:
            log.info(
                "files.preview.not_previewable",
                upload_id=str(upload_id),
                mime=mime,
                user_id=str(user.id),
            )
            raise FilePreviewNotPreviewableException(mime_type=mime)

        store = store if store is not None else get_object_store()
        cache_key = PreviewService._preview_key(upload.content_sha256)

        # Étape 3. Cache MinIO check.
        try:
            cached_exists = await store.object_exists(cache_key)
        except Exception as exc:  # noqa: BLE001 — fail-safe absolu
            log.warning(
                "files.preview.cache_check_failed",
                upload_id=str(upload_id),
                cache_key=cache_key,
                error=str(exc),
            )
            cached_exists = False

        if cached_exists:
            try:
                cached_bytes = await store.download_bytes(cache_key)
                log.info(
                    "files.preview.cache_hit",
                    upload_id=str(upload_id),
                    cache_key=cache_key,
                    size_bytes=len(cached_bytes),
                    user_id=str(user.id),
                )
                return PreviewResult(
                    pdf_bytes=cached_bytes,
                    from_cache=True,
                    truncated=False,  # info perdue après cache, V2 si besoin
                    size_bytes=len(cached_bytes),
                )
            except (FileNotFoundError, Exception) as exc:  # noqa: BLE001
                # Cache présent mais download fail (race condition delete
                # par autre process, ou MinIO transitoire) → fall-through
                # vers génération.
                log.warning(
                    "files.preview.cache_download_failed",
                    upload_id=str(upload_id),
                    cache_key=cache_key,
                    error=str(exc),
                )

        # Étape 4. Cache miss : download_bytes original.
        try:
            original_bytes = await store.download_bytes(upload.storage_key)
        except FileNotFoundError as exc:
            log.warning(
                "files.preview.original_not_found",
                upload_id=str(upload_id),
                storage_key=upload.storage_key,
            )
            raise FilePreviewUnavailableException(reason="original_storage_missing") from exc
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "files.preview.original_download_failed",
                upload_id=str(upload_id),
                error=str(exc),
            )
            raise FilePreviewUnavailableException(reason="storage_download_failed") from exc

        # Étape 5 + 6 : génération selon MIME.
        result_pdf_bytes: bytes
        truncated = False
        if mime == _PDF_MIME:
            # PDF natif : passthrough strict (PAS de re-render — RGPD,
            # on ne touche pas aux métadonnées XMP de l'original).
            result_pdf_bytes = original_bytes
        else:  # mime == _DOCX_MIME
            try:
                result_pdf_bytes, truncated = await PreviewService._render_docx_to_pdf(
                    original_bytes,
                    extracted_text_fallback=upload.extracted_text,
                )
            except FilePreviewUnavailableException:
                raise  # déjà typée, propager
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "files.preview.docx_render_unexpected_error",
                    upload_id=str(upload_id),
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
                raise FilePreviewUnavailableException(reason="docx_render_failed") from exc

        # Étape 7. Cache write fire-and-forget (l'user reçoit le PDF sans
        # attendre le cache write). Si le cache write rate, ce n'est pas
        # bloquant — le prochain GET regénérera (idempotent).
        asyncio.create_task(
            PreviewService._cache_preview_async(
                store=store,
                cache_key=cache_key,
                pdf_bytes=result_pdf_bytes,
                original_sha=upload.content_sha256,
            )
        )

        log.info(
            "files.preview.generated",
            upload_id=str(upload_id),
            mime=mime,
            size_bytes=len(result_pdf_bytes),
            truncated=truncated,
            user_id=str(user.id),
        )

        return PreviewResult(
            pdf_bytes=result_pdf_bytes,
            from_cache=False,
            truncated=truncated,
            size_bytes=len(result_pdf_bytes),
        )

    @staticmethod
    async def _cache_preview_async(
        *,
        store: ObjectStore,
        cache_key: str,
        pdf_bytes: bytes,
        original_sha: str,
    ) -> None:
        """Upload async fire-and-forget du preview généré dans le cache MinIO.

        Fail-safe absolu : toute exception swallow (log warning seulement).
        Le user a déjà reçu son PDF, le cache rate juste pour les requêtes
        suivantes qui regénéreront (idempotent).
        """
        try:
            await store.upload_bytes(
                cache_key,
                pdf_bytes,
                mime_type=_PDF_MIME,
                metadata={
                    "original_sha": original_sha,
                    "generator": "c410-preview-v1",
                },
            )
            log.debug(
                "files.preview.cached",
                cache_key=cache_key,
                size_bytes=len(pdf_bytes),
            )
        except Exception as exc:  # noqa: BLE001 — fail-safe absolu
            log.warning(
                "files.preview.cache_write_failed",
                cache_key=cache_key,
                error=str(exc),
            )

    @staticmethod
    async def _render_docx_to_pdf(
        docx_bytes: bytes,
        *,
        extracted_text_fallback: str | None = None,
    ) -> tuple[bytes, bool]:
        """Pipeline DOCX → HTML (mammoth) → PDF (weasyprint) → cap pages (pikepdf).

        AUCUN branding NEXYA appliqué (RGPD strict).

        Args:
            docx_bytes: Contenu DOCX brut.
            extracted_text_fallback: Texte extracted_text E3 utilisé en
                fallback si mammoth crash sur DOCX exotique.

        Returns:
            Tuple (pdf_bytes, truncated).

        Raises:
            FilePreviewUnavailableException: Si mammoth + fallback texte
                échouent tous les 2.
        """
        # Conversion DOCX → HTML via mammoth (pure Python, ~50 KB dep).
        try:
            html_body = await asyncio.to_thread(PreviewService._mammoth_convert_sync, docx_bytes)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "files.preview.mammoth_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # Fallback : utiliser extracted_text si dispo, sinon 503.
            if extracted_text_fallback and extracted_text_fallback.strip():
                log.info("files.preview.fallback_to_extracted_text")
                return await PreviewService._render_text_fallback_to_pdf(extracted_text_fallback)
            raise FilePreviewUnavailableException(reason="docx_mammoth_failed_no_fallback") from exc

        # Wrapper HTML minimaliste (PAS de branding NEXYA — RGPD).
        full_html = _HTML_WRAPPER_TEMPLATE.format(body_html=html_body)

        # Rendu PDF via weasyprint avec timeout strict.
        try:
            raw_pdf = await asyncio.wait_for(
                asyncio.to_thread(PreviewService._weasyprint_render_sync, full_html),
                timeout=settings.documents_generator_preview_timeout_seconds,
            )
        except TimeoutError as exc:
            log.warning(
                "files.preview.weasyprint_timeout",
                timeout=settings.documents_generator_preview_timeout_seconds,
            )
            raise FilePreviewUnavailableException(reason="weasyprint_timeout") from exc
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "files.preview.weasyprint_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise FilePreviewUnavailableException(reason="weasyprint_failed") from exc

        # Cap pages via pikepdf (anti-DOCX géant). AUCUN branding NEXYA
        # appliqué — on utilise UNIQUEMENT pikepdf pour la troncature
        # pages (pas apply_pdf_native_metadata C4.8 qui poserait dc:creator
        # NEXYA et casserait la contrainte RGPD).
        try:
            capped_pdf, truncated = await asyncio.to_thread(
                PreviewService._cap_pages_sync,
                raw_pdf,
                settings.documents_generator_preview_max_pages,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "files.preview.pikepdf_cap_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # Si pikepdf échoue (rare, weasyprint produit du PDF valide),
            # on retourne le PDF brut sans truncation plutôt que de fail.
            return raw_pdf, False

        return capped_pdf, truncated

    @staticmethod
    def _mammoth_convert_sync(docx_bytes: bytes) -> str:
        """Conversion DOCX → HTML synchrone (CPU-bound). Appelé via asyncio.to_thread.

        Returns:
            HTML string (body uniquement, sans <html><head>...).

        Raises:
            Exception: Toute erreur mammoth (DOCX corrompu, structure XML
                inattendue). Catch côté caller pour fallback texte.
        """
        import mammoth

        result = mammoth.convert_to_html(io.BytesIO(docx_bytes))
        # mammoth retourne ConvertResult avec .value (HTML) + .messages (warnings).
        # On ignore les warnings (la majorité sont des styles non supportés).
        return result.value or ""

    @staticmethod
    def _weasyprint_render_sync(html_content: str) -> bytes:
        """Rendu HTML → PDF synchrone (CPU-bound). Appelé via asyncio.to_thread.

        AUCUN branding NEXYA (pas de @top-left, pas de @bottom-center, pas
        de pikepdf metadata, pas de C2PA). Strict pour RGPD.
        """
        from weasyprint import HTML

        html_obj = HTML(
            string=html_content,
            base_url="",  # Pas de relative paths
        )
        return html_obj.write_pdf()

    @staticmethod
    def _cap_pages_sync(pdf_bytes: bytes, max_pages: int) -> tuple[bytes, bool]:
        """Cap pages via pikepdf — supprime les pages excédentaires.

        AUCUN branding metadata appliqué — uniquement troncature pages.
        """
        import pikepdf

        output = io.BytesIO()
        truncated = False

        with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
            total_pages = len(pdf.pages)
            if total_pages > max_pages:
                del pdf.pages[max_pages:]
                truncated = True

            # Sanitize metadata POST-render — retire les infos provider
            # (weasyprint version, etc.) sans poser d'identifiants NEXYA.
            # Strict : on garde le PDF "anonyme" côté metadata.
            with pdf.open_metadata() as meta:
                meta.load_from_docinfo(pdf.docinfo)

            pdf.save(
                output,
                compress_streams=True,
                object_stream_mode=pikepdf.ObjectStreamMode.generate,
                stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
            )

        return output.getvalue(), truncated

    @staticmethod
    async def _render_text_fallback_to_pdf(text: str) -> tuple[bytes, bool]:
        """Fallback PDF minimaliste avec texte brut extracted_text.

        Utilisé quand mammoth crash sur DOCX exotique. L'user voit AU MOINS
        le texte au lieu d'une erreur 503.

        Args:
            text: Contenu texte brut (extracted_text E3).

        Returns:
            Tuple (pdf_bytes, truncated=False).

        Raises:
            FilePreviewUnavailableException: Si même WeasyPrint texte simple
                échoue (double-échec extrêmement rare).
        """
        # Échappement HTML basique pour éviter injection (le texte vient de
        # extracted_text qui est déjà sanitisé par pypdf/python-docx, mais
        # défense en profondeur).
        safe_text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        # Cap 50 000 chars pour éviter PDF géant en cas d'extracted_text
        # pathologique (le cap E3 est à 500k, mais 500k chars en PDF
        # texte brut = ~150 pages, trop pour un preview).
        if len(safe_text) > 50_000:
            safe_text = safe_text[:50_000] + "\n\n[... texte tronqué pour le preview ...]"

        notice = "Aperçu visuel non disponible — voici le texte extrait du document."
        full_html = _FALLBACK_TEXT_HTML_TEMPLATE.format(notice=notice, text=safe_text)

        try:
            raw_pdf = await asyncio.wait_for(
                asyncio.to_thread(PreviewService._weasyprint_render_sync, full_html),
                timeout=settings.documents_generator_preview_timeout_seconds,
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "files.preview.fallback_text_render_failed",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise FilePreviewUnavailableException(reason="fallback_text_render_failed") from exc

        # Cap pages (le texte peut être long).
        try:
            capped_pdf, truncated = await asyncio.to_thread(
                PreviewService._cap_pages_sync,
                raw_pdf,
                settings.documents_generator_preview_max_pages,
            )
            return capped_pdf, truncated
        except Exception:  # noqa: BLE001
            # Si pikepdf échoue sur le PDF texte fallback, retourner brut.
            return raw_pdf, False


# ══════════════════════════════════════════════════════════════
# Public API
# ══════════════════════════════════════════════════════════════

__all__ = [
    "PreviewResult",
    "PreviewService",
]
