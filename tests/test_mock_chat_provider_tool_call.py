"""
Tests extension MockChatProvider — F2 scripted_tool_call.

Couvre :
- scripted_tool_call yield un ChatChunk avec tool_call délta
- Le dernier chunk porte finish_reason=TOOL_CALLS
- Pas de texte yield quand scripted_tool_call est actif
- ChatCompletionRequest peut accepter `tools` sans crash (rétro-compat)
"""

from __future__ import annotations

import pytest

from app.ai.providers import (
    ChatCompletionRequest,
    ChatMessage,
    FinishReason,
    MockChatProvider,
)


@pytest.mark.asyncio
async def test_scripted_tool_call_yields_tool_call_then_finish():
    provider = MockChatProvider(
        scripted_tool_call={
            "id": "call_1",
            "name": "create_task",
            "arguments": {"title": "demo"},
        }
    )
    request = ChatCompletionRequest(
        messages=[ChatMessage(role="user", content="plan ça")],
        model="mock-default",
    )
    chunks = []
    async for chunk in provider.stream_chat(request):
        chunks.append(chunk)

    # 2 chunks attendus : 1 tool_call delta + 1 finish TOOL_CALLS
    assert len(chunks) == 2
    assert chunks[0].tool_call is not None
    assert chunks[0].tool_call.name == "create_task"
    assert chunks[0].tool_call.id == "call_1"
    assert chunks[0].delta == ""
    # arguments_json_partial est le JSON sérialisé
    import json as _json

    assert _json.loads(chunks[0].tool_call.arguments_json_partial) == {"title": "demo"}
    assert chunks[1].finish_reason == FinishReason.TOOL_CALLS


@pytest.mark.asyncio
async def test_mock_without_tool_call_still_yields_text():
    provider = MockChatProvider()
    request = ChatCompletionRequest(
        messages=[ChatMessage(role="user", content="hi")],
        model="mock-default",
    )
    chunks = []
    async for chunk in provider.stream_chat(request):
        chunks.append(chunk)
    assert len(chunks) >= 2
    assert chunks[-1].finish_reason == FinishReason.STOP
    # Aucun chunk n'a de tool_call
    assert all(c.tool_call is None for c in chunks)


def test_chat_completion_request_accepts_tools_field():
    # Rétro-compat : tools optionnel, defaults None.
    req_without = ChatCompletionRequest(messages=[ChatMessage(role="user", content="x")])
    assert req_without.tools is None

    req_with = ChatCompletionRequest(
        messages=[ChatMessage(role="user", content="x")],
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "foo",
                    "description": "bar",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )
    assert req_with.tools is not None
    assert len(req_with.tools) == 1
    assert req_with.tools[0]["function"]["name"] == "foo"
