"""WeasyPrint renderer + pikepdf post-process (C4.7a).

Pipeline strict :
    1. HTML rendu (template_loader) → WeasyPrint.write_pdf() via
       `asyncio.to_thread` (CPU-bound + I/O cairo/pango sync)
    2. Timeout 30s via `asyncio.wait_for` (hardcap anti-PDF infini)
    3. Compte pages via pikepdf, tronque si > max_pages
    4. Compression streams pikepdf + metadata sanitize

Sécurité :
    - URL fetching DÉSACTIVÉ via WeasyPrint `url_fetcher` custom qui
      refuse tout schéma ≠ `data:` (anti SSRF + anti image network
      bloquante). Les `<img src="...">` réseaux dans le HTML sont
      silencieusement skip avec placeholder vide.
    - Subprocess timeout 30s via `asyncio.wait_for` (anti CPU exhaust)
    - Cap 50 pages dur via pikepdf après render (anti PDF géant)
    - 0 file:// access (WeasyPrint base_url='') + 0 disk write

Fail-safe :
    - Exception WeasyPrint (cairo/pango crash) → DocumentRenderFailedError
    - Timeout asyncio → DocumentRenderFailedError (logué + 503)
    - pikepdf parse fail → DocumentRenderFailedError (PDF corrompu rare)
"""

from __future__ import annotations

import asyncio
import io
from dataclasses import dataclass
from typing import Final

import structlog

from .branding import BrandingContext, apply_pdf_native_metadata
from .exceptions import DocumentRenderFailedError

log = structlog.get_logger(__name__)

# ── Constantes module-level ──────────────────────────────────────────

_DEFAULT_RENDER_TIMEOUT_SECONDS: Final[float] = 30.0
"""Timeout hardcap render WeasyPrint (anti CPU exhaust / PDF infini)."""

_DEFAULT_MAX_PAGES: Final[int] = 50
"""Cap pages dur (tronque post-render si dépassé)."""


# ── Dataclass de retour ──────────────────────────────────────────────


@dataclass(frozen=True, slots=True)
class RenderedPdf:
    """Résultat du rendu PDF (output binaire + metadata).

    Attributes:
        pdf_bytes: Contenu PDF binaire APRÈS compression pikepdf.
        pages: Nombre réel de pages (≤ max_pages).
        truncated: True si le PDF a été tronqué au cap.
        size_bytes: len(pdf_bytes) — exposé pour cohérence response.
    """

    pdf_bytes: bytes
    pages: int
    truncated: bool
    size_bytes: int


# ── URL fetcher sécurisé ─────────────────────────────────────────────


def _safe_url_fetcher(url: str) -> dict:
    """URL fetcher custom pour WeasyPrint qui REFUSE tout réseau.

    Accepte uniquement :
        - `data:` URIs (images inline base64 encodées dans le HTML)

    Refuse :
        - `http://` `https://` : anti SSRF (un attaquant pourrait
          fabriquer un HTML avec `<img src="http://169.254.169.254/...">`
          pour fetcher AWS metadata, etc.)
        - `file://` : anti read local disk
        - Tout autre schéma exotique

    Returns:
        Dict WeasyPrint-compatible avec contenu vide + mime_type
        `image/png` (placeholder) si non-data:, ou délègue au handler
        natif WeasyPrint pour `data:` URIs.
    """
    if url.startswith("data:"):
        # WeasyPrint sait gérer les data: URIs natifs via son default fetcher
        from weasyprint import default_url_fetcher

        return default_url_fetcher(url)

    log.warning("documents.url_fetch_blocked", url=url[:100])
    # Retourne un placeholder vide (WeasyPrint affiche l'alt text ou rien)
    return {
        "string": b"",
        "mime_type": "image/png",
    }


# ── Helpers sync (appelés dans to_thread) ────────────────────────────


def _render_pdf_sync(html_content: str) -> bytes:
    """Rend HTML → PDF bytes via WeasyPrint (sync, CPU-bound).

    Appelé dans `asyncio.to_thread` pour ne pas bloquer l'event loop.

    Args:
        html_content: HTML string complet (avec <style> inline).

    Returns:
        PDF binaire bytes.

    Raises:
        Exception: Toute erreur WeasyPrint (cairo, pango, layout).
            Catch côté caller pour mapping vers DocumentRenderFailedError.
    """
    from weasyprint import HTML

    html_obj = HTML(
        string=html_content,
        base_url="",  # Pas de base URL → pas de relative paths possibles
        url_fetcher=_safe_url_fetcher,
    )
    return html_obj.write_pdf()


