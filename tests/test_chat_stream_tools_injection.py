"""
F2.5 — Le router `/chat/stream` injecte `tool_registry.build_openai_tools()`
dans `StreamContext.tools` quand le kill-switch global est ON et que
l'expert courant autorise les tools.

Garde-fous testés :
1. Le `StreamContext` passé au handler porte `tools` peuplé quand tout est OK.
2. `settings.tools_enabled_in_chat=False` → `ctx.tools is None`.
3. `ExpertConfig.tools_allowed=False` (medical, legal) → `ctx.tools is None`.
4. Registry vide → `ctx.tools is None` (pas une liste vide).
5. Mode legacy stateless ET mode persisté propagent tous deux le champ.

Pas de Postgres, pas de Redis, pas d'IA réelle — `StreamHandler.stream` est
un fake qui capture le `ctx` reçu pour assertion.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.ai.streaming import StreamContext
from app.ai.tools import get_tool_registry, reset_tool_registry_for_tests
from app.ai.tools.base import ToolDefinition, ToolResult
from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.chat import router as chat_router_module
from app.features.chat.models import Conversation, Message
from app.features.chat.service import ConversationService
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _register_dummy_tool() -> None:
    """Place 1 tool dans le registry pour avoir un payload non-vide."""
    reset_tool_registry_for_tests()

    async def _handler(user, db, args):
        return ToolResult(success=True, data={})

    get_tool_registry().register(
        ToolDefinition(
            name="dummy_tool",
            description="Tool factice pour tests F2.5.",
            parameters_schema={"type": "object", "properties": {}},
            handler=_handler,
        )
    )


@pytest.fixture(autouse=True)
def _registry_isolation():
    """Garantit qu'un registry vide en début + fin de chaque test."""
    reset_tool_registry_for_tests()
    yield
    reset_tool_registry_for_tests()


