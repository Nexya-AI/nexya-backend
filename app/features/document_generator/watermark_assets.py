"""Singleton chargement du logo NEXYA pour watermark documents (C4.7d).

Charge le PNG `app/static/nexya_watermark.png` une seule fois au premier
appel (process-wide) et le sert :
- En `data:image/png;base64,...` URL pour l'injection dans WeasyPrint
  @page CSS `background-image: url(...)` (templates PDF).
- En `Path` brut pour `python-docx::add_picture(...)` (footer DOCX).

Discipline :
- **Singleton process-wide** — chargement asset disque ~5ms, on évite
  de payer ~20 reads par requête `/generate/document` à fort traffic.
- **Fail-safe absolu** — si le fichier est introuvable ou corrompu,
  les helpers retournent `None`. Le caller bascule en mode no-watermark
  (cf. service.py + template Jinja2 conditionnel `{% if watermark_data_url %}`).
- **`WATERMARK_VERSION`** constante versionnée — permet de changer de
  logo sans casser la traçabilité historique dans `library_items.metadata_json`.

Pattern aligné `app/features/images/watermark.py` E4 (singleton logo
Pillow image-only). Ici on a 2 formes (base64 data URL pour WeasyPrint
+ Path pour python-docx) car les 2 pipelines ne consomment pas le même
format d'entrée.
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Final

import structlog

log = structlog.get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

WATERMARK_VERSION: Final[str] = "v1-doc-pdf-docx-2026-05"
"""Version du watermark documents — incrémenter à chaque changement de
logo PNG. Tracée dans `library_items.metadata_json.watermark_version`
pour audit historique (anciens documents gardent leur version)."""

# Chemin vers le logo NEXYA — résolu depuis `app/static/` au niveau
# `app/`. `__file__` pointe vers ce module donc on remonte de 3 niveaux
# (document_generator → features → app).
_WATERMARK_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent.parent / "static" / "nexya_watermark.png"
)


# ══════════════════════════════════════════════════════════════
# Singletons en mémoire process-wide
# ══════════════════════════════════════════════════════════════

_data_url_cache: str | None = None
_path_cache: Path | None = None


def get_watermark_data_url() -> str | None:
    """Retourne le logo NEXYA encodé en `data:image/png;base64,...` URL.

    Singleton process-wide — charge le PNG depuis disque au premier appel,
    cache le résultat. ~5 ms one-shot vs ~5 ms × N requêtes sans cache.

    Returns:
        `data:image/png;base64,iVBOR...` prêt à injecter dans WeasyPrint
        @page CSS `background-image: url('...')`, ou `None` si le fichier
        est introuvable / corrompu (caller bascule en mode no-watermark
        fail-safe via Jinja2 `{% if watermark_data_url %}`).
    """
    global _data_url_cache
    if _data_url_cache is not None:
        return _data_url_cache
    try:
        png_bytes = _WATERMARK_PATH.read_bytes()
        b64 = base64.b64encode(png_bytes).decode("ascii")
        _data_url_cache = f"data:image/png;base64,{b64}"
        log.info(
            "documents.watermark.data_url_loaded",
            path=str(_WATERMARK_PATH),
            bytes=len(png_bytes),
            version=WATERMARK_VERSION,
        )
        return _data_url_cache
    except Exception as exc:  # noqa: BLE001 — fail-safe absolu
        log.warning(
            "documents.watermark.data_url_load_failed",
            path=str(_WATERMARK_PATH),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None


def get_watermark_path() -> Path | None:
    """Retourne le `Path` brut vers le logo PNG pour `python-docx::add_picture`.

    Singleton process-wide — vérifie l'existence du fichier au premier
    appel, cache le résultat. python-docx accepte un `str | Path | BytesIO`,
    on passe le Path pour ne pas charger les bytes en RAM 2 fois (DOCX
    fait la lecture en interne).

    Returns:
        `Path` vers `app/static/nexya_watermark.png` si fichier existe,
        sinon `None` (caller bascule en mode no-watermark fail-safe).
    """
    global _path_cache
    if _path_cache is not None:
        return _path_cache
    try:
        if _WATERMARK_PATH.is_file():
            _path_cache = _WATERMARK_PATH
            log.info(
                "documents.watermark.path_resolved",
                path=str(_WATERMARK_PATH),
                version=WATERMARK_VERSION,
            )
            return _path_cache
        log.warning(
            "documents.watermark.path_not_found",
            path=str(_WATERMARK_PATH),
        )
        return None
    except Exception as exc:  # noqa: BLE001 — fail-safe absolu
        log.warning(
            "documents.watermark.path_resolve_failed",
            path=str(_WATERMARK_PATH),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None


def reset_watermark_cache_for_tests() -> None:
    """Reset des singletons — usage tests uniquement."""
    global _data_url_cache, _path_cache
    _data_url_cache = None
    _path_cache = None
