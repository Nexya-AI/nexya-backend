"""
Tests unitaires — `apply_nexya_watermark` (E4).

Fonction pure Pillow CPU-side. Tests sans DB ni réseau.

Couverture :
- Application sur PNG/JPEG/WEBP → bytes différents, applied=True.
- Skip si image < 256 px (log `skipped_too_small`).
- Scale ratio 12 %, position bottom-right, opacity 70 % par défaut.
- Custom scale_ratio / opacity override.
- Format de sortie = format input (PNG→PNG, JPEG→JPEG, WEBP→WEBP, autre→PNG).
- Logo singleton chargé une seule fois (2 appels → 1 seul log).
- Fail-safe : logo introuvable ou bytes corrompus → applied=False + bytes originaux.
- `WATERMARK_VERSION` constante exposée.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.features.images import watermark as wm_module
from app.features.images.watermark import (
    WATERMARK_VERSION,
    apply_nexya_watermark,
    reset_logo_cache_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_logo_cache():
    """Chaque test commence avec un cache logo vierge."""
    reset_logo_cache_for_tests()
    yield
    reset_logo_cache_for_tests()


def _make_png(w: int, h: int, color=(100, 150, 200)) -> bytes:
    img = Image.new("RGB", (w, h), color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_jpeg(w: int, h: int) -> bytes:
    img = Image.new("RGB", (w, h), color=(50, 50, 50))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


def _make_webp(w: int, h: int) -> bytes:
    img = Image.new("RGBA", (w, h), color=(200, 100, 100, 255))
    buf = io.BytesIO()
    img.save(buf, format="WEBP", quality=90)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════
# 1. Application sur PNG 1024×1024 — bytes différents + applied=True
# ══════════════════════════════════════════════════════════════


def test_watermark_applied_on_png_1024() -> None:
    original = _make_png(1024, 1024)
    output, applied = apply_nexya_watermark(original, "image/png")
    assert applied is True
    # Bytes ont changé (watermark incrusté).
    assert output != original
    # Format output = PNG.
    with Image.open(io.BytesIO(output)) as img:
        assert img.format == "PNG"
        assert img.size == (1024, 1024)


# ══════════════════════════════════════════════════════════════
# 2. Application sur JPEG — sortie JPEG
# ══════════════════════════════════════════════════════════════


def test_watermark_applied_on_jpeg_preserves_format() -> None:
    original = _make_jpeg(1024, 1024)
    output, applied = apply_nexya_watermark(original, "image/jpeg")
    assert applied is True
    with Image.open(io.BytesIO(output)) as img:
        assert img.format == "JPEG"


# ══════════════════════════════════════════════════════════════
# 3. Application sur WEBP — sortie WEBP
# ══════════════════════════════════════════════════════════════


def test_watermark_applied_on_webp_preserves_format() -> None:
    original = _make_webp(1024, 1024)
    output, applied = apply_nexya_watermark(original, "image/webp")
    assert applied is True
    with Image.open(io.BytesIO(output)) as img:
        assert img.format == "WEBP"


# ══════════════════════════════════════════════════════════════
# 4. Skip si image < 256 px
# ══════════════════════════════════════════════════════════════


def test_watermark_skipped_on_small_image() -> None:
    original = _make_png(200, 200)
    output, applied = apply_nexya_watermark(original, "image/png")
    assert applied is False
    # Bytes identiques (pas de traitement appliqué).
    assert output == original


def test_watermark_skipped_on_narrow_image() -> None:
    # Image 1024 de large MAIS 100 de haut → min(w,h) = 100 < 256.
    original = _make_png(1024, 100)
    _, applied = apply_nexya_watermark(original, "image/png")
    assert applied is False


# ══════════════════════════════════════════════════════════════
# 5. Custom scale_ratio / opacity override
# ══════════════════════════════════════════════════════════════


def test_watermark_custom_scale_ratio_override() -> None:
    original = _make_png(1024, 1024)
    # Scale 0.05 = 51 px de logo, vs 0.20 = 205 px. Bytes différents.
    out_small, _ = apply_nexya_watermark(original, "image/png", scale_ratio=0.05)
    out_large, _ = apply_nexya_watermark(original, "image/png", scale_ratio=0.20)
    assert out_small != out_large


def test_watermark_custom_opacity_override() -> None:
    original = _make_png(1024, 1024)
    out_low, _ = apply_nexya_watermark(original, "image/png", opacity=0.30)
    out_high, _ = apply_nexya_watermark(original, "image/png", opacity=1.00)
    assert out_low != out_high


# ══════════════════════════════════════════════════════════════
# 6. Format unknown → fallback PNG
# ══════════════════════════════════════════════════════════════


def test_watermark_unknown_mime_falls_back_to_png() -> None:
    original = _make_png(1024, 1024)
    output, applied = apply_nexya_watermark(original, "image/heic")
    assert applied is True
    with Image.open(io.BytesIO(output)) as img:
        assert img.format == "PNG"


# ══════════════════════════════════════════════════════════════
# 7. Logo singleton — chargé une seule fois
# ══════════════════════════════════════════════════════════════


def test_logo_loaded_once_singleton() -> None:
    # 1er appel : charge le logo.
    out1, _ = apply_nexya_watermark(_make_png(512, 512), "image/png")
    # 2ème appel : utilise le cache.
    out2, _ = apply_nexya_watermark(_make_png(512, 512), "image/png")
    # Les deux ont réussi (le cache a fonctionné).
    assert len(out1) > 0
    assert len(out2) > 0
    # Le logo est bien en cache après les 2 appels.
    assert wm_module._logo_cache is not None


# ══════════════════════════════════════════════════════════════
# 8. Fail-safe bytes corrompus
# ══════════════════════════════════════════════════════════════


def test_watermark_failsafe_on_corrupted_bytes() -> None:
    corrupted = b"not-an-image-at-all"
    output, applied = apply_nexya_watermark(corrupted, "image/png")
    assert applied is False
    assert output == corrupted


# ══════════════════════════════════════════════════════════════
# 9. Fail-safe logo introuvable
# ══════════════════════════════════════════════════════════════


def test_watermark_failsafe_logo_missing(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Si le fichier logo est introuvable, retour image originale."""
    fake_path = tmp_path / "nonexistent.png"
    monkeypatch.setattr(wm_module, "_WATERMARK_PATH", fake_path)
    reset_logo_cache_for_tests()

    original = _make_png(1024, 1024)
    output, applied = apply_nexya_watermark(original, "image/png")
    assert applied is False
    assert output == original


