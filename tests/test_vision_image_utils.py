"""Tests unitaires — helpers image (resize + tokens tiles Gemini)."""

from __future__ import annotations

import io

from PIL import Image

from app.features.vision.image_utils import (
    estimate_gemini_image_tokens,
    resize_image_if_needed,
)


def _make_png(w: int, h: int) -> bytes:
    img = Image.new("RGB", (w, h), color=(128, 128, 128))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def test_resize_noop_when_below_max_dimension() -> None:
    data = _make_png(1024, 768)
    result = resize_image_if_needed(data, "image/png", max_dimension=2048)
    assert result.resized is False
    assert result.width == 1024
    assert result.height == 768
    assert result.data == data


def test_resize_downscales_wide_image_preserving_ratio() -> None:
    data = _make_png(4096, 2048)
    result = resize_image_if_needed(data, "image/png", max_dimension=2048)
    assert result.resized is True
    assert result.width == 2048
    assert result.height == 1024  # ratio 2:1 préservé
    # Fichier devenu plus petit.
    assert len(result.data) < len(data)


def test_resize_downscales_tall_image_preserving_ratio() -> None:
    data = _make_png(1024, 4096)
    result = resize_image_if_needed(data, "image/png", max_dimension=2048)
    assert result.resized is True
    assert result.height == 2048
    assert result.width == 512  # ratio 1:4 préservé


def test_estimate_tokens_small_image_returns_minimal() -> None:
    # ≤ 384×384 → 258 tokens.
    assert estimate_gemini_image_tokens(100, 100) == 258
    assert estimate_gemini_image_tokens(384, 384) == 258


def test_estimate_tokens_scales_with_tiles() -> None:
    # 1024×1024 → ceil(1024/768) × ceil(1024/768) × 258 = 2×2×258 = 1032.
    tokens = estimate_gemini_image_tokens(1024, 1024)
    assert tokens == 2 * 2 * 258
    # 2048×2048 → 3×3×258 = 2322.
    assert estimate_gemini_image_tokens(2048, 2048) == 3 * 3 * 258


def test_estimate_tokens_zero_dims_returns_fallback() -> None:
    assert estimate_gemini_image_tokens(0, 100) == 258
    assert estimate_gemini_image_tokens(-10, -10) == 258
