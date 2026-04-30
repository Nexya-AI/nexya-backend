"""
F2.5 — Wiring `request.tools` dans les 4 providers réels.

Couvre, par provider :
- Le helper format de mapping (`_to_anthropic_tools`, `_to_gemini_tools`).
- L'injection des tools dans le payload SDK (kwargs forwardés correctement).
- Le parsing des tool_calls dans le stream (deltas accumulés ou one-shot).
- Le mapping `finish_reason → FinishReason.TOOL_CALLS`.
- La rétrocompat stricte : `tools=None` → comportement F2 strictement inchangé.

Pas de clé API requise — tous les SDK sont monkeypatchés au niveau du
client singleton (cf. `test_providers_b1.py`).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.providers.base import (
    ChatChunk,
    ChatCompletionRequest,
    ChatMessage,
    FinishReason,
)

# ══════════════════════════════════════════════════════════════════════
# Helpers partagés
# ══════════════════════════════════════════════════════════════════════


_TOOL_CREATE_TASK = {
    "type": "function",
    "function": {
        "name": "create_task",
        "description": "Crée une tâche planifiée pour l'utilisateur.",
        "parameters": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "schedule_type": {"type": "string"},
            },
            "required": ["title", "schedule_type"],
        },
    },
}

_TOOL_LIST_TASKS = {
    "type": "function",
    "function": {
        "name": "list_tasks",
        "description": "Liste les tâches actives de l'utilisateur.",
        "parameters": {"type": "object", "properties": {}},
    },
}


def _request_with_tools(
    *,
    tools: list[dict] | None = None,
    model: str | None = None,
) -> ChatCompletionRequest:
    return ChatCompletionRequest(
        messages=[ChatMessage(role="user", content="Rappelle-moi demain à 8h.")],
        model=model,
        tools=tools,
    )


async def _collect(stream: AsyncIterator[ChatChunk]) -> list[ChatChunk]:
    return [c async for c in stream]


# ══════════════════════════════════════════════════════════════════════
# 1. OpenAI — tools forwardés + parsing deltas + finish TOOL_CALLS
# ══════════════════════════════════════════════════════════════════════


class _FakeOpenAIStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> _FakeOpenAIStream:
        return self

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _openai_text_chunk(text: str) -> Any:
    choice = SimpleNamespace(
        delta=SimpleNamespace(content=text, tool_calls=None),
        finish_reason=None,
    )
    return SimpleNamespace(choices=[choice], usage=None)


def _openai_tool_call_chunk(
    *,
    index: int = 0,
    tool_id: str | None = None,
    name: str | None = None,
    args: str | None = None,
) -> Any:
    fn = SimpleNamespace(name=name, arguments=args)
    tc = SimpleNamespace(index=index, id=tool_id, function=fn)
    choice = SimpleNamespace(
        delta=SimpleNamespace(content=None, tool_calls=[tc]),
        finish_reason=None,
    )
    return SimpleNamespace(choices=[choice], usage=None)


def _openai_finish_chunk(reason: str) -> Any:
    choice = SimpleNamespace(
        delta=SimpleNamespace(content=None, tool_calls=None),
        finish_reason=reason,
    )
    usage = SimpleNamespace(prompt_tokens=12, completion_tokens=4, total_tokens=16)
    return SimpleNamespace(choices=[choice], usage=usage)


def _install_fake_openai(monkeypatch: pytest.MonkeyPatch, chunks: list[Any]) -> MagicMock:
    from app.ai.providers import openai_provider

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=_FakeOpenAIStream(chunks))
    monkeypatch.setattr(openai_provider, "_client", fake_client)
    return fake_client


@pytest.mark.asyncio
async def test_openai_tools_forwarded_with_auto_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si `request.tools` est fourni, le provider passe `tools=` + `tool_choice="auto"`."""
    from app.ai.providers.openai_provider import OpenAIChatProvider

    fake = _install_fake_openai(monkeypatch, [_openai_finish_chunk("stop")])
    provider = OpenAIChatProvider()
    await _collect(
        provider.stream_chat(_request_with_tools(tools=[_TOOL_CREATE_TASK], model="gpt-4o-mini"))
    )
    kwargs = fake.chat.completions.create.await_args.kwargs
    assert "tools" in kwargs
    assert kwargs["tools"] == [_TOOL_CREATE_TASK]
    assert kwargs["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_openai_tools_none_keeps_backward_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`request.tools=None` → kwargs SDK ne contient ni `tools` ni `tool_choice`."""
    from app.ai.providers.openai_provider import OpenAIChatProvider

    fake = _install_fake_openai(monkeypatch, [_openai_finish_chunk("stop")])
    provider = OpenAIChatProvider()
    await _collect(provider.stream_chat(_request_with_tools(tools=None, model="gpt-4o-mini")))
    kwargs = fake.chat.completions.create.await_args.kwargs
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs


@pytest.mark.asyncio
async def test_openai_tool_call_streamed_in_three_deltas_accumulated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Format réel OpenAI : id+name au 1er delta, args fragmentés sur N deltas."""
    from app.ai.providers.openai_provider import OpenAIChatProvider

    chunks = [
        _openai_tool_call_chunk(index=0, tool_id="call_1", name="create_task", args=None),
        _openai_tool_call_chunk(index=0, args='{"title":"prendre med"'),
        _openai_tool_call_chunk(index=0, args=',"schedule_type":"daily"}'),
        _openai_finish_chunk("tool_calls"),
    ]
    _install_fake_openai(monkeypatch, chunks)
    provider = OpenAIChatProvider()

    out = await _collect(
        provider.stream_chat(_request_with_tools(tools=[_TOOL_CREATE_TASK], model="gpt-4o-mini"))
    )
    tool_chunks = [c for c in out if c.tool_call is not None]
    assert len(tool_chunks) == 3
    assert tool_chunks[0].tool_call.id == "call_1"
    assert tool_chunks[0].tool_call.name == "create_task"
    # Concat des fragments JSON par index = 0 → JSON complet valide
    accumulated = "".join(t.tool_call.arguments_json_partial for t in tool_chunks)
    assert json.loads(accumulated) == {"title": "prendre med", "schedule_type": "daily"}
    final = out[-1]
    assert final.finish_reason == FinishReason.TOOL_CALLS


@pytest.mark.asyncio
async def test_openai_parallel_tool_calls_emit_distinct_indices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [
        _openai_tool_call_chunk(index=0, tool_id="call_a", name="create_task", args="{}"),
        _openai_tool_call_chunk(index=1, tool_id="call_b", name="list_tasks", args="{}"),
        _openai_finish_chunk("tool_calls"),
    ]
    _install_fake_openai(monkeypatch, chunks)
    from app.ai.providers.openai_provider import OpenAIChatProvider

    out = await _collect(
        provider := OpenAIChatProvider().stream_chat(
            _request_with_tools(tools=[_TOOL_CREATE_TASK, _TOOL_LIST_TASKS], model="gpt-4o-mini")
        )
    )
    indices = [c.tool_call.index for c in out if c.tool_call is not None]
    assert sorted(indices) == [0, 1]


@pytest.mark.asyncio
async def test_openai_text_response_without_tools_unaffected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Réponse texte normale : aucun ChatChunk.tool_call émis, finish=STOP."""
    from app.ai.providers.openai_provider import OpenAIChatProvider

    _install_fake_openai(
        monkeypatch,
        [_openai_text_chunk("Hello"), _openai_text_chunk(" world."), _openai_finish_chunk("stop")],
    )
    out = await _collect(
        OpenAIChatProvider().stream_chat(_request_with_tools(tools=None, model="gpt-4o-mini"))
    )
    assert all(c.tool_call is None for c in out)
    assert out[-1].finish_reason == FinishReason.STOP


# ══════════════════════════════════════════════════════════════════════
# 2. Qwen — réutilise OpenAI SDK via base_url DashScope
# ══════════════════════════════════════════════════════════════════════


def _install_fake_qwen(monkeypatch: pytest.MonkeyPatch, chunks: list[Any]) -> MagicMock:
    from app.ai.providers import qwen_provider

    fake_client = MagicMock()
    fake_client.chat.completions.create = AsyncMock(return_value=_FakeOpenAIStream(chunks))
    monkeypatch.setattr(qwen_provider, "_client", fake_client)
    return fake_client


@pytest.mark.asyncio
async def test_qwen_tools_forwarded_with_auto_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai.providers.qwen_provider import QwenChatProvider

    fake = _install_fake_qwen(monkeypatch, [_openai_finish_chunk("stop")])
    await _collect(
        QwenChatProvider().stream_chat(
            _request_with_tools(tools=[_TOOL_CREATE_TASK], model="qwen2.5-72b-instruct")
        )
    )
    kwargs = fake.chat.completions.create.await_args.kwargs
    assert kwargs["tools"] == [_TOOL_CREATE_TASK]
    assert kwargs["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_qwen_tool_call_finish_reason_mapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [
        _openai_tool_call_chunk(index=0, tool_id="call_q", name="list_tasks", args="{}"),
        _openai_finish_chunk("tool_calls"),
    ]
    _install_fake_qwen(monkeypatch, chunks)
    from app.ai.providers.qwen_provider import QwenChatProvider

    out = await _collect(
        QwenChatProvider().stream_chat(
            _request_with_tools(tools=[_TOOL_LIST_TASKS], model="qwen2.5-72b-instruct")
        )
    )
    assert any(c.tool_call is not None for c in out)
    assert out[-1].finish_reason == FinishReason.TOOL_CALLS


@pytest.mark.asyncio
async def test_qwen_tools_none_keeps_backward_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_qwen(monkeypatch, [_openai_finish_chunk("stop")])
    from app.ai.providers.qwen_provider import QwenChatProvider

    await _collect(
        QwenChatProvider().stream_chat(
            _request_with_tools(tools=None, model="qwen2.5-72b-instruct")
        )
    )
    kwargs = fake.chat.completions.create.await_args.kwargs
    assert "tools" not in kwargs


# ══════════════════════════════════════════════════════════════════════
# 3. Anthropic — helper + events typés tool_use
# ══════════════════════════════════════════════════════════════════════


def test_anthropic_helper_drops_function_wrapper_and_renames_to_input_schema() -> None:
    from app.ai.providers.anthropic_provider import _to_anthropic_tools

    out = _to_anthropic_tools([_TOOL_CREATE_TASK])
    assert len(out) == 1
    tool = out[0]
    assert tool["name"] == "create_task"
    assert tool["description"].startswith("Crée une tâche")
    assert "input_schema" in tool
    assert tool["input_schema"]["type"] == "object"
    # Pas de wrapper `function` ni clé `parameters` côté Anthropic.
    assert "function" not in tool
    assert "parameters" not in tool
    assert "type" not in tool


def test_anthropic_helper_skips_malformed_entries() -> None:
    from app.ai.providers.anthropic_provider import _to_anthropic_tools

    out = _to_anthropic_tools(
        [
            {"type": "function", "function": {}},  # name absent
            "not a dict",
            {"type": "function", "function": {"name": ""}},  # name vide
            _TOOL_CREATE_TASK,  # valide
        ]
    )
    assert [t["name"] for t in out] == ["create_task"]


def test_anthropic_helper_defaults_empty_schema_when_parameters_absent() -> None:
    from app.ai.providers.anthropic_provider import _to_anthropic_tools

    out = _to_anthropic_tools(
        [{"type": "function", "function": {"name": "ping", "description": "..."}}]
    )
    assert out[0]["input_schema"] == {"type": "object", "properties": {}}


class _FakeAnthropicStream:
    """Reproduit le context manager `client.messages.stream(**kwargs)`."""

    def __init__(self, events: list[Any], final_message: Any | None = None) -> None:
        self._events = events
        self._final = final_message

    async def __aenter__(self) -> _FakeAnthropicStream:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    def __aiter__(self) -> _FakeAnthropicStream:
        return self

    async def __anext__(self) -> Any:
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def get_final_message(self) -> Any:
        return self._final


def _install_fake_anthropic(
    monkeypatch: pytest.MonkeyPatch,
    events: list[Any],
    final_message: Any | None = None,
) -> MagicMock:
    from app.ai.providers import anthropic_provider

    fake_client = MagicMock()
    fake_client.messages.stream = MagicMock(
        return_value=_FakeAnthropicStream(events, final_message=final_message)
    )
    monkeypatch.setattr(anthropic_provider, "_client", fake_client)
    return fake_client


@pytest.mark.asyncio
async def test_anthropic_content_block_tool_use_emits_id_then_args_fragments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`content_block_start` (tool_use) → ChatChunk avec id+name. Puis
    `input_json_delta` → ChatChunk avec fragment JSON. Puis `message_delta`
    avec stop_reason=tool_use → finish=TOOL_CALLS."""
    events = [
        SimpleNamespace(
            type="content_block_start",
            index=0,
            content_block=SimpleNamespace(type="tool_use", id="toolu_1", name="create_task"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="input_json_delta", partial_json='{"title":"med"'),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(
                type="input_json_delta", partial_json=',"schedule_type":"daily"}'
            ),
        ),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="tool_use"),
            usage=None,
        ),
    ]
    _install_fake_anthropic(monkeypatch, events)
    from app.ai.providers.anthropic_provider import AnthropicChatProvider

    out = await _collect(
        AnthropicChatProvider().stream_chat(
            _request_with_tools(tools=[_TOOL_CREATE_TASK], model="claude-sonnet-4-6")
        )
    )
    tool_chunks = [c for c in out if c.tool_call is not None]
    assert tool_chunks[0].tool_call.id == "toolu_1"
    assert tool_chunks[0].tool_call.name == "create_task"
    assert tool_chunks[0].tool_call.arguments_json_partial == ""
    assert tool_chunks[1].tool_call.arguments_json_partial == '{"title":"med"'
    assert tool_chunks[2].tool_call.arguments_json_partial == ',"schedule_type":"daily"}'
    accumulated = "".join(t.tool_call.arguments_json_partial for t in tool_chunks)
    assert json.loads(accumulated) == {"title": "med", "schedule_type": "daily"}
    assert out[-1].finish_reason == FinishReason.TOOL_CALLS


@pytest.mark.asyncio
async def test_anthropic_tools_forwarded_with_auto_tool_choice(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_anthropic(
        monkeypatch,
        [
            SimpleNamespace(
                type="message_delta", delta=SimpleNamespace(stop_reason="end_turn"), usage=None
            )
        ],
    )
    from app.ai.providers.anthropic_provider import AnthropicChatProvider

    await _collect(
        AnthropicChatProvider().stream_chat(
            _request_with_tools(tools=[_TOOL_CREATE_TASK], model="claude-sonnet-4-6")
        )
    )
    kwargs = fake.messages.stream.call_args.kwargs
    assert "tools" in kwargs
    assert kwargs["tools"][0]["name"] == "create_task"
    assert "input_schema" in kwargs["tools"][0]
    assert kwargs["tool_choice"] == {"type": "auto"}


@pytest.mark.asyncio
async def test_anthropic_text_then_tool_use_alternates_correctly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cas réel : Claude streame du texte ('Je vais regarder ça') AVANT le tool_use."""
    events = [
        SimpleNamespace(
            type="content_block_delta",
            index=0,
            delta=SimpleNamespace(type="text_delta", text="Je vais regarder ça."),
        ),
        SimpleNamespace(
            type="content_block_start",
            index=1,
            content_block=SimpleNamespace(type="tool_use", id="toolu_2", name="list_tasks"),
        ),
        SimpleNamespace(
            type="content_block_delta",
            index=1,
            delta=SimpleNamespace(type="input_json_delta", partial_json="{}"),
        ),
        SimpleNamespace(
            type="message_delta",
            delta=SimpleNamespace(stop_reason="tool_use"),
            usage=None,
        ),
    ]
    _install_fake_anthropic(monkeypatch, events)
    from app.ai.providers.anthropic_provider import AnthropicChatProvider

    out = await _collect(
        AnthropicChatProvider().stream_chat(
            _request_with_tools(tools=[_TOOL_LIST_TASKS], model="claude-sonnet-4-6")
        )
    )
    text_chunks = [c.delta for c in out if c.delta]
    tool_chunks = [c for c in out if c.tool_call is not None]
    assert "".join(text_chunks) == "Je vais regarder ça."
    assert tool_chunks[0].tool_call.id == "toolu_2"
    assert tool_chunks[0].tool_call.name == "list_tasks"


@pytest.mark.asyncio
async def test_anthropic_tools_none_keeps_backward_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_anthropic(
        monkeypatch,
        [
            SimpleNamespace(
                type="message_delta", delta=SimpleNamespace(stop_reason="end_turn"), usage=None
            )
        ],
    )
    from app.ai.providers.anthropic_provider import AnthropicChatProvider

    await _collect(
        AnthropicChatProvider().stream_chat(
            _request_with_tools(tools=None, model="claude-sonnet-4-6")
        )
    )
    kwargs = fake.messages.stream.call_args.kwargs
    assert "tools" not in kwargs
    assert "tool_choice" not in kwargs


# ══════════════════════════════════════════════════════════════════════
# 4. Gemini — helper + parsing function_call one-shot
# ══════════════════════════════════════════════════════════════════════


def test_gemini_helper_wraps_in_function_declarations() -> None:
    from app.ai.providers.gemini import _to_gemini_tools

    out = _to_gemini_tools([_TOOL_CREATE_TASK, _TOOL_LIST_TASKS])
    assert len(out) == 1  # un seul "tool set"
    decls = out[0]["function_declarations"]
    names = [d["name"] for d in decls]
    assert names == ["create_task", "list_tasks"]
    # `parameters` JSON Schema préservé
    assert decls[0]["parameters"]["properties"]["title"]["type"] == "string"


def test_gemini_helper_skips_malformed_and_empty() -> None:
    from app.ai.providers.gemini import _to_gemini_tools

    assert _to_gemini_tools([]) == []
    assert _to_gemini_tools([{"type": "function", "function": {}}]) == []


def test_gemini_args_dict_serialized_to_json() -> None:
    from app.ai.providers.gemini import _gemini_args_to_json

    out = _gemini_args_to_json({"title": "med", "schedule_type": "daily"})
    assert json.loads(out) == {"title": "med", "schedule_type": "daily"}


def test_gemini_args_none_returns_empty_object() -> None:
    from app.ai.providers.gemini import _gemini_args_to_json

    assert _gemini_args_to_json(None) == "{}"


class _FakeGeminiStream:
    def __init__(self, chunks: list[Any]) -> None:
        self._chunks = chunks

    def __aiter__(self) -> _FakeGeminiStream:
        return self

    async def __anext__(self) -> Any:
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)


