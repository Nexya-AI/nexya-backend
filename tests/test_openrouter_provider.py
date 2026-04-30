"""
Tests unitaires B1/B3 — `app.ai.providers.openrouter_provider`.

Couvre :
1. `_get_client` : headers HTTP-Referer + X-Title forwardés depuis settings,
   base_url override, ProviderAuthError si clé vide.
2. `supported_models` : liste curée attendue + rejet modèle non supporté.
3. `_map_sdk_exception` : mappe les classes d'erreur OpenAI SDK → hiérarchie
   neutre NEXYA (Auth / RateLimit / ContentFilter / InvalidRequest / Unavailable).
4. `stream_chat` : happy-path avec chunks + usage + finish_reason mappé.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.providers import openrouter_provider as openrouter_module
from app.ai.providers.base import (
    ChatCompletionRequest,
    ChatMessage,
    FinishReason,
    ProviderAuthError,
    ProviderContentFilteredError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from app.ai.providers.openrouter_provider import (
    OpenRouterChatProvider,
    _build_openrouter_messages,
    _extract_openrouter_usage,
    _get_client,
    _map_openrouter_finish_reason,
    _map_sdk_exception,
    _reset_client_for_tests,
)

# ══════════════════════════════════════════════════════════════
# 1. Identité + supported_models
# ══════════════════════════════════════════════════════════════


def test_provider_identity_and_curated_models() -> None:
    p = OpenRouterChatProvider()
    assert p.name == "openrouter"
    assert p.default_model == "anthropic/claude-3.5-sonnet"
    assert p.supported_models == frozenset(
        {
            "anthropic/claude-3.5-sonnet",
            "meta-llama/llama-3.1-70b-instruct",
            "mistralai/mistral-large",
            "deepseek/deepseek-chat",
            "qwen/qwen-2.5-72b-instruct",
        }
    )
    assert p.max_context_tokens == 128_000


# ══════════════════════════════════════════════════════════════
# 2. `_get_client` : auth + headers
# ══════════════════════════════════════════════════════════════


def test_get_client_raises_auth_error_when_key_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_client_for_tests()
    monkeypatch.setattr(openrouter_module.settings, "openrouter_api_key", "")
    with pytest.raises(ProviderAuthError):
        _get_client()


def test_get_client_forwards_optional_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_client_for_tests()

    captured: dict[str, Any] = {}

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    fake_module = SimpleNamespace(AsyncOpenAI=_FakeAsyncOpenAI)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_module)

    monkeypatch.setattr(openrouter_module.settings, "openrouter_api_key", "or-test-key")
    monkeypatch.setattr(
        openrouter_module.settings,
        "openrouter_base_url",
        "https://openrouter.ai/api/v1",
    )
    monkeypatch.setattr(openrouter_module.settings, "openrouter_referer", "https://nexya.ai")
    monkeypatch.setattr(openrouter_module.settings, "openrouter_app_title", "NEXYA")

    _get_client()
    assert captured["api_key"] == "or-test-key"
    assert captured["base_url"] == "https://openrouter.ai/api/v1"
    assert captured["max_retries"] == 0
    assert captured["default_headers"] == {
        "HTTP-Referer": "https://nexya.ai",
        "X-Title": "NEXYA",
    }
    _reset_client_for_tests()


def test_get_client_sends_no_headers_when_settings_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_client_for_tests()
    captured: dict[str, Any] = {}

    class _FakeAsyncOpenAI:
        def __init__(self, **kwargs: Any) -> None:
            captured.update(kwargs)

    fake_module = SimpleNamespace(AsyncOpenAI=_FakeAsyncOpenAI)
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_module)

    monkeypatch.setattr(openrouter_module.settings, "openrouter_api_key", "or-test-key")
    monkeypatch.setattr(openrouter_module.settings, "openrouter_referer", "")
    monkeypatch.setattr(openrouter_module.settings, "openrouter_app_title", "")

    _get_client()
    # Quand aucun header optionnel n'est configuré, on passe None pour ne rien
    # envoyer — pas un dict vide (pour éviter les overrides d'en-têtes par défaut).
    assert captured["default_headers"] is None
    _reset_client_for_tests()


# ══════════════════════════════════════════════════════════════
# 3. `_map_sdk_exception` — 5 branches + retry_after parsé
# ══════════════════════════════════════════════════════════════


class _FakeAuthenticationError(Exception):
    pass


class _FakePermissionDeniedError(Exception):
    pass


class _FakeBadRequestError(Exception):
    pass


class _FakeRateLimitError(Exception):
    def __init__(self, message: str, retry_after: str | None = None) -> None:
        super().__init__(message)
        if retry_after is not None:
            headers = {"retry-after": retry_after}
            self.response = SimpleNamespace(headers=headers)
        else:
            self.response = SimpleNamespace(headers={})


class _FakeNotFoundError(Exception):
    pass


class _FakeAPIConnectionError(Exception):
    pass


class _FakeAPITimeoutError(Exception):
    pass


class _FakeInternalServerError(Exception):
    pass


@pytest.fixture
def patch_openai_sdk_exception_classes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Patch l'import local `from openai import ...` dans `_map_sdk_exception`."""

    fake_openai = SimpleNamespace(
        APIConnectionError=_FakeAPIConnectionError,
        APITimeoutError=_FakeAPITimeoutError,
        AuthenticationError=_FakeAuthenticationError,
        BadRequestError=_FakeBadRequestError,
        InternalServerError=_FakeInternalServerError,
        NotFoundError=_FakeNotFoundError,
        PermissionDeniedError=_FakePermissionDeniedError,
        RateLimitError=_FakeRateLimitError,
    )
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai)


