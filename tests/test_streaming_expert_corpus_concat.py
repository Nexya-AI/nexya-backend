"""
Tests unitaires — concat `memory → corpus → system` dans `_stream_link` (G1).

Valide que la `StreamContext.expert_corpus_context` est bien concaténée
dans le bon ordre avant le system prompt expert, et que le bloc est
effectivement passé au provider via `ChatCompletionRequest.system_prompt`.

On instancie un `MockChatProvider` et on capture le `system_prompt` reçu.

Session A1 (2026-05-19) — Adaptation post-preamble : ces tests valident
la logique de concat G1 (memory → corpus → system_prompt expert)
**indépendamment** du préambule NEXYA injecté en tête par A1. Pour
préserver l'intent original sans casser sur l'ajout du preamble, on
désactive `settings.nexya_preamble_enabled` via fixture autouse.

Note (planner-from-chat LOT 2) : le bloc `[Contexte temporel]` est
désormais TOUJOURS injecté en tête (juste après le préambule, donc en
1ʳᵉ position quand le préambule est désactivé). Les tests vérifient donc
l'**ordre relatif** memory < corpus < system_prompt expert, jamais un
`startswith` sur un bloc optionnel.
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
from app.config import settings


@pytest.fixture(autouse=True)
def _disable_nexya_preamble(monkeypatch: pytest.MonkeyPatch) -> None:
    """Session A1 — désactive le preamble NEXYA pour isoler la concat G1.

    Le preamble est validé par sa propre suite de tests
    (`test_nexya_preamble.py` + `test_streaming_nexya_preamble_injection.py`).
    Ici on ne teste QUE l'ordre memory → corpus → system_prompt expert.
    """
    monkeypatch.setattr(settings, "nexya_preamble_enabled", False, raising=False)


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
    # Session A1 : on cherche un marker spécifique au prompt `language`
    # (post-cleanup `_NEXYA_IDENTITY=""`, le marker "NEXYA" n'est plus
    # dans le prompt expert, il vit désormais dans le preamble A1 — ici
    # désactivé via fixture autouse).
    expert_marker = "Expert Langues"
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
    # Aucun bloc mémoire injecté (memory_context=None).
    assert "[MEMORY_BLOCK]" not in sp
    # Le corpus précède le system prompt expert. On ne teste plus
    # `startswith` : le bloc `[Contexte temporel]` (LOT 2) est désormais
    # toujours injecté en tête.
    assert sp.index("[CORPUS_ONLY]") < sp.index("Expert Langues")


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