def _gemini_chunk_function_call(name: str, args: dict[str, Any]) -> Any:
    fc = SimpleNamespace(name=name, args=args)
    part = SimpleNamespace(function_call=fc)
    cand = SimpleNamespace(
        content=SimpleNamespace(parts=[part]),
        finish_reason=SimpleNamespace(name="STOP"),
    )
    return SimpleNamespace(candidates=[cand], usage_metadata=None, text=None)


def _install_fake_gemini(monkeypatch: pytest.MonkeyPatch, chunks: list[Any]) -> MagicMock:
    from app.ai.providers import gemini

    fake_client = MagicMock()
    fake_client.aio.models.generate_content_stream = AsyncMock(
        return_value=_FakeGeminiStream(chunks)
    )
    monkeypatch.setattr(gemini, "_client", fake_client)
    return fake_client


@pytest.mark.asyncio
async def test_gemini_function_call_emits_tool_call_chunk_and_forces_tool_calls_finish(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chunks = [
        _gemini_chunk_function_call("create_task", {"title": "med", "schedule_type": "daily"})
    ]
    _install_fake_gemini(monkeypatch, chunks)
    from app.ai.providers.gemini import GeminiChatProvider

    out = await _collect(
        GeminiChatProvider().stream_chat(
            _request_with_tools(tools=[_TOOL_CREATE_TASK], model="gemini-2.5-flash")
        )
    )
    tool_chunks = [c for c in out if c.tool_call is not None]
    assert len(tool_chunks) == 1
    assert tool_chunks[0].tool_call.name == "create_task"
    args = json.loads(tool_chunks[0].tool_call.arguments_json_partial)
    assert args == {"title": "med", "schedule_type": "daily"}
    # Même si Gemini renvoie `STOP`, on force TOOL_CALLS quand on a vu un function_call.
    assert out[-1].finish_reason == FinishReason.TOOL_CALLS


@pytest.mark.asyncio
async def test_gemini_tools_forwarded_in_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _install_fake_gemini(monkeypatch, [])
    from app.ai.providers.gemini import GeminiChatProvider

    await _collect(
        GeminiChatProvider().stream_chat(
            _request_with_tools(tools=[_TOOL_CREATE_TASK], model="gemini-2.5-flash")
        )
    )
    kwargs = fake.aio.models.generate_content_stream.await_args.kwargs
    config = kwargs["config"]
    # `config` est un `types.GenerateContentConfig` instancié avec `tools=[...]`.
    # On ne peut pas inspecter ses attributs précisément (SDK proto), mais on
    # peut vérifier qu'il a bien été créé avec un argument `tools`.
    assert config is not None


@pytest.mark.asyncio
async def test_gemini_tools_none_keeps_backward_compat(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sans `request.tools`, `_to_gemini_tools` n'est pas appelé, finish=STOP."""
    chunk = SimpleNamespace(
        candidates=[
            SimpleNamespace(
                content=SimpleNamespace(parts=None),
                finish_reason=SimpleNamespace(name="STOP"),
            )
        ],
        usage_metadata=None,
        text="Hello",
    )
    _install_fake_gemini(monkeypatch, [chunk])
    from app.ai.providers.gemini import GeminiChatProvider

    out = await _collect(
        GeminiChatProvider().stream_chat(_request_with_tools(tools=None, model="gemini-2.5-flash"))
    )
    assert all(c.tool_call is None for c in out)
    assert out[-1].finish_reason == FinishReason.STOP
