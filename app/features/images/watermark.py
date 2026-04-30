"""
Watermark visuel NEXYA — overlay Pillow CPU-side, fail-safe absolu.

Session E4. Appelé depuis `POST /image/generate` après la génération
DALL·E/Imagen, AVANT l'autosave Library C3.

Discipline :
- **Singleton logo en mémoire** — chargé au 1er appel, caché process-wide.
  Gain ~20 ms/requête vs re-charger le PNG à chaque fois.
- **Skip si image < 256 px** — sur une vignette le watermark devient
  illisible et gâche l'image. Log info `images.watermark.skipped_too_small`.
- **Fail-safe absolu** — toute exception (logo introuvable, image
  corrompue, OOM) → retour bytes originaux + `applied=False` + log
  warning. **Jamais** bloquer `/image/generate` pour un watermark raté.
- **Position bottom-right fixe** avec marge 2 % — aligné pratiques
  Gemini/DALL·E ChatGPT, simplicité + reconnaissance marque stable.
- **Scale 12 % largeur** (sur 1024² → ~128 px de watermark) — visible
  sans envahir l'image.
- **Opacity 70 %** — lisible mais pas écrasant, contraste avec tout
  type d'image (claire ou foncée).
- **Format de sortie = format input** — PNG preserve alpha, JPEG
  quality 88, WEBP quality 88. Pas de conversion forcée.
- **`WATERMARK_VERSION`** constante versionnée — permet de changer de
  logo plus tard sans casser la traçabilité historique dans
  `library_items.metadata_json`.

Hors scope E4 (sessions futures) :
- C2PA manifeste signé cryptographiquement (E4.5 manuelle, requiert
  clés X.509 Ivan).
- Watermark sur PDF/DOCX/PPTX (Phase 7-8 quand Nexya Studio exportera
  ces formats).
- Custom watermark user-uploaded (Phase 20 Enterprise).
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Final

import structlog
from PIL import Image

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

WATERMARK_VERSION: Final[str] = "v1-oiseau-bleu-2026-04"

_WATERMARK_SCALE_RATIO: Final[float] = 0.12  # 12 % de la largeur image
_WATERMARK_OPACITY: Final[float] = 0.70  # 70 % d'opacité
_WATERMARK_MARGIN_RATIO: Final[float] = 0.02  # 2 % de marge bottom-right
_MIN_IMAGE_DIMENSION: Final[int] = 256  # skip si image < 256 px

# Chemin vers l'asset logo — résolu depuis `app/static/` au niveau
# `app/`. `__file__` pointe vers watermark.py donc on remonte de 3 niveaux.
_WATERMARK_PATH: Final[Path] = (
    Path(__file__).resolve().parent.parent.parent / "static" / "nexya_watermark.png"
)


# ══════════════════════════════════════════════════════════════
# Singleton logo en mémoire
# ══════════════════════════════════════════════════════════════

_logo_cache: Image.Image | None = None


def _get_watermark_logo() -> Image.Image | None:
    """Charge le logo PNG une seule fois (singleton process-wide).

    Retourne `None` si le fichier est introuvable ou corrompu — le
    caller bascule en mode fail-safe (retour image originale).
    """
    global _logo_cache
    if _logo_cache is not None:
        return _logo_cache
    try:
        logo = Image.open(_WATERMARK_PATH).convert("RGBA")
        _logo_cache = logo
        log.info(
            "images.watermark.logo_loaded",
            path=str(_WATERMARK_PATH),
            size=logo.size,
            version=WATERMARK_VERSION,
        )
        return logo
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "images.watermark.logo_load_failed",
            path=str(_WATERMARK_PATH),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None


def reset_logo_cache_for_tests() -> None:
    """Reset du singleton — usage tests uniquement."""
    global _logo_cache
    _logo_cache = None


# ══════════════════════════════════════════════════════════════
# API publique
# ══════════════════════════════════════════════════════════════


def apply_nexya_watermark(
    image_bytes: bytes,
    mime_type: str,
    *,
    scale_ratio: float = _WATERMARK_SCALE_RATIO,
    opacity: float = _WATERMARK_OPACITY,
) -> tuple[bytes, bool]:
    """Applique le watermark NEXYA sur une image.

    Retourne `(bytes, applied)` :
    - `bytes` : image watermarkée si `applied=True`, bytes originaux sinon.
    - `applied: bool` : True si le watermark a été appliqué avec succès.

    Fail-safe absolu : si une exception survient (logo introuvable,
    image corrompue, OOM, format non supporté) → retour
    `(image_bytes, False)` avec log warning. **Jamais** de raise.

    Skip silencieux si image < 256 px (watermark illisible).
    """
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            w, h = img.size
            if min(w, h) < _MIN_IMAGE_DIMENSION:
                log.info(
                    "images.watermark.skipped_too_small",
                    width=w,
                    height=h,
                    min=_MIN_IMAGE_DIMENSION,
                )
                return image_bytes, False

            logo = _get_watermark_logo()
            if logo is None:
                # Logo introuvable — fail-safe retour image originale.
                return image_bytes, False

            # Resize logo à ~`scale_ratio` × largeur image, ratio préservé.
            target_w = max(1, int(w * scale_ratio))
            logo_ratio = logo.height / max(1, logo.width)
            target_h = max(1, int(target_w * logo_ratio))
            logo_resized = logo.resize((target_w, target_h), Image.Resampling.LANCZOS)

            # Appliquer opacity sur le canal alpha.
            if opacity < 1.0:
                alpha = logo_resized.split()[3]
                alpha = alpha.point(lambda p: int(p * opacity))
                logo_resized.putalpha(alpha)

            # Position bottom-right avec marge 2 %.
            margin = int(w * _WATERMARK_MARGIN_RATIO)
            pos_x = max(0, w - target_w - margin)
            pos_y = max(0, h - target_h - margin)

            # Composite en RGBA pour préserver l'alpha du logo.
            base = img.convert("RGBA")
            base.alpha_composite(logo_resized, dest=(pos_x, pos_y))

            # Re-encode selon le format input.
            out_buffer = io.BytesIO()
            fmt_out = _resolve_output_format(mime_type)
            if fmt_out == "JPEG":
                # JPEG ne supporte pas l'alpha → convertir RGB.
                base_rgb = base.convert("RGB")
                base_rgb.save(out_buffer, format="JPEG", quality=88, optimize=True)
            elif fmt_out == "WEBP":
                base.save(out_buffer, format="WEBP", quality=88)
            else:  # PNG default (preserves alpha)
                base.save(out_buffer, format="PNG", optimize=True)

            log.debug(
                "images.watermark.applied",
                input_bytes=len(image_bytes),
                output_bytes=out_buffer.tell(),
                image_size=(w, h),
                logo_size=(target_w, target_h),
                opacity=opacity,
                format_out=fmt_out,
            )
            return out_buffer.getvalue(), True
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "images.watermark.apply_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return image_bytes, False


# ══════════════════════════════════════════════════════════════
# Helpers privés
# ══════════════════════════════════════════════════════════════


def _resolve_output_format(mime_type: str) -> str:
    """Retourne le format Pillow correspondant au mime input.

    PNG par défaut (préserve alpha + qualité). JPEG si input JPEG
    (quality 88). WEBP si input WEBP.
    """
    low = (mime_type or "").lower()
    if "jpeg" in low or "jpg" in low:
        return "JPEG"
    if "webp" in low:
        return "WEBP"
    return "PNG"
