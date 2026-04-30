"""
Extracteur texte — PDF / DOCX / text plain, pure Python (stdlib + pypdf).

Pourquoi pas `python-docx` ?
- `python-docx` est une dépendance de ~600 KB qui fait bien plus que ce dont
  on a besoin (styles, tableaux, images, formatage complet). Notre besoin
  au C3/E3/D4 : **extraire le texte linéaire** pour indexation RAG.
- DOCX = ZIP Office Open XML. `word/document.xml` contient les `<w:t>`
  (text runs) dans une structure XML documentée et stable depuis 2007.
  Le parse par `xml.etree.ElementTree` (stdlib) est trivial et rapide.
- On évite une dépendance transitive, et la stdlib est hyper-stable.

Design :
- Toutes les fonctions d'extraction sont **synchrones** (CPU-bound).
- Le caller est responsable du `asyncio.to_thread(extract_text, ...)` pour
  ne pas bloquer l'event loop sur un PDF de 100 pages.
- Cap strict sur la taille du texte extrait (`max_chars`, défaut 500k) :
  un PDF scanné géant produirait un texte OCR de plusieurs Mo qui ferait
  gonfler la row DB et consommerait la RAM sans valeur ajoutée.
- Fail-safe : toute exception interne est convertie en status `'failed'`
  avec log — l'upload parent ne doit pas être bloqué par une extraction
  ratée.

Status retournés :
- `'ok'`     : extraction réussie, texte non vide.
- `'empty'`  : extraction réussie mais texte vide (PDF image scan sans OCR).
- `'unsupported'` : mime non reconnu pour extraction.
- `'failed'` : erreur pendant le parse (fichier corrompu, format non
  respecté, bug pypdf).
"""

from __future__ import annotations

import io
import logging
import xml.etree.ElementTree as ET
import zipfile
from dataclasses import dataclass
from typing import Final, Literal

import structlog

log = structlog.get_logger()

ExtractionStatus = Literal["ok", "empty", "unsupported", "failed"]

# ══════════════════════════════════════════════════════════════
# MIME catégorisés pour dispatch
# ══════════════════════════════════════════════════════════════

_PDF_MIME: Final[str] = "application/pdf"
_DOCX_MIME: Final[str] = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
# Plain text family — tous traités pareil (decode bytes).
_PLAIN_TEXT_MIMES: Final[set[str]] = {
    "text/plain",
    "text/markdown",
    "text/x-markdown",
    "text/csv",
}

# Namespace unique pour l'extraction DOCX.
_DOCX_NS: Final[str] = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"

# Cap par défaut : 500k chars ≈ 100 pages A4 de texte dense. Au-delà c'est
# un scan géant sans valeur textuelle, on truncate.
_DEFAULT_MAX_CHARS: Final[int] = 500_000


# ══════════════════════════════════════════════════════════════
# Résultat retourné
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ExtractedText:
    """Résultat d'une extraction.

    - `text` : le texte extrait (concaténé), potentiellement vide.
    - `page_count` : nombre de pages pour les formats paginés (PDF), None sinon.
    - `truncated` : True si le texte a été tronqué au cap `max_chars`.
    - `status` : issue de l'extraction (voir docstring module).
    """

    text: str
    page_count: int | None
    truncated: bool
    status: ExtractionStatus


# ══════════════════════════════════════════════════════════════
# API publique
# ══════════════════════════════════════════════════════════════


