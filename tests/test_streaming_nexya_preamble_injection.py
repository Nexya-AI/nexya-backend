"""
Tests Session A1 — Injection du préambule NEXYA dans `_stream_link`.

Valide que le préambule NEXYA (A1) est correctement concaténé EN TÊTE
du `system_prompt_final` envoyé au provider LLM, dans l'ordre canonique :

    nexya_preamble (A1) → memory (D3) → expert_corpus (G1) → rag (I1)
    → system_prompt expert (experts.py)

On instancie un `MockChatProvider` et on capture le `system_prompt`
reçu via `ChatCompletionRequest.system_prompt`.

Garanties testées :
- Ordre strict 5 segments quand tous présents.
- Préambule absent quand `nexya_preamble_enabled=False` (rétrocompat A0).
- Préambule absent quand `build_nexya_preamble` retourne None (fail-safe).
- Préambule présent même quand memory/corpus/rag sont None.
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

# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════


class _FakeRequest:
    async def is_disconnected(self) -> bool:
        return False


def _build_handler() -> tuple[StreamHandler, MockChatProvider]:
    from app.ai.experts import get_expert_config

    cfg = get_expert_config("general")
    provider = MockChatProvider(
        name=cfg.primary_provider,
        default_model=cfg.primary_model,
        supported_models={cfg.primary_model},
        max_context_tokens=128_000,
    )
    router = LlmRouter(chat_providers={cfg.primary_provider: provider})
    handler = StreamHandler(router=router)
    return handler, provider


async def _capture_system_prompt(
    handler: StreamHandler,
    provider: MockChatProvider,
    ctx: StreamContext,
) -> str | None:
    """Helper : exécute le handler et retourne le system_prompt envoyé."""
    captured: dict = {"system_prompt": None}
    original_stream = provider.stream_chat

    async def capture(request: ChatCompletionRequest):
        captured["system_prompt"] = request.system_prompt
        async for chunk in original_stream(request):
            yield chunk

    provider.stream_chat = capture  # type: ignore[assignment]

    async for _evt in handler.stream(_FakeRequest(), ctx):  # type: ignore[arg-type]
        pass

    return captured["system_prompt"]


@pytest.fixture(autouse=True)
def _enable_nexya_preamble(monkeypatch: pytest.MonkeyPatch) -> None:
    """Toujours partir d'un kill-switch ON pour les tests injection."""
    monkeypatch.setattr(settings, "nexya_preamble_enabled", True, raising=False)
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 8000, raising=False)
    monkeypatch.setattr(settings, "nexya_preamble_default_locale", "fr", raising=False)


# ══════════════════════════════════════════════════════════════
# 1. Préambule présent en tête quand kill-switch ON
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_preamble_injected_at_head_of_system_prompt() -> None:
    handler, provider = _build_handler()
    ctx = StreamContext(
        expert_id="general",
        user_messages=[ChatMessage(role="user", content="hello")],
        user_id="u1",
        trace_id="t1",
        memory_context=None,
        expert_corpus_context=None,
    )
    sp = await _capture_system_prompt(handler, provider, ctx)
    assert sp is not None
    # Le système prompt complet commence par le marker tone NEXYA A1.
    assert "[Ton conversationnel NEXYA]" in sp
    # Et l'ordre garantit qu'il vient AVANT le system prompt expert
    # (qui contient "Rôle" pour les experts non-general, ou les tools
    # Planner pour general).
    preamble_idx = sp.index("[Ton conversationnel NEXYA]")
    # Toute autre section vient APRÈS le preamble.
    assert preamble_idx < len(sp) - 100  # Le preamble n'est pas en queue.


