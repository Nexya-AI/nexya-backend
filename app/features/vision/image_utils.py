"""
Helpers image — resize Pillow + estimation tokens Gemini tiles-based.

Fonctions pures synchrones (CPU-bound). Le caller est responsable du
`asyncio.to_thread(resize_image, ...)` pour ne pas bloquer l'event loop
sur une image 4K.

**Stratégie de resize** : si `max(width, height) > max_dim`, on redimensionne
en gardant le ratio, puis on re-encode dans le format d'origine (PNG si
PNG, JPEG sinon — WEBP peut être demandé mais on garde JPEG par
sécurité compat Claude qui n'accepte pas toujours WEBP). Économie
typique : image 4K → 2K = **4× moins de tokens** Gemini.

**Estimation tokens Gemini** : règle tiles-based documentée Google :
- Image ≤ 384×384 → 258 tokens
- Chaque tile 768×768 → 258 tokens
- Image plus grande → ceil(w/768) × ceil(h/768) × 258 tokens
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass
from typing import Final

import structlog

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

_TILE_SIZE: Final[int] = 768
_TOKENS_PER_TILE: Final[int] = 258
_SMALL_IMAGE_THRESHOLD: Final[int] = 384
_SMALL_IMAGE_TOKENS: Final[int] = 258

# Formats Pillow ↔ MIME.
_PIL_FORMAT_BY_MIME: Final[dict[str, str]] = {
    "image/png": "PNG",
    "image/jpeg": "JPEG",
    "image/jpg": "JPEG",
    "image/webp": "WEBP",
    "image/gif": "GIF",
}


@dataclass(frozen=True, slots=True)
class ResizedImage:
    """Résultat d'un resize : bytes + dims finales + mime + resize_applied."""

    data: bytes
    width: int
    height: int
    mime_type: str
    resized: bool  # True si un resize a été appliqué


# ══════════════════════════════════════════════════════════════
# API publique
# ══════════════════════════════════════════════════════════════


def resize_image_if_needed(
    data: bytes,
    mime_type: str,
    *,
    max_dimension: int = 2048,
) -> ResizedImage:
    """Redimensionne l'image si `max(w, h) > max_dimension`, sinon retourne
    l'original inchangé (mais récupère quand même les dimensions pour
    le caller).

    Fail-safe : si Pillow ne peut pas décoder l'image (format exotique,
    corruption), retourne les bytes bruts avec `width=None, height=None`
    plutôt que de raise — le provider LLM aura sa propre tolérance, on
    ne bloque pas en amont pour ça.
    """
    try:
        from PIL import Image  # noqa: PLC0415 — dépendance lourde
    except ImportError:
        log.warning("vision.image.pillow_missing")
        return ResizedImage(data=data, width=0, height=0, mime_type=mime_type, resized=False)

    try:
        with Image.open(io.BytesIO(data)) as img:
            w, h = img.size
            if max(w, h) <= max_dimension:
                # Pas de resize mais on mémorise les dims.
                return ResizedImage(
                    data=data,
                    width=w,
                    height=h,
                    mime_type=mime_type,
                    resized=False,
                )

            # Calcul ratio préservé.
            if w > h:
                new_w = max_dimension
                new_h = int(h * max_dimension / w)
            else:
                new_h = max_dimension
                new_w = int(w * max_dimension / h)

            img_resized = img.resize((new_w, new_h), Image.Resampling.LANCZOS)

            # Re-encode dans le format original (ou JPEG si transparence
            # pas supportée).
            pil_format = _PIL_FORMAT_BY_MIME.get(mime_type.lower(), "JPEG")
            out_buffer = io.BytesIO()
            save_kwargs: dict = {}
            if pil_format == "JPEG":
                save_kwargs["quality"] = 88
                save_kwargs["optimize"] = True
                # Convertir RGBA en RGB si JPEG (pas d'alpha en JPEG).
                if img_resized.mode in ("RGBA", "LA", "P"):
                    img_resized = img_resized.convert("RGB")
            elif pil_format == "PNG":
                save_kwargs["optimize"] = True
            elif pil_format == "WEBP":
                save_kwargs["quality"] = 90

            img_resized.save(out_buffer, format=pil_format, **save_kwargs)
            resized_bytes = out_buffer.getvalue()

            # mime_type recalculé si on a switché RGBA→JPEG.
            out_mime = mime_type
            if pil_format == "JPEG" and mime_type.lower() == "image/png":
                out_mime = "image/jpeg"

            log.debug(
                "vision.image.resized",
                original_size=(w, h),
                new_size=(new_w, new_h),
                original_bytes=len(data),
                new_bytes=len(resized_bytes),
                mime_out=out_mime,
            )
            return ResizedImage(
                data=resized_bytes,
                width=new_w,
                height=new_h,
                mime_type=out_mime,
                resized=True,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "vision.image.resize_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return ResizedImage(data=data, width=0, height=0, mime_type=mime_type, resized=False)


def estimate_gemini_image_tokens(width: int, height: int) -> int:
    """Estime le nombre de tokens image Gemini selon la règle tiles-based.

    - Image ≤ 384×384 → 258 tokens (unit).
    - Sinon → ceil(w/768) × ceil(h/768) × 258 tokens.

    Utilisé pré-appel pour caper `total_input_tokens` contre abus.
    """
    if width <= 0 or height <= 0:
        return _SMALL_IMAGE_TOKENS
    if width <= _SMALL_IMAGE_THRESHOLD and height <= _SMALL_IMAGE_THRESHOLD:
        return _SMALL_IMAGE_TOKENS
    tiles_w = math.ceil(width / _TILE_SIZE)
    tiles_h = math.ceil(height / _TILE_SIZE)
    return max(_SMALL_IMAGE_TOKENS, tiles_w * tiles_h * _TOKENS_PER_TILE)
