"""
Tests unitaires B1 — Providers OpenAI, Anthropic, Qwen + Mock + Router factory.

Couvre :
1. `MockChatProvider` : streaming par défaut, scripted_chunks, force_fail,
   model non supporté → ProviderInvalidRequestError, echo_user_message,
   CancelledError propagé.
2. `OpenAIChatProvider` : happy-path streaming (monkeypatch AsyncOpenAI),
   système fusionné, reasoning model (o1) sans `temperature`/`system`,
   finish_reason mapping, usage extraction, error mapping (Auth / RateLimit /
   ContentFilter / InvalidRequest / Unavailable), CancelledError.
3. `AnthropicChatProvider` : happy-path streaming (context manager fake),
   system prompt séparé, stop_reason mapping, usage via `get_final_message`,
   error mapping.
4. `QwenChatProvider` : happy-path streaming (même infra OpenAI), error
   mapping identique.
5. Router factory `build_default_router()` : sélection Mock si clé vide,
   sélection réelle si clé présente (monkeypatch settings), image provider
   absent si gemini_api_key vide.
6. Live tests gated via `skipif` sur clé API absente (smoke test minimal
   par provider).

Aucune clé API réelle n'est requise pour 99% des tests : les SDK sont
monkeypatchés au niveau du client singleton.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.providers.base import (
    ChatChunk,
    ChatCompletionRequest,
    ChatMessage,
    ChatUsage,
    FinishReason,
    ProviderAuthError,
    ProviderContentFilteredError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from app.ai.providers.mock import MockChatProvider

# ══════════════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════════════


def _request(
    *,
    messages: list[ChatMessage] | None = None,
    system_prompt: str | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
    stop: list[str] | None = None,
    temperature: float = 0.7,
    user_id: str | None = None,
) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        messages=messages or [ChatMessage(role="user", content="Bonjour.")],
        system_prompt=system_prompt,
        model=model,
        max_tokens=max_tokens,
        stop_sequences=list(stop) if stop else [],
        temperature=temperature,
        user_id=user_id,
    )


async def _drain(
    provider_stream: AsyncIterator[ChatChunk],
) -> tuple[list[str], ChatChunk | None]:
    """Consomme le stream d'un provider et retourne (deltas, last_chunk)."""
    deltas: list[str] = []
    final: ChatChunk | None = None
    async for chunk in provider_stream:
        if chunk.finish_reason is not None:
            final = chunk
        elif chunk.delta:
            deltas.append(chunk.delta)
    return deltas, final


# ══════════════════════════════════════════════════════════════════════
# 1. MockChatProvider
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_default_streams_and_final_chunk() -> None:
    mock = MockChatProvider()
    deltas, final = await _drain(mock.stream_chat(_request(model="mock-default")))
    assert len(deltas) >= 1
    assert "".join(deltas).startswith("Bonjour")
    assert final is not None
    assert final.finish_reason == FinishReason.STOP
    assert final.usage is not None
    assert final.usage.prompt_tokens >= 1
    assert final.usage.completion_tokens >= 1


@pytest.mark.asyncio
async def test_mock_scripted_chunks_are_yielded_verbatim() -> None:
    mock = MockChatProvider(scripted_chunks=["un ", "deux ", "trois"])
    deltas, final = await _drain(mock.stream_chat(_request(model="mock-default")))
    assert deltas == ["un ", "deux ", "trois"]
    assert final is not None and final.finish_reason == FinishReason.STOP


@pytest.mark.asyncio
async def test_mock_force_fail_raises_before_yielding() -> None:
    err = ProviderRateLimitError("simulated 429", provider="mock")
    mock = MockChatProvider(force_fail=err)
    with pytest.raises(ProviderRateLimitError):
        async for _ in mock.stream_chat(_request(model="mock-default")):
            pytest.fail("should not yield")