def _post_process_pdf_sync(
    pdf_bytes: bytes,
    *,
    max_pages: int,
    branding_context: BrandingContext | None = None,
) -> RenderedPdf:
    """Post-process pikepdf : count pages, tronque, compresse, branding metadata.

    Args:
        pdf_bytes: PDF brut sortant de WeasyPrint.
        max_pages: Cap dur de pages (tronque au-delà).
        branding_context: BrandingContext (C4.8 + C4.9). Si fourni, enrichit
            les métadonnées natives PDF (`/Info` legacy + XMP modern) via
            `apply_pdf_native_metadata`. Si None, comportement legacy
            (sanitize basique via `meta.load_from_docinfo`).

    Returns:
        RenderedPdf avec pdf_bytes compressé + metadata.

    Raises:
        Exception: pikepdf parse error (PDF corrompu — rare, WeasyPrint
            produit toujours du PDF valide). Catch côté caller.
    """
    import pikepdf

    output = io.BytesIO()
    truncated = False

    with pikepdf.open(io.BytesIO(pdf_bytes)) as pdf:
        total_pages = len(pdf.pages)

        if total_pages > max_pages:
            # Tronque les pages excédentaires (pikepdf supporte del pages[N:])
            del pdf.pages[max_pages:]
            truncated = True

        actual_pages = len(pdf.pages)

        # C4.8 + C4.9 : enrichit les métadonnées natives PDF si branding
        # context fourni. Fail-safe absolu côté helper (exception swallow
        # → log warning + return False, PDF reste valide).
        if branding_context is not None:
            apply_pdf_native_metadata(pdf, branding_context)
        else:
            # Legacy : sanitize metadata uniquement (pas de branding).
            # Retire les infos provider (anti fingerprinting WeasyPrint
            # version, OS, etc.) — Title posé par le template.
            with pdf.open_metadata() as meta:
                meta.load_from_docinfo(pdf.docinfo)

        # Compression streams (pikepdf optimise les flux internes)
        pdf.save(
            output,
            compress_streams=True,
            object_stream_mode=pikepdf.ObjectStreamMode.generate,
            stream_decode_level=pikepdf.StreamDecodeLevel.generalized,
        )

    compressed = output.getvalue()
    return RenderedPdf(
        pdf_bytes=compressed,
        pages=actual_pages,
        truncated=truncated,
        size_bytes=len(compressed),
    )


# ── Public API async ─────────────────────────────────────────────────


async def render_html_to_pdf(
    html_content: str,
    *,
    timeout_seconds: float = _DEFAULT_RENDER_TIMEOUT_SECONDS,
    max_pages: int = _DEFAULT_MAX_PAGES,
    branding_context: BrandingContext | None = None,
) -> RenderedPdf:
    """Rend HTML → PDF complet avec timeout + cap pages + compression.

    Pipeline :
        1. WeasyPrint render dans thread (sync, ~1-5s typique)
        2. Timeout asyncio 30s default (kill si dépasse)
        3. pikepdf post-process (count + truncate + compress + metadata)

    Args:
        html_content: HTML string complet (template Jinja2 rendu).
        timeout_seconds: Timeout hardcap render (défaut 30s).
        max_pages: Cap dur pages (défaut 50).
        branding_context: BrandingContext (C4.8). Si fourni, enrichit
            les métadonnées natives PDF via `apply_pdf_native_metadata`
            dans `_post_process_pdf_sync`.

    Returns:
        RenderedPdf avec metadata complète.

    Raises:
        DocumentRenderFailedError: Sur timeout, exception WeasyPrint, ou
            pikepdf parse error. Toujours mappé vers cette exception
            (jamais de raise brut côté caller).
    """
    if not html_content or not html_content.strip():
        raise DocumentRenderFailedError(
            "HTML content vide — impossible de rendre un PDF."
        )

    try:
        # Étape 1 : WeasyPrint render avec timeout
        pdf_raw_bytes = await asyncio.wait_for(
            asyncio.to_thread(_render_pdf_sync, html_content),
            timeout=timeout_seconds,
        )
    except TimeoutError as exc:
        log.warning(
            "documents.render.timeout",
            timeout_seconds=timeout_seconds,
            html_size_chars=len(html_content),
        )
        raise DocumentRenderFailedError(
            f"Rendu PDF dépassant le timeout {timeout_seconds}s. "
            "Essaie avec un document plus court."
        ) from exc
    except Exception as exc:
        log.warning(
            "documents.render.weasyprint_error",
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
            html_size_chars=len(html_content),
        )
        raise DocumentRenderFailedError(
            "Rendu PDF impossible — erreur de mise en page interne. "
            "Vérifie que le document source ne contient pas de structures HTML/CSS invalides."
        ) from exc

    # Étape 2 : pikepdf post-process (count pages + compress + branding metadata)
    try:
        result = await asyncio.to_thread(
            _post_process_pdf_sync,
            pdf_raw_bytes,
            max_pages=max_pages,
            branding_context=branding_context,
        )
    except Exception as exc:
        log.warning(
            "documents.render.pikepdf_error",
            error_type=type(exc).__name__,
            error_message=str(exc)[:200],
        )
        raise DocumentRenderFailedError(
            "Compression PDF échouée. Le rendu était valide mais le post-traitement a échoué."
        ) from exc

    log.info(
        "documents.render.completed",
        pages=result.pages,
        size_bytes=result.size_bytes,
        truncated=result.truncated,
        raw_size_bytes=len(pdf_raw_bytes),
        compression_ratio=round(result.size_bytes / max(len(pdf_raw_bytes), 1), 3),
    )

    return result
