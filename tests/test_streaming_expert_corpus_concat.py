"""
Tests unitaires — concat `memory → corpus → system` dans `_stream_link` (G1).

Valide que la `StreamContext.expert_corpus_context` est bien concaténée
dans le bon ordre avant le system prompt expert, et que le bloc est
effectivement passé au provider via `ChatCompletionRequest.system_prompt`.

On instancie un `MockChatProvider` et on capture le `system_prompt` reçu.
"""

from __future__ import annotations

import pytest

from app.ai.providers import (
    ChatCompletionRequest,
    ChatMessage,
    MockChatProvider,
)
from app.ai.router import LlmRouter
from app.ai.streaming import StreamContext, StreamHandler


class _FakeRequest:
    """Simule `fastapi.Request.is_disconnected`."""

    async def is_disconnected(self) -> bool:
        return False


def _build_handler() -> tuple[StreamHandler, MockChatProvider]:
    from app.ai.experts import get_expert_config

    cfg = get_expert_config("language")
    provider = MockChatProvider(
        name=cfg.primary_provider,
        default_model=cfg.primary_model,
        supported_models={cfg.primary_model},
        max_context_tokens=128_000,
    )
    # On intercepte les appels stream_chat pour capturer la request.
    router = LlmRouter(chat_providers={cfg.primary_provider: provider})
    handler = StreamHandler(router=router)
    return handler, provider


@pytest.mark.asyncio
async def test_concat_order_memory_then_corpus_then_system() -> None:
    handler, provider = _build_handler()
    captured: dict = {}

    original_stream = provider.stream_chat

    async def capture(request: ChatCompletionRequest):
        captured["system_prompt"] = request.system_prompt
        async for chunk in original_stream(request):
            yield chunk

    provider.stream_chat = capture  # type: ignore[assignment]

    ctx = StreamContext(
        expert_id="language",
        user_messages=[ChatMessage(role="user", content="hello")],
        user_id="user-1",
        trace_id="trace-1",
        memory_context="[MEMORY_BLOCK]",
        expert_corpus_context="[CORPUS_BLOCK]",
    )

    events: list[str] = []
    async for evt in handler.stream(_FakeRequest(), ctx):  # type: ignore[arg-type]
        events.append(evt)

    system_prompt = captured["system_prompt"]
    assert system_prompt is not None
    # Ordre strict : mémoire avant corpus avant system prompt.
    mem_idx = system_prompt.index("[MEMORY_BLOCK]")
    corpus_idx = system_prompt.index("[CORPUS_BLOCK]")
    assert mem_idx < corpus_idx
    # Le system prompt expert doit venir après les deux.
    expert_marker = "NEXYA"  # présent dans `_NEXYA_IDENTITY`
    expert_idx = system_prompt.index(expert_marker)
    assert corpus_idx < expert_idx


@pytest.mark.asyncio
async def test_concat_only_corpus_present_no_memory() -> None:
    handler, provider = _build_handler()
    captured: dict = {}
    original = provider.stream_chat

    async def capture(request: ChatCompletionRequest):
        captured["system_prompt"] = request.system_prompt
        async for chunk in original(request):
            yield chunk

    provider.stream_chat = capture  # type: ignore[assignment]

    ctx = StreamContext(
        expert_id="language",
        user_messages=[ChatMessage(role="user", content="hello")],
        user_id="u1",
        trace_id="t1",
        memory_context=None,
        expert_corpus_context="[CORPUS_ONLY]",
    )
    async for _ in handler.stream(_FakeRequest(), ctx):  # type: ignore[arg-type]
        pass

    sp = captured["system_prompt"]
    assert "[CORPUS_ONLY]" in sp
    assert sp.startswith("[CORPUS_ONLY]")


@pytest.mark.asyncio
async def test_concat_no_extras_falls_back_to_raw_system_prompt() -> None:
    handler, provider = _build_handler()
    captured: dict = {}
    original = provider.stream_chat

    async def capture(request: ChatCompletionRequest):
        captured["system_prompt"] = request.system_prompt
        async for chunk in original(request):
            yield chunk

    provider.stream_chat = capture  # type: ignore[assignment]

    ctx = StreamContext(
        expert_id="language",
        user_messages=[ChatMessage(role="user", content="hello")],
        user_id="u1",
        trace_id="t1",
        memory_context=None,
        expert_corpus_context=None,
    )
    async for _ in handler.stream(_FakeRequest(), ctx):  # type: ignore[arg-type]
        pass

    # Pas de marqueurs additionnels injectés.
    sp = captured["system_prompt"]
    assert "[MEMORY_BLOCK]" not in sp
    assert "[CORPUS_BLOCK]" not in sp
