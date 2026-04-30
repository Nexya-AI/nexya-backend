"""Tests unitaires — `MockVisionProvider` (E2)."""

from __future__ import annotations

import pytest

from app.ai.vision.base import ImageInput, VisionInvalidRequestError
from app.ai.vision.mock_vision import MockVisionProvider


def _img(data: bytes = b"fake-img") -> ImageInput:
    return ImageInput(data=data, mime_type="image/png", width=1024, height=768)


@pytest.mark.asyncio
async def test_mock_analyze_returns_deterministic_text_per_inputs() -> None:
    provider = MockVisionProvider()
    r1 = await provider.analyze_images([_img(b"abc")], "décris cette image")
    r2 = await provider.analyze_images([_img(b"abc")], "décris cette image")
    assert r1.text == r2.text
    # Texte contient le sha hex[:16].
    assert "sha=" in r1.text


@pytest.mark.asyncio
async def test_mock_analyze_supports_both_tiers() -> None:
    provider = MockVisionProvider()
    assert "flash" in provider.supports_tiers
    assert "pro" in provider.supports_tiers
    r_flash = await provider.analyze_images([_img()], "q", tier="flash")
    r_pro = await provider.analyze_images([_img()], "q", tier="pro")
    assert "tier=flash" in r_flash.text
    assert "tier=pro" in r_pro.text
    assert r_flash.model == "mock-vision-flash"
    assert r_pro.model == "mock-vision-pro"


@pytest.mark.asyncio
async def test_mock_analyze_cost_is_zero_for_mock() -> None:
    provider = MockVisionProvider()
    r = await provider.analyze_images([_img()], "q")
    assert r.cost_usd == 0.0
    assert r.provider == "mock"


@pytest.mark.asyncio
async def test_mock_analyze_multi_images_counted() -> None:
    provider = MockVisionProvider()
    r = await provider.analyze_images([_img(b"a"), _img(b"b"), _img(b"c")], "compare")
    assert "3 image(s)" in r.text
    # Tokens input scale avec le nombre d'images (258 × n + prompt_tokens).
    assert r.tokens_input >= 258 * 3


@pytest.mark.asyncio
async def test_mock_analyze_rejects_empty_images() -> None:
    provider = MockVisionProvider()
    with pytest.raises(VisionInvalidRequestError):
        await provider.analyze_images([], "q")


@pytest.mark.asyncio
async def test_mock_analyze_respects_max_output_tokens_cap() -> None:
    provider = MockVisionProvider()
    r = await provider.analyze_images([_img()], "q", max_output_tokens=50)
    assert r.tokens_output <= 50