def test_map_authentication_error(patch_openai_sdk_exception_classes: None) -> None:
    exc = _FakeAuthenticationError("invalid key")
    mapped = _map_sdk_exception(exc, model="m")
    assert isinstance(mapped, ProviderAuthError)
    assert mapped.provider == "openrouter"


def test_map_permission_denied_maps_to_auth(
    patch_openai_sdk_exception_classes: None,
) -> None:
    mapped = _map_sdk_exception(_FakePermissionDeniedError("403"), model="m")
    assert isinstance(mapped, ProviderAuthError)


def test_map_rate_limit_parses_retry_after(
    patch_openai_sdk_exception_classes: None,
) -> None:
    mapped = _map_sdk_exception(_FakeRateLimitError("slow down", retry_after="7.5"), model="m")
    assert isinstance(mapped, ProviderRateLimitError)
    assert mapped.retry_after_seconds == 7.5


def test_map_rate_limit_retry_after_invalid_becomes_none(
    patch_openai_sdk_exception_classes: None,
) -> None:
    mapped = _map_sdk_exception(_FakeRateLimitError("x", retry_after="not-a-number"), model="m")
    assert isinstance(mapped, ProviderRateLimitError)
    assert mapped.retry_after_seconds is None


def test_map_content_filter_on_bad_request(
    patch_openai_sdk_exception_classes: None,
) -> None:
    mapped = _map_sdk_exception(_FakeBadRequestError("content_filter triggered"), model="m")
    assert isinstance(mapped, ProviderContentFilteredError)


def test_map_bad_request_other_becomes_invalid_request(
    patch_openai_sdk_exception_classes: None,
) -> None:
    mapped = _map_sdk_exception(_FakeBadRequestError("invalid message format"), model="m")
    assert isinstance(mapped, ProviderInvalidRequestError)


def test_map_not_found_becomes_invalid_request(
    patch_openai_sdk_exception_classes: None,
) -> None:
    mapped = _map_sdk_exception(_FakeNotFoundError("no model"), model="m")
    assert isinstance(mapped, ProviderInvalidRequestError)


def test_map_api_connection_becomes_unavailable(
    patch_openai_sdk_exception_classes: None,
) -> None:
    mapped = _map_sdk_exception(_FakeAPIConnectionError("conn reset"), model="m")
    assert isinstance(mapped, ProviderUnavailableError)


def test_map_generic_exception_becomes_unavailable(
    patch_openai_sdk_exception_classes: None,
) -> None:
    mapped = _map_sdk_exception(RuntimeError("weird"), model="m")
    assert isinstance(mapped, ProviderUnavailableError)


# ══════════════════════════════════════════════════════════════
# 4. `_map_openrouter_finish_reason`
# ══════════════════════════════════════════════════════════════


def test_map_finish_reason_stop() -> None:
    assert _map_openrouter_finish_reason("stop") == FinishReason.STOP


def test_map_finish_reason_length() -> None:
    assert _map_openrouter_finish_reason("length") == FinishReason.LENGTH


def test_map_finish_reason_content_filter_both_spellings() -> None:
    assert _map_openrouter_finish_reason("content_filter") == FinishReason.CONTENT_FILTER
    assert _map_openrouter_finish_reason("content-filter") == FinishReason.CONTENT_FILTER


def test_map_finish_reason_unknown_defaults_to_stop() -> None:
    assert _map_openrouter_finish_reason("weird-thing") == FinishReason.STOP


# ══════════════════════════════════════════════════════════════
# 5. Helpers `_build_openrouter_messages` + `_extract_openrouter_usage`
# ══════════════════════════════════════════════════════════════


def test_build_messages_merges_system_prompt_first() -> None:
    req = ChatCompletionRequest(
        model="anthropic/claude-3.5-sonnet",
        messages=[
            ChatMessage(role="system", content="Be concise."),
            ChatMessage(role="user", content="Hello"),
        ],
        system_prompt="You are NEXYA.",
    )
    msgs = _build_openrouter_messages(req)
    assert msgs[0]["role"] == "system"
    assert "NEXYA" in msgs[0]["content"]
    assert "concise" in msgs[0]["content"]
    assert msgs[-1] == {"role": "user", "content": "Hello"}


