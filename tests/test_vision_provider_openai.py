"""Tests unitaires — `OpenAIVisionProvider` avec SDK `openai` mocké."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.vision import openai_vision as ov_module
from app.ai.vision.base import (
    ImageInput,
    VisionAuthError,
    VisionInvalidRequestError,
    VisionRateLimitError,
    VisionUnavailableError,
)
from app.ai.vision.openai_vision import OpenAIVisionProvider


class _FakeErrors:
    class AuthenticationError(Exception):
        pass

    class PermissionDeniedError(Exception):
        pass

    class RateLimitError(Exception):
        def __init__(self, msg: str, retry_after: str | None = None) -> None:
            super().__init__(msg)
            self.response = MagicMock()
            self.response.headers = {"retry-after": retry_after} if retry_after else {}

    class NotFoundError(Exception):
        pass

    class BadRequestError(Exception):
        pass

    class APIConnectionError(Exception):
        pass

    class APITimeoutError(Exception):
        pass


def _img() -> ImageInput:
    return ImageInput(data=b"fake", mime_type="image/png", width=512, height=512)


def _install_fake_openai(monkeypatch: pytest.MonkeyPatch, *, response=None, side_effect=None):
    import sys

    fake = MagicMock()
    fake.AuthenticationError = _FakeErrors.AuthenticationError
    fake.PermissionDeniedError = _FakeErrors.PermissionDeniedError
    fake.RateLimitError = _FakeErrors.RateLimitError
    fake.NotFoundError = _FakeErrors.NotFoundError
    fake.BadRequestError = _FakeErrors.BadRequestError
    fake.APIConnectionError = _FakeErrors.APIConnectionError
    fake.APITimeoutError = _FakeErrors.APITimeoutError

    client = MagicMock()
    client.chat = MagicMock()
    client.chat.completions = MagicMock()
    if side_effect is not None:
        client.chat.completions.create = AsyncMock(side_effect=side_effect)
    else:
        client.chat.completions.create = AsyncMock(return_value=response)
    fake.AsyncOpenAI = MagicMock(return_value=client)

    monkeypatch.setitem(sys.modules, "openai", fake)
    ov_module._reset_client_for_tests()

    from app.config import settings

    monkeypatch.setattr(settings, "openai_api_key", "sk-fake", raising=False)
    return client


@pytest.mark.asyncio
async def test_openai_analyze_happy_path_with_cost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message = MagicMock()
    response.choices[0].message.content = "Description GPT-4o"
    response.usage = MagicMock()
    response.usage.prompt_tokens = 500
    response.usage.completion_tokens = 100

    _install_fake_openai(monkeypatch, response=response)
    provider = OpenAIVisionProvider()
    result = await provider.analyze_images([_img()], "q", tier="pro")
    assert result.text == "Description GPT-4o"
    assert result.model == "gpt-4o"
    # Cost = 500*2.50/1M + 100*10.00/1M = 0.00125 + 0.001 = 0.00225.
    assert result.cost_usd == round(500 * 2.50 / 1_000_000 + 100 * 10.00 / 1_000_000, 6)


@pytest.mark.asyncio
async def test_openai_analyze_rejects_flash_tier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_openai(monkeypatch, response=MagicMock())
    provider = OpenAIVisionProvider()
    with pytest.raises(VisionInvalidRequestError):
        await provider.analyze_images([_img()], "q", tier="flash")


@pytest.mark.asyncio
async def test_openai_analyze_maps_auth_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_openai(
        monkeypatch,
        side_effect=_FakeErrors.AuthenticationError("401"),
    )
    provider = OpenAIVisionProvider()
    with pytest.raises(VisionAuthError):
        await provider.analyze_images([_img()], "q", tier="pro")


@pytest.mark.asyncio
async def test_openai_analyze_maps_rate_limit_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_openai(
        monkeypatch,
        side_effect=_FakeErrors.RateLimitError("429", retry_after="45"),
    )
    provider = OpenAIVisionProvider()
    with pytest.raises(VisionRateLimitError) as ctx:
        await provider.analyze_images([_img()], "q", tier="pro")
    assert ctx.value.retry_after == 45.0


@pytest.mark.asyncio
async def test_openai_analyze_maps_connection_as_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_fake_openai(
        monkeypatch,
        side_effect=_FakeErrors.APIConnectionError("down"),
    )
    provider = OpenAIVisionProvider()
    with pytest.raises(VisionUnavailableError):
        await provider.analyze_images([_img()], "q", tier="pro")


@pytest.mark.asyncio
async def test_openai_analyze_builds_multimodal_message_format(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = MagicMock()
    response.choices = [MagicMock()]
    response.choices[0].message = MagicMock()
    response.choices[0].message.content = "ok"
    response.usage = MagicMock()
    response.usage.prompt_tokens = 100
    response.usage.completion_tokens = 20

    client = _install_fake_openai(monkeypatch, response=response)
    provider = OpenAIVisionProvider()
    await provider.analyze_images([_img()], "q", tier="pro")
    kwargs = client.chat.completions.create.await_args.kwargs
    # Message user contient liste avec image_url + text.
    msgs = kwargs["messages"]
    user_msg = msgs[-1]
    assert user_msg["role"] == "user"
    assert isinstance(user_msg["content"], list)
    assert any(part["type"] == "image_url" for part in user_msg["content"])
    assert any(part["type"] == "text" for part in user_msg["content"])