@pytest.mark.asyncio
async def test_mock_unsupported_model_raises_invalid_request() -> None:
    mock = MockChatProvider()
    with pytest.raises(ProviderInvalidRequestError):
        async for _ in mock.stream_chat(_request(model="gpt-4-ultra-9000")):
            pytest.fail("should not yield")


@pytest.mark.asyncio
async def test_mock_echo_user_message_prefixes_reply() -> None:
    mock = MockChatProvider(echo_user_message=True)
    req = _request(
        messages=[ChatMessage(role="user", content="quelle heure est-il ?")],
        model="mock-default",
    )
    deltas, _ = await _drain(mock.stream_chat(req))
    text = "".join(deltas)
    assert "quelle heure" in text
    assert "[mock:mock]" in text


@pytest.mark.asyncio
async def test_mock_usurps_provider_identity_for_factory_wiring() -> None:
    mock = MockChatProvider(
        name="openai",
        default_model="gpt-4o-mini",
        supported_models=["gpt-4o-mini", "gpt-4o"],
    )
    assert mock.name == "openai"
    assert mock.default_model == "gpt-4o-mini"
    assert mock.supports_model("gpt-4o")
    assert not mock.supports_model("claude-opus-4-6")


@pytest.mark.asyncio
async def test_mock_propagates_cancellation() -> None:
    mock = MockChatProvider(scripted_chunks=["a", "b", "c"], min_chunk_delay_seconds=0.05)

    async def _run() -> None:
        async for _ in mock.stream_chat(_request(model="mock-default")):
            pass

    task = asyncio.create_task(_run())
    await asyncio.sleep(0.01)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


# ══════════════════════════════════════════════════════════════════════
# 2. OpenAIChatProvider — streaming + error mapping (SDK monkeypatched)
# ══════════════════════════════════════════════════════════════════════


class _FakeOpenAIStream:
    """AsyncIterable qui joue une liste de chunks pré-scripted."""

    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> _FakeOpenAIStream:
        return self

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _openai_chunk(*, delta: str = "", finish_reason: str | None = None, usage: Any = None) -> Any:
    choice = SimpleNamespace(
        delta=SimpleNamespace(content=delta or None),
        finish_reason=finish_reason,
    )
    return SimpleNamespace(choices=[choice], usage=usage)


def _openai_usage(prompt: int, completion: int) -> Any:
    return SimpleNamespace(
        prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
    )


def _install_fake_openai_client(monkeypatch: pytest.MonkeyPatch, chunks: list[Any]) -> MagicMock:
    from app.ai.providers import openai_provider

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=_FakeOpenAIStream(chunks))
    fake_client.models.list = AsyncMock(return_value=[])
    monkeypatch.setattr(openai_provider, "_client", fake_client)
    return fake_client


@pytest.mark.asyncio
async def test_openai_happy_path_streams_deltas_and_final_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.providers.openai_provider import OpenAIChatProvider

    _install_fake_openai_client(
        monkeypatch,
        chunks=[
            _openai_chunk(delta="Bon"),
            _openai_chunk(delta="jour"),
            _openai_chunk(finish_reason="stop", usage=_openai_usage(10, 5)),
        ],
    )
    provider = OpenAIChatProvider()
    deltas, final = await _drain(provider.stream_chat(_request(model="gpt-4o-mini")))
    assert "".join(deltas) == "Bonjour"
    assert final is not None
    assert final.finish_reason == FinishReason.STOP
    assert final.usage == ChatUsage(10, 5, 15)


@pytest.mark.asyncio
async def test_openai_unsupported_model_raises_invalid_request() -> None:
    from app.ai.providers.openai_provider import OpenAIChatProvider

    provider = OpenAIChatProvider()
    with pytest.raises(ProviderInvalidRequestError):
        async for _ in provider.stream_chat(_request(model="gpt-99-turbo")):
            pytest.fail("should not yield")