def test_build_messages_without_system_prompt_passes_user_only() -> None:
    req = ChatCompletionRequest(
        model="anthropic/claude-3.5-sonnet",
        messages=[ChatMessage(role="user", content="Hi")],
    )
    msgs = _build_openrouter_messages(req)
    assert msgs == [{"role": "user", "content": "Hi"}]


def test_extract_usage_returns_none_when_zero() -> None:
    obj = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    assert _extract_openrouter_usage(obj) is None


def test_extract_usage_parses_values() -> None:
    obj = SimpleNamespace(prompt_tokens=10, completion_tokens=20, total_tokens=30)
    usage = _extract_openrouter_usage(obj)
    assert usage is not None
    assert usage.prompt_tokens == 10
    assert usage.completion_tokens == 20
    assert usage.total_tokens == 30


def test_extract_usage_recovers_total_from_sum() -> None:
    obj = SimpleNamespace(prompt_tokens=5, completion_tokens=7, total_tokens=0)
    usage = _extract_openrouter_usage(obj)
    assert usage is not None
    assert usage.total_tokens == 12


# ══════════════════════════════════════════════════════════════
# 6. `stream_chat` — intégration avec client mocké
# ══════════════════════════════════════════════════════════════


class _AsyncChunkIter:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> _AsyncChunkIter:
        return self

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


@pytest.mark.asyncio
async def test_stream_chat_rejects_unsupported_model() -> None:
    provider = OpenRouterChatProvider()
    req = ChatCompletionRequest(
        model="gpt-4o",  # pas dans la liste curée OpenRouter
        messages=[ChatMessage(role="user", content="hi")],
    )
    with pytest.raises(ProviderInvalidRequestError):
        async for _ in provider.stream_chat(req):
            pass


@pytest.mark.asyncio
async def test_stream_chat_happy_path_yields_chunks_and_usage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Chunks SDK simulés (shape identique à openai.types.chat.ChatCompletionChunk).
    def _make_delta_chunk(text: str, finish: str | None = None) -> Any:
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    delta=SimpleNamespace(content=text),
                    finish_reason=finish,
                )
            ],
            usage=None,
        )

    final_usage = SimpleNamespace(prompt_tokens=3, completion_tokens=4, total_tokens=7)
    final_chunk = SimpleNamespace(
        choices=[
            SimpleNamespace(
                delta=SimpleNamespace(content=""),
                finish_reason="stop",
            )
        ],
        usage=final_usage,
    )

    chunks = [
        _make_delta_chunk("Hello "),
        _make_delta_chunk("world"),
        final_chunk,
    ]
    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=_AsyncChunkIter(chunks))

    monkeypatch.setattr(openrouter_module, "_get_client", lambda: fake_client)

    provider = OpenRouterChatProvider()
    req = ChatCompletionRequest(
        model="anthropic/claude-3.5-sonnet",
        messages=[ChatMessage(role="user", content="hi")],
    )

    received = []
    async for chunk in provider.stream_chat(req):
        received.append(chunk)

    # 2 deltas texte + 1 trailer avec usage/finish.
    deltas = [c.delta for c in received if c.delta]
    assert deltas == ["Hello ", "world"]
    trailer = received[-1]
    assert trailer.finish_reason == FinishReason.STOP
    assert trailer.usage is not None
    assert trailer.usage.total_tokens == 7


@pytest.mark.asyncio
async def test_stream_chat_maps_create_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Simule une erreur dès `client.chat.completions.create(...)`.
    fake_client = MagicMock()
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_client.chat.completions.create = AsyncMock(side_effect=RuntimeError("boom"))
    monkeypatch.setattr(openrouter_module, "_get_client", lambda: fake_client)

    # Le mapper importe depuis `openai` — on fournit des classes vides pour que
    # les `isinstance` échouent tous → on tombe dans le fallback Unavailable.
    fake_openai = SimpleNamespace(
        APIConnectionError=type("X1", (Exception,), {}),
        APITimeoutError=type("X2", (Exception,), {}),
        AuthenticationError=type("X3", (Exception,), {}),
        BadRequestError=type("X4", (Exception,), {}),
        InternalServerError=type("X5", (Exception,), {}),
        NotFoundError=type("X6", (Exception,), {}),
        PermissionDeniedError=type("X7", (Exception,), {}),
        RateLimitError=type("X8", (Exception,), {}),
    )
    monkeypatch.setitem(__import__("sys").modules, "openai", fake_openai)

    provider = OpenRouterChatProvider()
    req = ChatCompletionRequest(
        model="anthropic/claude-3.5-sonnet",
        messages=[ChatMessage(role="user", content="hi")],
    )

    with pytest.raises(ProviderUnavailableError):
        async for _ in provider.stream_chat(req):
            pass