def extract_text(
    data: bytes,
    mime_type: str,
    *,
    max_chars: int = _DEFAULT_MAX_CHARS,
    inject_page_markers: bool = False,
) -> ExtractedText:
    """Dispatche sur le bon extracteur selon le mime.

    CPU-bound. À appeler via `await asyncio.to_thread(extract_text, ...)`
    depuis un handler async.

    Paramètre `inject_page_markers` (D4 RAG) :
    - Si `True` et mime == PDF → insère `[[PAGE:N]]` au début du texte de
      chaque page. Consommé par le chunker pour dériver le `page_number`
      de chaque chunk.
    - Sinon → pas de marqueur (comportement E3 par défaut, stocké en DB).
    - Pour DOCX / TXT / MD : l'option n'a pas d'effet (pas de notion de
      page fiable).
    """
    if mime_type == _PDF_MIME:
        return _extract_pdf(
            data,
            max_chars=max_chars,
            inject_page_markers=inject_page_markers,
        )
    if mime_type == _DOCX_MIME:
        return _extract_docx(data, max_chars=max_chars)
    if mime_type in _PLAIN_TEXT_MIMES:
        return _extract_plain(data, max_chars=max_chars)
    return ExtractedText(text="", page_count=None, truncated=False, status="unsupported")


# ══════════════════════════════════════════════════════════════
# PDF — pypdf
# ══════════════════════════════════════════════════════════════


def _extract_pdf(
    data: bytes,
    *,
    max_chars: int,
    inject_page_markers: bool = False,
) -> ExtractedText:
    """Extrait le texte d'un PDF via `pypdf.PdfReader`.

    `page.extract_text()` peut retourner None sur certaines pages (scan
    image sans OCR). On traite ça comme une page vide (contribution 0 au
    texte accumulé). Si *aucune* page n'a de texte → status 'empty'.

    Si `inject_page_markers=True`, insère `[[PAGE:N]]\\n` avant le texte
    de chaque page qui a du contenu. Le chunker D4 utilise ces marqueurs
    pour dériver le `page_number` de chaque chunk.
    """
    # Import local — pypdf a un cold-start de ~50 ms qu'on ne veut pas
    # payer dans l'interpreter startup.
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        log.error("text_extractor.pypdf_not_installed", error=str(exc))
        return ExtractedText(text="", page_count=None, truncated=False, status="failed")

    # pypdf lui-même log des warnings via `logging` stdlib (pas structlog).
    # On désactive son bruit pendant l'extraction — les warnings
    # « CropBox missing », « xref table not zero-indexed » n'intéressent
    # personne et inondent les logs applicatifs.
    pypdf_logger = logging.getLogger("pypdf")
    previous_level = pypdf_logger.level
    pypdf_logger.setLevel(logging.ERROR)

    try:
        reader = PdfReader(io.BytesIO(data))
        pages = reader.pages
        page_count = len(pages)

        parts: list[str] = []
        accumulated = 0
        truncated = False

        for page_index, page in enumerate(pages, start=1):
            try:
                page_text = page.extract_text() or ""
            except Exception as exc:  # noqa: BLE001
                # Une page corrompue ne doit pas faire planter le fichier.
                log.debug("text_extractor.pdf.page_error", error=str(exc))
                continue

            if not page_text:
                continue

            # D4 — injection marqueur `[[PAGE:N]]` pour le chunker.
            if inject_page_markers:
                marker = f"[[PAGE:{page_index}]]\n"
                page_text = marker + page_text

            remaining = max_chars - accumulated
            if remaining <= 0:
                truncated = True
                break

            if len(page_text) > remaining:
                parts.append(page_text[:remaining])
                accumulated += remaining
                truncated = True
                break

            parts.append(page_text)
            accumulated += len(page_text)

        # On insère un double-saut de ligne entre pages pour que le texte
        # garde une structure lisible (et splittable si D4 veut chunker).
        text = "\n\n".join(parts)
        # Clamp final — les séparateurs `\n\n` ajoutés par le join ont pu
        # faire déborder le cap applicatif. On tranche net au max_chars.
        if len(text) > max_chars:
            text = text[:max_chars]
            truncated = True
        status: ExtractionStatus = "ok" if text.strip() else "empty"

        return ExtractedText(
            text=text,
            page_count=page_count,
            truncated=truncated,
            status=status,
        )
    except PdfReadError as exc:
        log.warning("text_extractor.pdf.read_error", error=str(exc))
        return ExtractedText(text="", page_count=None, truncated=False, status="failed")
    except (EOFError, ValueError, OSError) as exc:
        log.warning("text_extractor.pdf.io_error", error=str(exc))
        return ExtractedText(text="", page_count=None, truncated=False, status="failed")
    except Exception as exc:  # noqa: BLE001
        # Garde-fou : pypdf peut lever des exceptions non-documentées sur
        # des PDFs pathologiques. On log et on fail proprement.
        log.warning("text_extractor.pdf.unexpected", error=str(exc), error_type=type(exc).__name__)
        return ExtractedText(text="", page_count=None, truncated=False, status="failed")
    finally:
        pypdf_logger.setLevel(previous_level)