@pytest.fixture
def client():
    fake_user = _make_fake_user()
    fake_db = MagicMock()

    async def _user_override():
        return fake_user

    async def _db_override():
        yield fake_db

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_db] = _db_override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def _install_minimal_ai(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stubs budget + modération + modération métier."""
    budget = MagicMock()
    budget.check_and_consume_chat = AsyncMock(return_value=None)
    decision = MagicMock()
    decision.allowed = True
    moderation = MagicMock()
    moderation.check = AsyncMock(return_value=decision)
    monkeypatch.setattr(chat_router_module, "get_budget_tracker", lambda: budget)
    monkeypatch.setattr(chat_router_module, "get_moderation_service", lambda: moderation)
    monkeypatch.setattr(
        chat_router_module,
        "build_memory_context",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        chat_router_module,
        "build_expert_corpus_context",
        AsyncMock(return_value=None),
    )

    # estimateur tokens : retourne un usage très petit pour passer le cap.
    estimate = MagicMock(prompt_tokens=10, completion_tokens=0, total_tokens=10)
    monkeypatch.setattr(chat_router_module, "estimate_tokens", lambda **kw: estimate)


def _install_capture_handler(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Remplace `get_stream_handler()` par un fake qui capture le `ctx`."""
    captured: dict = {}

    async def _fake_stream(request, ctx: StreamContext):
        captured["ctx"] = ctx
        # Un seul SSE pour fermer proprement.
        yield 'event: done\ndata: {"reason":"stop"}\n\n'

    fake_handler = MagicMock()
    fake_handler.stream = _fake_stream
    monkeypatch.setattr(chat_router_module, "get_stream_handler", lambda: fake_handler)
    return captured


# ══════════════════════════════════════════════════════════════════════
# 1. Mode legacy stateless — tools injectés
# ══════════════════════════════════════════════════════════════════════


def test_legacy_stream_injects_tools_when_setting_on_and_expert_allows(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """`expert_id='general'` → `tools_allowed=True`, registry peuplé →
    `ctx.tools` doit contenir la définition du `dummy_tool` au format OpenAI."""
    _register_dummy_tool()
    _install_minimal_ai(monkeypatch)
    captured = _install_capture_handler(monkeypatch)

    monkeypatch.setattr(chat_router_module.settings, "tools_enabled_in_chat", True)

    resp = client.post(
        "/chat/stream",
        json={
            "message": "Salut",
            "history": [{"role": "assistant", "content": "Je suis prêt."}],
            "expert_id": "general",
        },
    )
    assert resp.status_code == 200
    ctx = captured["ctx"]
    assert ctx.tools is not None
    assert isinstance(ctx.tools, list)
    assert any(t.get("function", {}).get("name") == "dummy_tool" for t in ctx.tools)


def test_legacy_stream_skips_tools_when_kill_switch_off(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    _register_dummy_tool()
    _install_minimal_ai(monkeypatch)
    captured = _install_capture_handler(monkeypatch)

    monkeypatch.setattr(chat_router_module.settings, "tools_enabled_in_chat", False)

    resp = client.post(
        "/chat/stream",
        json={
            "message": "Salut",
            "history": [{"role": "assistant", "content": "Je suis prêt."}],
            "expert_id": "general",
        },
    )
    assert resp.status_code == 200
    assert captured["ctx"].tools is None


def test_legacy_stream_skips_tools_for_safety_critical_experts(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """`expert_id='medicine'` → `tools_allowed=False` → `ctx.tools is None`
    même si le kill-switch global est ON."""
    _register_dummy_tool()
    _install_minimal_ai(monkeypatch)
    captured = _install_capture_handler(monkeypatch)

    monkeypatch.setattr(chat_router_module.settings, "tools_enabled_in_chat", True)

    resp = client.post(
        "/chat/stream",
        json={
            "message": "Quel est le traitement de l'hypertension ?",
            "history": [{"role": "assistant", "content": "Je suis prêt."}],
            "expert_id": "medicine",
        },
    )
    assert resp.status_code == 200
    assert captured["ctx"].tools is None


def test_legacy_stream_skips_tools_when_registry_empty(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Registry vide → `build_openai_tools()` retourne `[]` → `ctx.tools is None`."""
    # Le lifespan d'`app` (TestClient) appelle `register_planner_tools()` au
    # startup et peuple les 4 tools Planner. On force le registry à vide
    # APRÈS le démarrage du client pour ce test précis.
    reset_tool_registry_for_tests()
    _install_minimal_ai(monkeypatch)
    captured = _install_capture_handler(monkeypatch)

    monkeypatch.setattr(chat_router_module.settings, "tools_enabled_in_chat", True)

    resp = client.post(
        "/chat/stream",
        json={
            "message": "Salut",
            "history": [{"role": "assistant", "content": "Je suis prêt."}],
            "expert_id": "general",
        },
    )
    assert resp.status_code == 200
    # Pas de [] vide non plus — on préfère None pour être strict côté providers.
    assert captured["ctx"].tools is None


# ══════════════════════════════════════════════════════════════════════
# 2. Mode persisté — tools propagés au StreamContext persisté
# ══════════════════════════════════════════════════════════════════════


def _make_fake_conversation() -> Conversation:
    now = datetime(2026, 4, 25, 14, 30, 0, tzinfo=UTC)
    conv = Conversation(user_id=_FAKE_USER_ID, title=None, expert_id="general")
    conv.id = uuid.UUID("a1b2c3d4-0000-4000-8000-000000000042")
    conv.last_message_at = None
    conv.message_count = 0
    conv.is_archived = False
    conv.is_favorite = False
    conv.title_generated_at = None
    conv.deleted_at = None
    conv.created_at = now
    conv.updated_at = now
    return conv


def _make_fake_message() -> Message:
    msg = Message(
        conversation_id=uuid.UUID("a1b2c3d4-0000-4000-8000-000000000042"),
        role="assistant",
        content="",
        status="streaming",
    )
    msg.id = uuid.uuid4()
    return msg


def test_persisted_stream_propagates_tools_to_stream_context(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """En mode persisté (history vide, conversation_id=None), les tools doivent
    aussi atteindre le StreamContext."""
    _register_dummy_tool()
    _install_minimal_ai(monkeypatch)
    captured = _install_capture_handler(monkeypatch)

    monkeypatch.setattr(chat_router_module.settings, "tools_enabled_in_chat", True)

    fake_conv = _make_fake_conversation()
    fake_msg = _make_fake_message()
    monkeypatch.setattr(
        ConversationService,
        "ensure_conversation_for_stream",
        AsyncMock(return_value=fake_conv),
    )
    monkeypatch.setattr(
        ConversationService,
        "load_context_messages",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        ConversationService,
        "start_stream_turn",
        AsyncMock(return_value=(fake_msg, fake_msg)),
    )

    # Bypass de la finalisation pour ne pas toucher AsyncSessionLocal.
    async def _noop_finalize(**_kwargs):
        return None

    monkeypatch.setattr(chat_router_module, "_finalize_in_fresh_session", _noop_finalize)

    resp = client.post(
        "/chat/stream",
        json={"message": "Salut", "expert_id": "general"},
    )
    assert resp.status_code == 200
    ctx = captured["ctx"]
    assert ctx.tools is not None
    assert any(t.get("function", {}).get("name") == "dummy_tool" for t in ctx.tools)