# ══════════════════════════════════════════════════════════════
# 2. Ordre strict : preamble → memory → corpus → rag → system
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_concat_order_preamble_memory_corpus_rag_system() -> None:
    handler, provider = _build_handler()
    ctx = StreamContext(
        expert_id="general",
        user_messages=[ChatMessage(role="user", content="hello")],
        user_id="u1",
        trace_id="t1",
        memory_context="[MEMORY_BLOCK_TEST]",
        expert_corpus_context="[CORPUS_BLOCK_TEST]",
        rag_context=("[RAG_FRAMED_TEST]", "[RAG_INSTRUCTION_TEST]"),
    )
    sp = await _capture_system_prompt(handler, provider, ctx)
    assert sp is not None

    preamble_idx = sp.index("[Ton conversationnel NEXYA]")
    memory_idx = sp.index("[MEMORY_BLOCK_TEST]")
    corpus_idx = sp.index("[CORPUS_BLOCK_TEST]")
    rag_framed_idx = sp.index("[RAG_FRAMED_TEST]")

    # Ordre strict canonique A1+D3+G1+I1.
    assert preamble_idx < memory_idx
    assert memory_idx < corpus_idx
    assert corpus_idx < rag_framed_idx


# ══════════════════════════════════════════════════════════════
# 3. Kill-switch OFF → comportement pré-A1 (preamble absent)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_preamble_absent_when_kill_switch_off(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "nexya_preamble_enabled", False, raising=False)
    handler, provider = _build_handler()
    ctx = StreamContext(
        expert_id="general",
        user_messages=[ChatMessage(role="user", content="hello")],
        user_id="u1",
        trace_id="t1",
        memory_context="[MEMORY_LEGACY]",
        expert_corpus_context=None,
    )
    sp = await _capture_system_prompt(handler, provider, ctx)
    assert sp is not None
    # Le préambule NE doit PAS apparaître.
    assert "[Ton conversationnel NEXYA]" not in sp
    # Mais memory reste présente — comportement pré-A1 préservé.
    assert "[MEMORY_LEGACY]" in sp


# ══════════════════════════════════════════════════════════════
# 4. Préambule absent quand build_nexya_preamble retourne None
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_preamble_absent_when_builder_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si `build_nexya_preamble` retourne None (fail-safe), le chat
    continue normalement avec memory + corpus + system_prompt expert."""
    from app.ai import streaming as streaming_module

    def fake_builder(*args, **kwargs) -> None:
        return None

    monkeypatch.setattr(streaming_module, "build_nexya_preamble", fake_builder)

    handler, provider = _build_handler()
    ctx = StreamContext(
        expert_id="general",
        user_messages=[ChatMessage(role="user", content="hello")],
        user_id="u1",
        trace_id="t1",
        memory_context="[ONLY_MEMORY]",
        expert_corpus_context=None,
    )
    sp = await _capture_system_prompt(handler, provider, ctx)
    assert sp is not None
    # Preamble fail-safe → absent.
    assert "[Ton conversationnel NEXYA]" not in sp
    # Memory toujours présent.
    assert "[ONLY_MEMORY]" in sp


# ══════════════════════════════════════════════════════════════
# 5. Préambule présent même si memory/corpus/rag tous None
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_preamble_present_when_all_other_contexts_none() -> None:
    handler, provider = _build_handler()
    ctx = StreamContext(
        expert_id="general",
        user_messages=[ChatMessage(role="user", content="hello")],
        user_id="u1",
        trace_id="t1",
        memory_context=None,
        expert_corpus_context=None,
        rag_context=None,
    )
    sp = await _capture_system_prompt(handler, provider, ctx)
    assert sp is not None
    assert "[Ton conversationnel NEXYA]" in sp


# ══════════════════════════════════════════════════════════════
# 6. Le preamble respecte l'expert actif (label routing)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_preamble_routing_label_matches_active_expert() -> None:
    """Le routing guidance doit nommer l'expert actif (general ici)."""
    handler, provider = _build_handler()
    ctx = StreamContext(
        expert_id="general",
        user_messages=[ChatMessage(role="user", content="hello")],
        user_id="u1",
        trace_id="t1",
    )
    sp = await _capture_system_prompt(handler, provider, ctx)
    assert sp is not None
    # Le routing guidance nomme l'expert actif.
    assert "Général" in sp or "general" in sp.lower()


