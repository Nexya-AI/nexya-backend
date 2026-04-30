"""
Tests de l'orchestrateur tool_calls.

Couvre :
- collect_tool_calls_from_chunks : reconstruit les tool_calls depuis un
  flux de ChatChunk (simple + multi-deltas + multi-tools parallèles).
- execute_tool_call : parse args JSON, appelle handler, catche exceptions.
- build_tool_messages_for_next_round : format des messages injectés.
- run_with_tool_rounds : cycle complet stream → tool → re-stream,
  cap max_rounds anti-boucle.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from unittest.mock import MagicMock

import pytest

from app.ai.providers import ChatChunk, FinishReason
from app.ai.providers.base import ToolCallDelta
from app.ai.tools import (
    ToolDefinition,
    ToolRegistry,
    ToolResult,
    build_tool_messages_for_next_round,
    collect_tool_calls_from_chunks,
    execute_tool_call,
    run_with_tool_rounds,
)
from app.ai.tools.base import ToolExecutionError


def _mk_tool_call_chunk(
    *, name="create_task", args_json='{"title":"x"}', index=0, call_id="c1"
) -> ChatChunk:
    return ChatChunk(
        delta="",
        tool_call=ToolCallDelta(
            id=call_id,
            name=name,
            arguments_json_partial=args_json,
            index=index,
        ),
    )


def _mk_done_tool_calls() -> ChatChunk:
    return ChatChunk(delta="", finish_reason=FinishReason.TOOL_CALLS)


def _mk_done_stop() -> ChatChunk:
    return ChatChunk(delta="", finish_reason=FinishReason.STOP)


# ───────────────────────────────────────────────────────────────────
# collect_tool_calls_from_chunks
# ───────────────────────────────────────────────────────────────────


def test_collect_single_tool_call():
    chunks = [_mk_tool_call_chunk(), _mk_done_tool_calls()]
    result = collect_tool_calls_from_chunks(chunks)
    assert result.finished_with_tool_calls is True
    assert len(result.tool_calls) == 1
    tc = result.tool_calls[0]
    assert tc.name == "create_task"
    assert tc.arguments_json == '{"title":"x"}'
    assert tc.id == "c1"
    assert tc.index == 0


def test_collect_multi_delta_same_index_accumulates_args():
    chunks = [
        ChatChunk(
            delta="",
            tool_call=ToolCallDelta(
                id="c1", name="create_task", arguments_json_partial='{"ti', index=0
            ),
        ),
        ChatChunk(
            delta="",
            tool_call=ToolCallDelta(id="c1", name="", arguments_json_partial='tle":"x"}', index=0),
        ),
        _mk_done_tool_calls(),
    ]
    result = collect_tool_calls_from_chunks(chunks)
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].arguments_json == '{"title":"x"}'
    assert result.tool_calls[0].name == "create_task"


def test_collect_returns_empty_if_no_tool_call():
    chunks = [ChatChunk(delta="hello"), _mk_done_stop()]
    result = collect_tool_calls_from_chunks(chunks)
    assert result.tool_calls == []
    assert result.finished_with_tool_calls is False


def test_collect_multiple_tool_calls_different_indices():
    chunks = [
        _mk_tool_call_chunk(name="a", index=0, call_id="c1"),
        _mk_tool_call_chunk(name="b", index=1, call_id="c2"),
        _mk_done_tool_calls(),
    ]
    result = collect_tool_calls_from_chunks(chunks)
    assert len(result.tool_calls) == 2
    names = {tc.name for tc in result.tool_calls}
    assert names == {"a", "b"}


# ───────────────────────────────────────────────────────────────────
# execute_tool_call
# ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_execute_tool_happy_path():
    async def handler(user, db, args):
        return ToolResult(success=True, data={"echo": args})

    tool = ToolDefinition(
        name="echo",
        description="echo",
        parameters_schema={"type": "object"},
        handler=handler,
    )
    reg = ToolRegistry()
    reg.register(tool)

    from app.ai.tools.orchestrator import CollectedToolCall

    tc = CollectedToolCall(id="c1", name="echo", arguments_json='{"a":1}')
    result = await execute_tool_call(tc, registry=reg, user=MagicMock(), db=MagicMock())
    assert result.success is True
    assert result.data["echo"] == {"a": 1}


@pytest.mark.asyncio
async def test_execute_tool_not_found_returns_failure():
    from app.ai.tools.orchestrator import CollectedToolCall

    reg = ToolRegistry()
    tc = CollectedToolCall(id="c1", name="unknown", arguments_json="{}")
    result = await execute_tool_call(tc, registry=reg, user=MagicMock(), db=MagicMock())
    assert result.success is False
    assert result.error["code"] == "TOOL_NOT_FOUND"


@pytest.mark.asyncio
async def test_execute_tool_bad_json_args_returns_failure():
    async def handler(user, db, args):
        return ToolResult(success=True)

    tool = ToolDefinition(
        name="x",
        description="x",
        parameters_schema={"type": "object"},
        handler=handler,
    )
    reg = ToolRegistry()
    reg.register(tool)

    from app.ai.tools.orchestrator import CollectedToolCall

    tc = CollectedToolCall(id="c1", name="x", arguments_json="{not-json")
    result = await execute_tool_call(tc, registry=reg, user=MagicMock(), db=MagicMock())
    assert result.success is False
    assert result.error["code"] == "TOOL_ARGS_INVALID"


@pytest.mark.asyncio
async def test_execute_tool_raises_execution_error_caught():
    async def handler(user, db, args):
        raise ToolExecutionError("SOMETHING", "Quelque chose a échoué.")

    tool = ToolDefinition(
        name="fail",
        description="fail",
        parameters_schema={"type": "object"},
        handler=handler,
    )
    reg = ToolRegistry()
    reg.register(tool)

    from app.ai.tools.orchestrator import CollectedToolCall

    tc = CollectedToolCall(id="c1", name="fail", arguments_json="{}")
    result = await execute_tool_call(tc, registry=reg, user=MagicMock(), db=MagicMock())
    assert result.success is False
    assert result.error["code"] == "SOMETHING"


@pytest.mark.asyncio
async def test_execute_tool_catches_unexpected_exception():
    async def handler(user, db, args):
        raise RuntimeError("boom")

    tool = ToolDefinition(
        name="boom",
        description="x",
        parameters_schema={"type": "object"},
        handler=handler,
    )
    reg = ToolRegistry()
    reg.register(tool)

    from app.ai.tools.orchestrator import CollectedToolCall

    tc = CollectedToolCall(id="c1", name="boom", arguments_json="{}")
    result = await execute_tool_call(tc, registry=reg, user=MagicMock(), db=MagicMock())
    assert result.success is False
    assert result.error["code"] == "TOOL_INTERNAL_ERROR"


# ───────────────────────────────────────────────────────────────────
# build_tool_messages_for_next_round
# ───────────────────────────────────────────────────────────────────


def test_build_tool_messages_injects_summary_and_results():
    from app.ai.tools.orchestrator import CollectedToolCall, ToolRoundResult

    tc = CollectedToolCall(id="c1", name="echo", arguments_json='{"a":1}')
    tr = ToolResult(success=True, data={"ok": True})
    round_result = ToolRoundResult(
        tool_calls=[tc],
        results=[(tc, tr)],
        finished_with_tool_calls=True,
    )
    messages = build_tool_messages_for_next_round(round_result)
    assert len(messages) == 2
    assert messages[0].role == "assistant"
    assert "echo" in messages[0].content
    assert messages[1].role == "user"
    payload = json.loads(messages[1].content.split("\n", 1)[1])
    assert payload["success"] is True
    assert payload["data"] == {"ok": True}


# ───────────────────────────────────────────────────────────────────
# run_with_tool_rounds (loop intégrale)
# ───────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_with_tool_rounds_single_round_no_tools_stops():
    async def _stream(messages) -> AsyncIterator[ChatChunk]:
        yield ChatChunk(delta="hello")
        yield _mk_done_stop()

    reg = ToolRegistry()
    emitted: list[ChatChunk] = []
    async for chunk in run_with_tool_rounds(
        initial_messages=[],
        stream_factory=_stream,
        registry=reg,
        user=MagicMock(),
        db=MagicMock(),
        max_rounds=3,
    ):
        emitted.append(chunk)
    assert any(c.delta == "hello" for c in emitted)


@pytest.mark.asyncio
async def test_run_with_tool_rounds_executes_tool_then_second_round():
    async def handler(user, db, args):
        return ToolResult(success=True, data={"created": args.get("title")})

    tool = ToolDefinition(
        name="create_task",
        description="create",
        parameters_schema={"type": "object"},
        handler=handler,
    )
    reg = ToolRegistry()
    reg.register(tool)

    call_count = {"n": 0}

    async def _stream(messages) -> AsyncIterator[ChatChunk]:
        call_count["n"] += 1
        if call_count["n"] == 1:
            yield _mk_tool_call_chunk(name="create_task", args_json='{"title":"x"}')
            yield _mk_done_tool_calls()
        else:
            yield ChatChunk(delta="Fait.")
            yield _mk_done_stop()

    chunks = []
    async for c in run_with_tool_rounds(
        initial_messages=[],
        stream_factory=_stream,
        registry=reg,
        user=MagicMock(),
        db=MagicMock(),
        max_rounds=5,
    ):
        chunks.append(c)

    assert call_count["n"] == 2  # deux rounds
    assert any(c.delta == "Fait." for c in chunks)
    # Le tool_call du round 1 doit avoir été yield
    assert any(c.tool_call is not None for c in chunks)


@pytest.mark.asyncio
async def test_run_with_tool_rounds_caps_at_max_rounds():
    async def handler(user, db, args):
        return ToolResult(success=True, data={})

    tool = ToolDefinition(
        name="loop",
        description="loop",
        parameters_schema={"type": "object"},
        handler=handler,
    )
    reg = ToolRegistry()
    reg.register(tool)

    call_count = {"n": 0}

    async def _stream(messages) -> AsyncIterator[ChatChunk]:
        call_count["n"] += 1
        # Jamais stop — toujours tool_call. Simule un LLM buggé.
        yield _mk_tool_call_chunk(name="loop", args_json="{}")
        yield _mk_done_tool_calls()

    async for _ in run_with_tool_rounds(
        initial_messages=[],
        stream_factory=_stream,
        registry=reg,
        user=MagicMock(),
        db=MagicMock(),
        max_rounds=3,
    ):
        pass

    # Au max 3 rounds, pas plus — sinon boucle infinie
    assert call_count["n"] == 3