@pytest.mark.asyncio
async def test_openai_system_prompt_is_merged_as_system_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.providers.openai_provider import OpenAIChatProvider

    fake_client = _install_fake_openai_client(
        monkeypatch,
        chunks=[_openai_chunk(delta="ok", finish_reason="stop", usage=_openai_usage(3, 1))],
    )
    provider = OpenAIChatProvider()
    req = _request(
        model="gpt-4o-mini",
        system_prompt="Tu es NEXYA.",
        messages=[
            ChatMessage(role="system", content="Réponds en français."),
            ChatMessage(role="user", content="Salut"),
        ],
    )
    async for _ in provider.stream_chat(req):
        pass
    call_kwargs = fake_client.chat.completions.create.await_args.kwargs
    messages = call_kwargs["messages"]
    assert messages[0]["role"] == "system"
    assert "Tu es NEXYA." in messages[0]["content"]
    assert "Réponds en français." in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "Salut"}


@pytest.mark.asyncio
async def test_openai_reasoning_model_drops_temperature_and_merges_system_into_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.providers.openai_provider import OpenAIChatProvider

    fake_client = _install_fake_openai_client(
        monkeypatch,
        chunks=[_openai_chunk(delta="ok", finish_reason="stop", usage=_openai_usage(3, 1))],
    )
    provider = OpenAIChatProvider()
    req = _request(
        model="o1-mini",
        system_prompt="Tu réfléchis.",
        messages=[ChatMessage(role="user", content="Résous.")],
        max_tokens=200,
    )
    async for _ in provider.stream_chat(req):
        pass
    call_kwargs = fake_client.chat.completions.create.await_args.kwargs
    assert "temperature" not in call_kwargs
    assert "max_completion_tokens" in call_kwargs
    assert call_kwargs["max_completion_tokens"] == 200
    # Le premier message user doit porter les instructions system.
    first_user = call_kwargs["messages"][0]
    assert first_user["role"] == "user"
    assert "Tu réfléchis." in first_user["content"]


@pytest.mark.asyncio
async def test_openai_finish_reason_length_mapped_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.providers.openai_provider import OpenAIChatProvider

    _install_fake_openai_client(
        monkeypatch,
        chunks=[
            _openai_chunk(delta="tronqué"),
            _openai_chunk(finish_reason="length", usage=_openai_usage(5, 50)),
        ],
    )
    provider = OpenAIChatProvider()
    _, final = await _drain(provider.stream_chat(_request(model="gpt-4o-mini")))
    assert final is not None and final.finish_reason == FinishReason.LENGTH


@pytest.mark.asyncio
async def test_openai_content_filter_finish_reason_mapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.providers.openai_provider import OpenAIChatProvider

    _install_fake_openai_client(
        monkeypatch,
        chunks=[_openai_chunk(finish_reason="content_filter", usage=_openai_usage(5, 0))],
    )
    provider = OpenAIChatProvider()
    _, final = await _drain(provider.stream_chat(_request(model="gpt-4o-mini")))
    assert final is not None and final.finish_reason == FinishReason.CONTENT_FILTER


@pytest.mark.asyncio
async def test_openai_auth_error_is_mapped_before_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openai import AuthenticationError

    from app.ai.providers import openai_provider
    from app.ai.providers.openai_provider import OpenAIChatProvider

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=AuthenticationError(
            message="invalid api key",
            response=MagicMock(status_code=401, headers={}, request=MagicMock()),
            body=None,
        )
    )
    monkeypatch.setattr(openai_provider, "_client", fake_client)
    provider = OpenAIChatProvider()
    with pytest.raises(ProviderAuthError):
        async for _ in provider.stream_chat(_request(model="gpt-4o-mini")):
            pytest.fail("should not yield")


@pytest.mark.asyncio
async def test_openai_rate_limit_error_is_mapped_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openai import RateLimitError

    from app.ai.providers import openai_provider
    from app.ai.providers.openai_provider import OpenAIChatProvider

    response = MagicMock()
    response.status_code = 429
    response.headers = {"retry-after": "12"}
    response.request = MagicMock()

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=RateLimitError(message="rate", response=response, body=None)
    )
    monkeypatch.setattr(openai_provider, "_client", fake_client)
    provider = OpenAIChatProvider()
    with pytest.raises(ProviderRateLimitError) as excinfo:
        async for _ in provider.stream_chat(_request(model="gpt-4o-mini")):
            pytest.fail("should not yield")
    assert excinfo.value.retry_after_seconds == 12.0