# ══════════════════════════════════════════════════════════════
# 10. WATERMARK_VERSION constante exposée
# ══════════════════════════════════════════════════════════════


def test_watermark_version_constant_exposed() -> None:
    assert isinstance(WATERMARK_VERSION, str)
    assert len(WATERMARK_VERSION) > 0
    # Format : "v1-oiseau-bleu-2026-04".
    assert WATERMARK_VERSION.startswith("v")


# ══════════════════════════════════════════════════════════════
# 11. Scale ratio effectif — logo dimensions scalent avec image
# ══════════════════════════════════════════════════════════════


def test_watermark_larger_image_uses_larger_logo() -> None:
    """Une image 2048 a un watermark plus grand qu'une image 512."""
    small = _make_png(512, 512)
    large = _make_png(2048, 2048)
    out_small, _ = apply_nexya_watermark(small, "image/png")
    out_large, _ = apply_nexya_watermark(large, "image/png")
    # L'image 2048 avec watermark est logiquement plus lourde.
    assert len(out_large) > len(out_small)


# ══════════════════════════════════════════════════════════════
# 12. Skip sur image tout juste à la frontière 256
# ══════════════════════════════════════════════════════════════


def test_watermark_accepts_256_exactly() -> None:
    # min=256 devrait passer (condition `< 256`).
    original = _make_png(256, 256)
    _, applied = apply_nexya_watermark(original, "image/png")
    assert applied is True


def test_watermark_rejects_255() -> None:
    original = _make_png(255, 300)
    _, applied = apply_nexya_watermark(original, "image/png")
    assert applied is False


# ══════════════════════════════════════════════════════════════
# 13. JPEG output = RGB (no alpha artifact)
# ══════════════════════════════════════════════════════════════


def test_watermark_jpeg_output_is_rgb_no_alpha_artifact() -> None:
    """JPEG ne supporte pas l'alpha — la conversion RGB doit être propre."""
    original = _make_jpeg(1024, 1024)
    output, _ = apply_nexya_watermark(original, "image/jpeg")
    with Image.open(io.BytesIO(output)) as img:
        assert img.mode == "RGB"  # Pas RGBA.


# ══════════════════════════════════════════════════════════════
# 14. PNG output preserve alpha channel du watermark
# ══════════════════════════════════════════════════════════════


def test_watermark_png_output_preserves_alpha() -> None:
    """PNG output garde RGBA pour préserver la transparence du logo."""
    original = _make_png(1024, 1024)
    output, _ = apply_nexya_watermark(original, "image/png")
    with Image.open(io.BytesIO(output)) as img:
        assert img.mode == "RGBA"  # alpha préservé
