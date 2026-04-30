"""
Tests unitaires B4 — `app.ai.engine.query_engine`.

Couvre :
1. `DONE_REASON_TO_STATUS` : mapping 1:1 aligné sur le CHECK SQL.
2. `StreamOutcome` : valeurs par défaut, `final_status()` + `final_content()`.
3. `observe_sse_event` : chunk / done / error / keepalive / malformé.
4. `QueryEngine.run()` : yield pass-through + outcome peuplé à la volée.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.ai.engine.query_engine import (
    DONE_REASON_TO_STATUS,
    QueryEngine,
    StreamOutcome,
    observe_sse_event,
)

# ══════════════════════════════════════════════════════════════
# 1. Mapping SSE → SQL
# ══════════════════════════════════════════════════════════════


def test_done_reason_mapping_aligned_on_check_sql() -> None:
    assert DONE_REASON_TO_STATUS == {
        "stop": "completed",
        "cancelled": "cancelled",
        "error": "failed",
    }


# ══════════════════════════════════════════════════════════════
# 2. StreamOutcome — defaults + helpers
# ══════════════════════════════════════════════════════════════


def test_stream_outcome_defaults() -> None:
    out = StreamOutcome()
    assert out.done_reason == "error"
    assert out.error_code is None
    assert out.content_parts == []


def test_final_status_returns_failed_for_unknown_reason() -> None:
    out = StreamOutcome(done_reason="weird")
    assert out.final_status() == "failed"


def test_final_status_stop_is_completed() -> None:
    out = StreamOutcome(done_reason="stop")
    assert out.final_status() == "completed"


def test_final_content_joins_parts_in_order() -> None:
    out = StreamOutcome(content_parts=["Bon", "jour", " !"])
    assert out.final_content() == "Bonjour !"


# ══════════════════════════════════════════════════════════════
# 3. observe_sse_event — parsing
# ══════════════════════════════════════════════════════════════


def test_observe_chunk_event_appends_delta() -> None:
    out = StreamOutcome()
    observe_sse_event('event: chunk\ndata: {"delta":"Bon"}\n\n', out)
    observe_sse_event('event: chunk\ndata: {"delta":"jour"}\n\n', out)
    assert out.final_content() == "Bonjour"


def test_observe_done_event_sets_reason() -> None:
    out = StreamOutcome()
    observe_sse_event('event: done\ndata: {"reason":"stop"}\n\n', out)
    assert out.done_reason == "stop"
    assert out.final_status() == "completed"


def test_observe_error_event_captures_code_and_done_marks_failed() -> None:
    out = StreamOutcome()
    observe_sse_event('event: error\ndata: {"code":"LLM_UNAVAILABLE","message":"x"}\n\n', out)
    observe_sse_event('event: done\ndata: {"reason":"error"}\n\n', out)
    assert out.error_code == "LLM_UNAVAILABLE"
    assert out.final_status() == "failed"


def test_observe_keepalive_comment_is_ignored() -> None:
    out = StreamOutcome()
    observe_sse_event(": keepalive\n\n", out)
    assert out.done_reason == "error"
    assert out.error_code is None
    assert out.content_parts == []


def test_observe_malformed_json_is_swallowed() -> None:
    out = StreamOutcome()
    observe_sse_event("event: chunk\ndata: not-json\n\n", out)
    assert out.content_parts == []


def test_observe_event_missing_data_line_is_ignored() -> None:
    out = StreamOutcome()
    observe_sse_event("event: done\n\n", out)
    assert out.done_reason == "error"


def test_observe_chunk_with_non_string_delta_ignored() -> None:
    out = StreamOutcome()
    observe_sse_event('event: chunk\ndata: {"delta":42}\n\n', out)
    assert out.content_parts == []


# ══════════════════════════════════════════════════════════════
# 4. QueryEngine.run — yield + outcome peuplé
# ══════════════════════════════════════════════════════════════


class _FakeStreamHandler:
    """Handler fake qui yield une séquence d'events SSE pré-cannée."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    def stream(self, request: Any, ctx: Any) -> AsyncIterator[str]:
        async def _gen() -> AsyncIterator[str]:
            for event in self._events:
                yield event

        return _gen()


@pytest.mark.asyncio
async def test_query_engine_run_yields_events_and_populates_outcome() -> None:
    handler = _FakeStreamHandler(
        events=[
            'event: chunk\ndata: {"delta":"Hello"}\n\n',
            'event: chunk\ndata: {"delta":" world"}\n\n',
            'event: done\ndata: {"reason":"stop"}\n\n',
        ]
    )
    engine = QueryEngine(handler=handler)  # type: ignore[arg-type]
    outcome = StreamOutcome()
    received = []
    async for event in engine.run(MagicMock(), MagicMock(), outcome=outcome):
        received.append(event)
    assert len(received) == 3
    assert outcome.final_content() == "Hello world"
    assert outcome.final_status() == "completed"


@pytest.mark.asyncio
async def test_query_engine_run_records_error_code_on_error_event() -> None:
    handler = _FakeStreamHandler(
        events=[
            'event: error\ndata: {"code":"STREAM_CANCELLED","message":"stop"}\n\n',
            'event: done\ndata: {"reason":"cancelled"}\n\n',
        ]
    )
    engine = QueryEngine(handler=handler)  # type: ignore[arg-type]
    outcome = StreamOutcome()
    async for _ in engine.run(MagicMock(), MagicMock(), outcome=outcome):
        pass
    assert outcome.error_code == "STREAM_CANCELLED"
    assert outcome.final_status() == "cancelled"


@pytest.mark.asyncio
async def test_query_engine_run_pass_through_of_keepalive_events() -> None:
    handler = _FakeStreamHandler(
        events=[
            ": keepalive\n\n",
            'event: chunk\ndata: {"delta":"hi"}\n\n',
            ": keepalive\n\n",
            'event: done\ndata: {"reason":"stop"}\n\n',
        ]
    )
    engine = QueryEngine(handler=handler)  # type: ignore[arg-type]
    outcome = StreamOutcome()
    received = []
    async for event in engine.run(MagicMock(), MagicMock(), outcome=outcome):
        received.append(event)
    assert received.count(": keepalive\n\n") == 2
    assert outcome.final_content() == "hi"