@pytest.mark.asyncio
async def test_openai_content_filter_bad_request_mapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openai import BadRequestError

    from app.ai.providers import openai_provider
    from app.ai.providers.openai_provider import OpenAIChatProvider

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=BadRequestError(
            message="content_filter triggered",
            response=MagicMock(status_code=400, headers={}, request=MagicMock()),
            body=None,
        )
    )
    monkeypatch.setattr(openai_provider, "_client", fake_client)
    provider = OpenAIChatProvider()
    with pytest.raises(ProviderContentFilteredError):
        async for _ in provider.stream_chat(_request(model="gpt-4o-mini")):
            pytest.fail("should not yield")


@pytest.mark.asyncio
async def test_openai_connection_error_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openai import APIConnectionError

    from app.ai.providers import openai_provider
    from app.ai.providers.openai_provider import OpenAIChatProvider

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=APIConnectionError(request=MagicMock())
    )
    monkeypatch.setattr(openai_provider, "_client", fake_client)
    provider = OpenAIChatProvider()
    with pytest.raises(ProviderUnavailableError) as excinfo:
        async for _ in provider.stream_chat(_request(model="gpt-4o-mini")):
            pytest.fail("should not yield")
    assert excinfo.value.retryable is True


@pytest.mark.asyncio
async def test_openai_cancellation_propagated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.providers import openai_provider
    from app.ai.providers.openai_provider import OpenAIChatProvider

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(side_effect=asyncio.CancelledError())
    monkeypatch.setattr(openai_provider, "_client", fake_client)
    provider = OpenAIChatProvider()
    with pytest.raises(asyncio.CancelledError):
        async for _ in provider.stream_chat(_request(model="gpt-4o-mini")):
            pytest.fail("should not yield")


# ══════════════════════════════════════════════════════════════════════
# 3. AnthropicChatProvider — streaming via context manager fake
# ══════════════════════════════════════════════════════════════════════


class _FakeClaudeStream:
    def __init__(self, events: list[Any], final_message: Any | None = None) -> None:
        self._events = events
        self._final_message = final_message

    async def __aenter__(self) -> _FakeClaudeStream:
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None

    def __aiter__(self) -> _FakeClaudeStream:
        return self

    async def __anext__(self) -> Any:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def get_final_message(self) -> Any:
        return self._final_message


def _claude_content_delta(text: str) -> Any:
    return SimpleNamespace(type="content_block_delta", delta=SimpleNamespace(text=text))


def _claude_message_stop() -> Any:
    return SimpleNamespace(type="message_stop")


def _claude_final_message(*, stop_reason: str, prompt: int, completion: int) -> Any:
    return SimpleNamespace(
        stop_reason=stop_reason,
        usage=SimpleNamespace(input_tokens=prompt, output_tokens=completion),
    )


def _install_fake_claude(
    monkeypatch: pytest.MonkeyPatch, events: list[Any], final_message: Any | None = None
) -> MagicMock:
    from app.ai.providers import anthropic_provider

    fake_client = MagicMock()
    fake_stream = _FakeClaudeStream(events, final_message=final_message)
    fake_client.messages.stream = MagicMock(return_value=fake_stream)
    fake_client.messages.create = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(anthropic_provider, "_client", fake_client)
    return fake_client