# ══════════════════════════════════════════════════════════════
# DOCX — zipfile + xml.etree (stdlib)
# ══════════════════════════════════════════════════════════════


def _extract_docx(data: bytes, *, max_chars: int) -> ExtractedText:
    """Extrait le texte linéaire d'un .docx.

    Structure : le ZIP contient `word/document.xml` avec un arbre
    `<w:document><w:body>...</w:body></w:document>`. Les texts sont
    dans `<w:t>` (text runs), groupés par `<w:p>` (paragraphes).

    Algo :
    - Parse l'XML.
    - Parcours récursif : pour chaque `<w:p>`, concat les `<w:t>` avec " ".
    - Concat les paragraphes avec "\n".
    - Truncate à max_chars.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            if "word/document.xml" not in zf.namelist():
                log.warning("text_extractor.docx.no_document_xml")
                return ExtractedText(text="", page_count=None, truncated=False, status="failed")
            xml_bytes = zf.read("word/document.xml")
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        log.warning("text_extractor.docx.zip_error", error=str(exc))
        return ExtractedText(text="", page_count=None, truncated=False, status="failed")

    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as exc:
        log.warning("text_extractor.docx.xml_parse_error", error=str(exc))
        return ExtractedText(text="", page_count=None, truncated=False, status="failed")

    paragraphs: list[str] = []
    accumulated = 0
    truncated = False

    # Les éléments intéressants sont `{ns}p` et `{ns}t`.
    ns = _DOCX_NS
    for paragraph in root.iter(f"{{{ns}}}p"):
        runs: list[str] = []
        for t in paragraph.iter(f"{{{ns}}}t"):
            # `t.text` peut être None si l'élément est vide (<w:t/>).
            if t.text:
                runs.append(t.text)
        if not runs:
            continue
        paragraph_text = " ".join(runs)

        remaining = max_chars - accumulated
        if remaining <= 0:
            truncated = True
            break

        if len(paragraph_text) > remaining:
            paragraphs.append(paragraph_text[:remaining])
            accumulated += remaining
            truncated = True
            break

        paragraphs.append(paragraph_text)
        accumulated += len(paragraph_text)

    text = "\n".join(paragraphs)
    # Clamp final — les `\n` entre paragraphes peuvent faire déborder.
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True
    status: ExtractionStatus = "ok" if text.strip() else "empty"

    return ExtractedText(
        text=text,
        page_count=None,  # DOCX n'a pas de notion de page fiable en XML.
        truncated=truncated,
        status=status,
    )


# ══════════════════════════════════════════════════════════════
# Plain text — UTF-8 strict, fallback latin-1
# ══════════════════════════════════════════════════════════════


def _extract_plain(data: bytes, *, max_chars: int) -> ExtractedText:
    """Décoder bytes → str avec UTF-8 strict puis fallback latin-1.

    Latin-1 est le fallback « jamais d'erreur » pour les fichiers Windows
    anciens. Ça peut donner un encodage faux sémantiquement, mais ça ne
    perd pas de bytes — un RAG pourra recalculer l'encodage plus tard.
    """
    try:
        text = data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        # Fallback — jamais d'erreur car latin-1 couvre tout byte < 256.
        text = data.decode("latin-1", errors="replace")

    truncated = False
    if len(text) > max_chars:
        text = text[:max_chars]
        truncated = True

    status: ExtractionStatus = "ok" if text.strip() else "empty"
    return ExtractedText(text=text, page_count=None, truncated=truncated, status=status)