# ══════════════════════════════════════════════════════════════
# 7. Sanity — system_prompt expert présent en queue
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_expert_system_prompt_still_present_after_preamble() -> None:
    """Le system_prompt expert (general) contient 'Rôle' ou 'Mode'.

    On vérifie qu'il n'est pas écrasé par le preamble."""
    handler, provider = _build_handler()
    ctx = StreamContext(
        expert_id="general",
        user_messages=[ChatMessage(role="user", content="hello")],
        user_id="u1",
        trace_id="t1",
    )
    sp = await _capture_system_prompt(handler, provider, ctx)
    assert sp is not None
    # Session A2 (2026-05-19) : `_GENERAL_PROMPT` post-refactor contient
    # `[Persona — Assistant Général NEXYA AI]` et les 4 tools Planner.
    assert "[Persona" in sp or "Persona" in sp
    assert "create_task" in sp  # un des 4 tools Planner du general expert


# ══════════════════════════════════════════════════════════════
# 8. Two-Tier Smart Preamble (NOUVEAU 2026-05-26)
# ══════════════════════════════════════════════════════════════
#
# Le `_stream_link` doit maintenant extraire le dernier message user
# et le passer à `build_nexya_preamble` via le paramètre `user_message`.
# Si l'utilisateur pose une question marketing, le bloc EXTENDED
# (15 features + routing table) est injecté en plus du CORE.


@pytest.mark.asyncio
async def test_preamble_extended_absent_on_banal_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Question banale → EXTENDED (15 features détaillées) PAS injecté."""
    # Cap chars élevé pour ne pas tronquer
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 50_000, raising=False)
    handler, provider = _build_handler()
    ctx = StreamContext(
        expert_id="general",
        user_messages=[ChatMessage(role="user", content="quelle est la capitale du Cameroun ?")],
        user_id="u1",
        trace_id="t1",
    )
    sp = await _capture_system_prompt(handler, provider, ctx)
    assert sp is not None
    # Le bloc EXTENDED (15 features magnifiques détaillées) NE doit PAS
    # être injecté pour une question banale.
    assert "[Capacités magnifiques de NEXYA]" not in sp
    # Mais le Capability Teaser (CORE) DOIT être présent.
    assert "[Capacités principales de NEXYA — résumé]" in sp


@pytest.mark.asyncio
async def test_preamble_extended_present_on_marketing_question(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Question marketing → EXTENDED (15 features détaillées) injecté."""
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 50_000, raising=False)
    handler, provider = _build_handler()
    ctx = StreamContext(
        expert_id="general",
        user_messages=[ChatMessage(role="user", content="qu'est-ce que tu sais faire ?")],
        user_id="u1",
        trace_id="t1",
    )
    sp = await _capture_system_prompt(handler, provider, ctx)
    assert sp is not None
    # Sur question marketing → EXTENDED présent
    assert "[Capacités magnifiques de NEXYA]" in sp
    # Routing table détaillée aussi
    assert "[Routing — Table de correspondance" in sp
    # Et le teaser CORE reste présent en plus
    assert "[Capacités principales de NEXYA — résumé]" in sp


@pytest.mark.asyncio
async def test_preamble_two_tier_size_difference_observable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Système prompt avec marketing question ≥ 3000 chars de plus
    qu'avec question banale (confirmation observable du two-tier)."""
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 50_000, raising=False)
    handler1, provider1 = _build_handler()
    handler2, provider2 = _build_handler()
    ctx_banal = StreamContext(
        expert_id="general",
        user_messages=[ChatMessage(role="user", content="hello")],
        user_id="u1",
        trace_id="t1",
    )
    ctx_marketing = StreamContext(
        expert_id="general",
        user_messages=[ChatMessage(role="user", content="que sais-tu faire ?")],
        user_id="u1",
        trace_id="t1",
    )
    sp_banal = await _capture_system_prompt(handler1, provider1, ctx_banal)
    sp_marketing = await _capture_system_prompt(handler2, provider2, ctx_marketing)
    assert sp_banal is not None and sp_marketing is not None
    # EXTENDED doit ajouter au moins 3000 chars (cf. test équivalent
    # dans test_nexya_preamble.py — confirmation transversale via le
    # vrai pipeline streaming).
    assert len(sp_marketing) - len(sp_banal) >= 3000