@pytest.mark.asyncio
async def test_anthropic_happy_path_streams_and_final_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.providers.anthropic_provider import AnthropicChatProvider

    _install_fake_claude(
        monkeypatch,
        events=[
            _claude_content_delta("Bon"),
            _claude_content_delta("jour"),
            _claude_message_stop(),
        ],
        final_message=_claude_final_message(stop_reason="end_turn", prompt=8, completion=2),
    )
    provider = AnthropicChatProvider()
    deltas, final = await _drain(provider.stream_chat(_request(model="claude-sonnet-4-6")))
    assert "".join(deltas) == "Bonjour"
    assert final is not None
    assert final.finish_reason == FinishReason.STOP
    assert final.usage == ChatUsage(8, 2, 10)


@pytest.mark.asyncio
async def test_anthropic_system_prompt_passed_as_separate_param(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.providers.anthropic_provider import AnthropicChatProvider

    fake_client = _install_fake_claude(
        monkeypatch,
        events=[_claude_message_stop()],
        final_message=_claude_final_message(stop_reason="end_turn", prompt=3, completion=0),
    )
    provider = AnthropicChatProvider()
    req = _request(
        model="claude-sonnet-4-6",
        system_prompt="Tu es NEXYA.",
        messages=[
            ChatMessage(role="system", content="Français uniquement."),
            ChatMessage(role="user", content="Salut"),
        ],
    )
    async for _ in provider.stream_chat(req):
        pass
    call_kwargs = fake_client.messages.stream.call_args.kwargs
    # `system` doit contenir les deux morceaux ; le tableau `messages` ne doit
    # contenir AUCUN rôle "system".
    assert "Tu es NEXYA." in call_kwargs["system"]
    assert "Français uniquement." in call_kwargs["system"]
    for msg in call_kwargs["messages"]:
        assert msg["role"] != "system"


@pytest.mark.asyncio
async def test_anthropic_max_tokens_default_applied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.providers.anthropic_provider import AnthropicChatProvider

    fake_client = _install_fake_claude(
        monkeypatch,
        events=[_claude_message_stop()],
        final_message=_claude_final_message(stop_reason="end_turn", prompt=1, completion=0),
    )
    provider = AnthropicChatProvider()
    async for _ in provider.stream_chat(_request(model="claude-sonnet-4-6")):
        pass
    call_kwargs = fake_client.messages.stream.call_args.kwargs
    assert call_kwargs["max_tokens"] >= 1  # défaut 4096 appliqué


@pytest.mark.asyncio
async def test_anthropic_stop_reason_max_tokens_mapped_to_length(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.providers.anthropic_provider import AnthropicChatProvider

    _install_fake_claude(
        monkeypatch,
        events=[_claude_content_delta("tronqué"), _claude_message_stop()],
        final_message=_claude_final_message(stop_reason="max_tokens", prompt=5, completion=10),
    )
    provider = AnthropicChatProvider()
    _, final = await _drain(provider.stream_chat(_request(model="claude-sonnet-4-6")))
    assert final is not None and final.finish_reason == FinishReason.LENGTH


@pytest.mark.asyncio
async def test_anthropic_auth_error_mapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from anthropic import AuthenticationError

    from app.ai.providers import anthropic_provider
    from app.ai.providers.anthropic_provider import AnthropicChatProvider

    class _BoomStream:
        async def __aenter__(self) -> _BoomStream:
            raise AuthenticationError(
                message="bad key",
                response=MagicMock(status_code=401, headers={}, request=MagicMock()),
                body=None,
            )

        async def __aexit__(self, *a: Any) -> None:
            return None

    fake_client = MagicMock()
    fake_client.messages.stream = MagicMock(return_value=_BoomStream())
    monkeypatch.setattr(anthropic_provider, "_client", fake_client)
    provider = AnthropicChatProvider()
    with pytest.raises(ProviderAuthError):
        async for _ in provider.stream_chat(_request(model="claude-sonnet-4-6")):
            pytest.fail("should not yield")


@pytest.mark.asyncio
async def test_anthropic_unsupported_model_raises_invalid_request() -> None:
    from app.ai.providers.anthropic_provider import AnthropicChatProvider

    provider = AnthropicChatProvider()
    with pytest.raises(ProviderInvalidRequestError):
        async for _ in provider.stream_chat(_request(model="claude-ultra-7000")):
            pytest.fail("should not yield")


# ══════════════════════════════════════════════════════════════════════
# 4. QwenChatProvider — réutilise infra OpenAI
# ══════════════════════════════════════════════════════════════════════


def _install_fake_qwen_client(monkeypatch: pytest.MonkeyPatch, chunks: list[Any]) -> MagicMock:
    from app.ai.providers import qwen_provider

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=_FakeOpenAIStream(chunks))
    fake_client.models.list = AsyncMock(return_value=[])
    monkeypatch.setattr(qwen_provider, "_client", fake_client)
    return fake_client


@pytest.mark.asyncio
async def test_qwen_happy_path_streams_deltas_and_final_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.providers.qwen_provider import QwenChatProvider

    _install_fake_qwen_client(
        monkeypatch,
        chunks=[
            _openai_chunk(delta="Sa"),
            _openai_chunk(delta="lut"),
            _openai_chunk(finish_reason="stop", usage=_openai_usage(6, 3)),
        ],
    )
    provider = QwenChatProvider()
    deltas, final = await _drain(provider.stream_chat(_request(model="qwen2.5-72b-instruct")))
    assert "".join(deltas) == "Salut"
    assert final is not None and final.finish_reason == FinishReason.STOP
    assert final.usage == ChatUsage(6, 3, 9)


@pytest.mark.asyncio
async def test_qwen_unsupported_model_raises_invalid_request() -> None:
    from app.ai.providers.qwen_provider import QwenChatProvider

    provider = QwenChatProvider()
    with pytest.raises(ProviderInvalidRequestError):
        async for _ in provider.stream_chat(_request(model="qwen-4000")):
            pytest.fail("should not yield")


@pytest.mark.asyncio
async def test_qwen_rate_limit_mapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from openai import RateLimitError

    from app.ai.providers import qwen_provider
    from app.ai.providers.qwen_provider import QwenChatProvider

    response = MagicMock()
    response.status_code = 429
    response.headers = {"retry-after": "3"}
    response.request = MagicMock()

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(
        side_effect=RateLimitError(message="rate", response=response, body=None)
    )
    monkeypatch.setattr(qwen_provider, "_client", fake_client)
    provider = QwenChatProvider()
    with pytest.raises(ProviderRateLimitError) as excinfo:
        async for _ in provider.stream_chat(_request(model="qwen2.5-72b-instruct")):
            pytest.fail("should not yield")
    assert excinfo.value.retry_after_seconds == 3.0


# ══════════════════════════════════════════════════════════════════════
# 5. Router factory — sélection Mock vs réel par clé
# ══════════════════════════════════════════════════════════════════════


def test_build_default_router_uses_mock_when_key_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai import router as router_mod
    from app.ai.providers.mock import MockChatProvider
    from app.ai.providers.openai_provider import OpenAIChatProvider

    monkeypatch.setattr(router_mod.settings, "openai_api_key", "", raising=False)
    monkeypatch.setattr(router_mod.settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(router_mod.settings, "qwen_api_key", "", raising=False)
    monkeypatch.setattr(router_mod.settings, "gemini_api_key", "fake-gemini", raising=False)

    router = router_mod.build_default_router()
    assert router.has_chat_provider("openai")
    assert router.has_chat_provider("anthropic")
    assert router.has_chat_provider("qwen")
    # OpenAI doit être un Mock (clé vide)
    openai_entry = router._chat["openai"]  # noqa: SLF001 — test introspection
    anthropic_entry = router._chat["anthropic"]  # noqa: SLF001
    qwen_entry = router._chat["qwen"]  # noqa: SLF001
    assert isinstance(openai_entry, MockChatProvider)
    assert isinstance(anthropic_entry, MockChatProvider)
    assert isinstance(qwen_entry, MockChatProvider)
    # Le mock porte bien l'identité openai
    assert openai_entry.name == "openai"
    assert openai_entry.default_model == OpenAIChatProvider.default_model


def test_build_default_router_uses_real_when_key_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai import router as router_mod
    from app.ai.providers.anthropic_provider import AnthropicChatProvider
    from app.ai.providers.openai_provider import OpenAIChatProvider
    from app.ai.providers.qwen_provider import QwenChatProvider

    monkeypatch.setattr(router_mod.settings, "openai_api_key", "sk-fake", raising=False)
    monkeypatch.setattr(router_mod.settings, "anthropic_api_key", "sk-ant", raising=False)
    monkeypatch.setattr(router_mod.settings, "qwen_api_key", "sk-qwen", raising=False)
    monkeypatch.setattr(router_mod.settings, "gemini_api_key", "fake-gemini", raising=False)

    router = router_mod.build_default_router()
    assert isinstance(router._chat["openai"], OpenAIChatProvider)  # noqa: SLF001
    assert isinstance(router._chat["anthropic"], AnthropicChatProvider)  # noqa: SLF001
    assert isinstance(router._chat["qwen"], QwenChatProvider)  # noqa: SLF001


def test_build_default_router_drops_image_provider_when_gemini_key_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai import router as router_mod

    monkeypatch.setattr(router_mod.settings, "gemini_api_key", "", raising=False)
    monkeypatch.setattr(router_mod.settings, "openai_api_key", "", raising=False)
    monkeypatch.setattr(router_mod.settings, "anthropic_api_key", "", raising=False)
    monkeypatch.setattr(router_mod.settings, "qwen_api_key", "", raising=False)

    router = router_mod.build_default_router()
    assert router.image_provider_names() == []


# ══════════════════════════════════════════════════════════════════════
# 6. LIVE tests — gated par skipif (branchés uniquement quand Ivan fournit)
# ══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY absente — live test skippé (mock-first par défaut).",
)
async def test_live_openai_smoke_streams_tokens() -> None:
    from app.ai.providers.openai_provider import (
        OpenAIChatProvider,
        _reset_client_for_tests,
    )

    _reset_client_for_tests()
    provider = OpenAIChatProvider()
    req = _request(
        model="gpt-4o-mini",
        messages=[ChatMessage(role="user", content="Réponds en un mot : bonjour.")],
        max_tokens=20,
    )
    deltas, final = await _drain(provider.stream_chat(req))
    assert deltas, "aucun delta reçu — provider ne stream pas"
    assert final is not None
    assert final.finish_reason in {FinishReason.STOP, FinishReason.LENGTH}


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY absente — live test skippé.",
)
async def test_live_anthropic_smoke_streams_tokens() -> None:
    from app.ai.providers.anthropic_provider import (
        AnthropicChatProvider,
        _reset_client_for_tests,
    )

    _reset_client_for_tests()
    provider = AnthropicChatProvider()
    req = _request(
        model="claude-haiku-4-5",
        messages=[ChatMessage(role="user", content="Réponds 'ok'.")],
        max_tokens=20,
    )
    deltas, final = await _drain(provider.stream_chat(req))
    assert deltas
    assert final is not None
    assert final.finish_reason in {FinishReason.STOP, FinishReason.LENGTH}


@pytest.mark.asyncio
@pytest.mark.skipif(
    not os.environ.get("QWEN_API_KEY"),
    reason="QWEN_API_KEY absente — live test skippé.",
)
async def test_live_qwen_smoke_streams_tokens() -> None:
    from app.ai.providers.qwen_provider import (
        QwenChatProvider,
        _reset_client_for_tests,
    )

    _reset_client_for_tests()
    provider = QwenChatProvider()
    req = _request(
        model="qwen2.5-7b-instruct",
        messages=[ChatMessage(role="user", content="Dis 'ok'.")],
        max_tokens=20,
    )
    deltas, final = await _drain(provider.stream_chat(req))
    assert deltas
    assert final is not None
    assert final.finish_reason in {FinishReason.STOP, FinishReason.LENGTH}
