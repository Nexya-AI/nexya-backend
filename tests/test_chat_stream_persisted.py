"""
Tests — chat stream persisté (Lot 4).

Couverture :
1. Parsing SSE → accumulation de contenu + capture de l'outcome
2. Mapping `done.reason` → `Message.status` final (`completed`/`failed`/`cancelled`)
3. Service — `ensure_conversation_for_stream` (création + chemin owned)
4. Service — `start_stream_turn` (INSERT user + placeholder, counter +2 atomique)
5. Service — `finalize_assistant_stream` (UPDATE + conversion cost_usd, rejet de
   status hors vocabulaire CHECK SQL)
6. Router — `POST /chat/stop` pose la clé Redis via `mark_cancelled`
7. Router — `POST /chat/stream` legacy stateless : PAS de persistance, pas
   d'appel à `ensure_conversation_for_stream`
8. Router — `POST /chat/stream` persisté : `finalize_assistant_stream` appelée
   dans le `finally` avec `status='completed'` et `content` accumulé
9. Router — `POST /chat/stream` persisté, stream qui finit en `done reason=error`
   → `finalize_assistant_stream` appelée avec `status='failed'` + `error_code`

Discipline tests :
- Pas de démarrage Postgres (tout en AsyncMock).
- Pas d'appel Redis réel (monkeypatch sur `mark_cancelled`).
- Pas d'appel provider IA réel (fake `StreamHandler.stream` qui yield des SSE).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.ai.engine.query_engine import (
    DONE_REASON_TO_STATUS as _DONE_REASON_TO_STATUS,
)
from app.ai.engine.query_engine import (
    StreamOutcome as _StreamOutcome,
)
from app.ai.engine.query_engine import (
    observe_sse_event as _observe_sse_event,
)
from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.chat import router as chat_router_module
from app.features.chat.models import Conversation, Message
from app.features.chat.service import ConversationService
from app.main import app

# ══════════════════════════════════════════════════════════════
# Fixtures communes (aux tests d'intégration router)
# ══════════════════════════════════════════════════════════════

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _make_fake_conversation(expert_id: str = "general") -> Conversation:
    now = datetime(2026, 4, 21, 14, 30, 0, tzinfo=UTC)
    conv = Conversation(user_id=_FAKE_USER_ID, title=None, expert_id=expert_id)
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


def _make_fake_message(role: str, content: str, status_: str) -> Message:
    now = datetime(2026, 4, 21, 14, 31, 0, tzinfo=UTC)
    msg = Message(
        conversation_id=uuid.UUID("a1b2c3d4-0000-4000-8000-000000000042"),
        role=role,
        content=content,
        status=status_,
    )
    msg.id = uuid.uuid4()
    msg.provider = None
    msg.model = None
    msg.prompt_tokens = None
    msg.completion_tokens = None
    msg.total_tokens = None
    msg.cost_usd = None
    msg.error_code = None
    msg.finished_at = None
    msg.deleted_at = None
    msg.created_at = now
    msg.updated_at = now
    return msg


@pytest.fixture
def client() -> TestClient:
    """TestClient avec `get_current_user` + `get_db` surchargés."""
    fake_user = _make_fake_user()
    fake_db = MagicMock()

    async def _fake_user_override() -> User:
        return fake_user

    async def _fake_db_override():
        yield fake_db

    app.dependency_overrides[get_current_user] = _fake_user_override
    app.dependency_overrides[get_db] = _fake_db_override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


# ══════════════════════════════════════════════════════════════
# Helpers monkeypatch (modules de dépendances IA)
# ══════════════════════════════════════════════════════════════


class _FakeBudgetTracker:
    """Autorise tout par défaut. Surchargeable pour simuler un quota dépassé."""

    def __init__(self) -> None:
        self.check_and_consume_chat = AsyncMock(return_value=None)
        self.check_and_consume_image = AsyncMock(return_value=None)


class _FakeModerationService:
    """Par défaut : `allowed=True`. Passer un `decision` custom pour simuler un blocage."""

    def __init__(self, allowed: bool = True) -> None:
        decision = MagicMock()
        decision.allowed = allowed
        self.check = AsyncMock(return_value=decision)


class _FakeAsyncContextSession:
    """Implémente juste `__aenter__` / `__aexit__` pour mimer `AsyncSessionLocal`.

    Le contenu de la session (MagicMock inerte) n'est pas consulté parce que
    `finalize_assistant_stream` est elle-même monkeypatchée dans les tests
    d'intégration router. `session.get` est par défaut un AsyncMock retournant
    `None` (suffisant pour neutraliser le hook d'enqueue de titre dans les
    tests qui ne s'en occupent pas)."""

    def __init__(self, conversation: Conversation | None = None) -> None:
        self.session = MagicMock()
        self.session.get = AsyncMock(return_value=conversation)

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _install_ai_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    budget: _FakeBudgetTracker | None = None,
    moderation: _FakeModerationService | None = None,
) -> tuple[_FakeBudgetTracker, _FakeModerationService]:
    """Injecte des stubs pour budget + modération au niveau du module router."""
    budget = budget or _FakeBudgetTracker()
    moderation = moderation or _FakeModerationService()
    monkeypatch.setattr(chat_router_module, "get_budget_tracker", lambda: budget)
    monkeypatch.setattr(chat_router_module, "get_moderation_service", lambda: moderation)
    return budget, moderation


def _install_fake_stream_handler(
    monkeypatch: pytest.MonkeyPatch, sse_events: list[str]
) -> MagicMock:
    """Remplace le singleton `get_stream_handler()` par un fake qui émet
    `sse_events` dans l'ordre. Le handler reçoit `(request, ctx)` et doit
    retourner un async generator — on renvoie directement un générateur."""
    fake_handler = MagicMock()

    async def _fake_stream(request, ctx):
        for evt in sse_events:
            yield evt

    fake_handler.stream = _fake_stream
    monkeypatch.setattr(chat_router_module, "get_stream_handler", lambda: fake_handler)
    return fake_handler


# ══════════════════════════════════════════════════════════════
# 1. Parsing SSE — `_observe_sse_event`
# ══════════════════════════════════════════════════════════════


def test_observe_accumulates_deltas_from_chunk_events() -> None:
    outcome = _StreamOutcome()
    _observe_sse_event('event: chunk\ndata: {"delta":"Bon"}\n\n', outcome)
    _observe_sse_event('event: chunk\ndata: {"delta":"jour"}\n\n', outcome)
    assert "".join(outcome.content_parts) == "Bonjour"


def test_observe_captures_done_reason() -> None:
    outcome = _StreamOutcome()
    _observe_sse_event('event: done\ndata: {"reason":"stop"}\n\n', outcome)
    assert outcome.done_reason == "stop"


def test_observe_captures_error_code_then_done_reason_error() -> None:
    outcome = _StreamOutcome()
    _observe_sse_event(
        'event: error\ndata: {"code":"LLM_UNAVAILABLE","message":"bye"}\n\n',
        outcome,
    )
    _observe_sse_event('event: done\ndata: {"reason":"error"}\n\n', outcome)
    assert outcome.error_code == "LLM_UNAVAILABLE"
    assert outcome.done_reason == "error"


def test_observe_ignores_keepalive_comments() -> None:
    """Le commentaire SSE `: keepalive` ne doit pas altérer l'état."""
    outcome = _StreamOutcome()
    _observe_sse_event(": keepalive\n\n", outcome)
    assert outcome.content_parts == []
    assert outcome.done_reason == "error"  # défaut inchangé
    assert outcome.error_code is None


def test_observe_survives_malformed_json() -> None:
    """Un payload JSON invalide est loggé et ignoré — jamais de crash."""
    outcome = _StreamOutcome()
    _observe_sse_event("event: chunk\ndata: not-json-at-all\n\n", outcome)
    assert outcome.content_parts == []


# ══════════════════════════════════════════════════════════════
# 2. Mapping done.reason → Message.status
# ══════════════════════════════════════════════════════════════


def test_done_reason_status_mapping_is_aligned_with_check_constraint() -> None:
    """Le vocabulaire doit être EXACTEMENT celui du CHECK SQL messages.status."""
    assert _DONE_REASON_TO_STATUS["stop"] == "completed"
    assert _DONE_REASON_TO_STATUS["cancelled"] == "cancelled"
    assert _DONE_REASON_TO_STATUS["error"] == "failed"


# ══════════════════════════════════════════════════════════════
# 3. Service — ensure_conversation_for_stream
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ensure_conversation_for_stream_creates_new_when_id_is_none() -> None:
    """Sans `conversation_id`, on crée une nouvelle conv avec l'expert_hint."""
    user = _make_fake_user()

    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    conv = await ConversationService.ensure_conversation_for_stream(
        None, user, db, expert_id_hint="computer"
    )

    assert isinstance(conv, Conversation)
    assert conv.user_id == _FAKE_USER_ID
    assert conv.expert_id == "computer"
    db.add.assert_called_once()
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_ensure_conversation_for_stream_loads_owned_when_id_provided(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Avec un `conversation_id`, on délègue à `_get_owned_conversation`."""
    user = _make_fake_user()
    existing = _make_fake_conversation(expert_id="medicine")
    db = MagicMock()

    mock_owned = AsyncMock(return_value=existing)
    monkeypatch.setattr(ConversationService, "_get_owned_conversation", mock_owned)

    result = await ConversationService.ensure_conversation_for_stream(
        existing.id, user, db, expert_id_hint="ignored-on-existing"
    )

    assert result is existing
    mock_owned.assert_awaited_once_with(existing.id, user.id, db)


# ══════════════════════════════════════════════════════════════
# 4. Service — start_stream_turn
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_start_stream_turn_inserts_user_placeholder_and_bumps_by_two(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deux INSERTs + bump counters delta=2 dans la même transaction, puis commit."""
    conv = _make_fake_conversation()

    added: list[Message] = []
    db = MagicMock()
    db.add = MagicMock(side_effect=lambda m: added.append(m))
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    mock_bump = AsyncMock()
    monkeypatch.setattr(ConversationService, "_bump_counters", mock_bump)

    user_msg, placeholder = await ConversationService.start_stream_turn(
        conv, "Salut, dis-moi bonjour !", db
    )

    assert len(added) == 2
    assert user_msg.role == "user"
    assert user_msg.status == "completed"
    assert user_msg.content == "Salut, dis-moi bonjour !"
    assert placeholder.role == "assistant"
    assert placeholder.status == "streaming"
    assert placeholder.content == ""

    mock_bump.assert_awaited_once()
    assert mock_bump.await_args.kwargs.get("delta") == 2
    db.commit.assert_awaited_once()


# ══════════════════════════════════════════════════════════════
# 5. Service — finalize_assistant_stream
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_finalize_assistant_stream_updates_message_and_conversation() -> None:
    """UPDATE Message + UPDATE Conversation.last_message_at, commit unique."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    await ConversationService.finalize_assistant_stream(
        uuid.uuid4(),
        uuid.uuid4(),
        db,
        content="Bonjour !",
        status="completed",
        provider="gemini",
        model="gemini-2.5-flash",
        prompt_tokens=10,
        completion_tokens=3,
        total_tokens=13,
        cost_usd=0.000123,
        error_code=None,
    )

    # Deux UPDATE attendus : Message puis Conversation
    assert db.execute.await_count == 2
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_finalize_assistant_stream_rejects_invalid_status() -> None:
    """Status hors vocabulaire CHECK → ValueError, aucun SQL émis."""
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    with pytest.raises(ValueError):
        await ConversationService.finalize_assistant_stream(
            uuid.uuid4(),
            uuid.uuid4(),
            db,
            content="",
            status="in_progress",  # invalide
            provider=None,
            model=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            cost_usd=None,
            error_code=None,
        )
    db.execute.assert_not_awaited()


@pytest.mark.asyncio
async def test_finalize_assistant_stream_converts_float_cost_to_decimal() -> None:
    """`estimate_cost_usd` renvoie un float ; la DB attend un Decimal. La
    conversion doit passer par `str(...)` pour éviter les artefacts binaires."""
    captured_values: list[dict] = []
    db = MagicMock()

    def capture(stmt):
        # On capture les valeurs de chaque UPDATE pour vérifier le type du cost
        params = getattr(stmt, "_values", None)
        if params:
            captured_values.append({k.name: v.value for k, v in params.items()})
        return MagicMock()

    db.execute = AsyncMock(side_effect=capture)
    db.commit = AsyncMock()

    await ConversationService.finalize_assistant_stream(
        uuid.uuid4(),
        uuid.uuid4(),
        db,
        content="Bonjour",
        status="completed",
        provider="openai",
        model="gpt-4o-mini",
        prompt_tokens=5,
        completion_tokens=2,
        total_tokens=7,
        cost_usd=0.1,  # float
        error_code=None,
    )

    # Premier UPDATE = Message
    message_values = captured_values[0]
    assert isinstance(message_values["cost_usd"], Decimal)
    assert message_values["cost_usd"] == Decimal("0.1")


# ══════════════════════════════════════════════════════════════
# 6. Router — POST /chat/stop
# ══════════════════════════════════════════════════════════════


def test_chat_stop_posts_redis_key_via_mark_cancelled(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_cancel = AsyncMock(return_value=None)
    monkeypatch.setattr(chat_router_module, "mark_cancelled", mock_cancel)

    response = client.post("/chat/stop", json={"session_id": "abc-123"})

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["session_id"] == "abc-123"
    assert body["data"]["cancelled"] is True
    mock_cancel.assert_awaited_once_with("abc-123")


# ══════════════════════════════════════════════════════════════
# 7. Router — /chat/stream legacy stateless
# ══════════════════════════════════════════════════════════════


def test_chat_stream_legacy_mode_skips_persistence(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`conversation_id=None` + `history=[...]` → pas de persistance, pas de
    finalize, pas d'appel à `ensure_conversation_for_stream`."""
    _install_ai_mocks(monkeypatch)
    _install_fake_stream_handler(
        monkeypatch,
        [
            'event: chunk\ndata: {"delta":"Salut"}\n\n',
            'event: done\ndata: {"reason":"stop"}\n\n',
        ],
    )

    ensure_mock = AsyncMock()
    finalize_mock = AsyncMock()
    monkeypatch.setattr(ConversationService, "ensure_conversation_for_stream", ensure_mock)
    monkeypatch.setattr(ConversationService, "finalize_assistant_stream", finalize_mock)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Bonjour",
            "history": [{"role": "user", "content": "Hello precedent"}],
        },
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "X-Session-Id" in response.headers
    assert "X-Conversation-Id" not in response.headers
    # C2-fix : en mode legacy stateless, aucun message n'est persisté en DB,
    # donc pas de header X-Assistant-Message-Id (cohérent — pas de row backend
    # à cibler pour feedback/report).
    assert "X-Assistant-Message-Id" not in response.headers

    ensure_mock.assert_not_awaited()
    finalize_mock.assert_not_awaited()


# ══════════════════════════════════════════════════════════════
# 8. Router — /chat/stream persisté (happy path)
# ══════════════════════════════════════════════════════════════


def test_chat_stream_persisted_finalizes_with_completed_status(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Flux complet : ensure → start_turn → stream → finalize(status='completed').

    On vérifie que `finalize_assistant_stream` est appelée avec :
    - `status='completed'` (mappé depuis `done.reason=stop`)
    - `content` = concaténation des deltas observés
    - `error_code` = None
    """
    _install_ai_mocks(monkeypatch)
    _install_fake_stream_handler(
        monkeypatch,
        [
            'event: chunk\ndata: {"delta":"Bon"}\n\n',
            'event: chunk\ndata: {"delta":"jour !"}\n\n',
            'event: done\ndata: {"reason":"stop","duration_ms":42}\n\n',
        ],
    )

    conv = _make_fake_conversation(expert_id="general")
    placeholder = _make_fake_message("assistant", "", "streaming")
    user_msg = _make_fake_message("user", "Dis bonjour", "completed")

    monkeypatch.setattr(
        ConversationService,
        "ensure_conversation_for_stream",
        AsyncMock(return_value=conv),
    )
    monkeypatch.setattr(
        ConversationService,
        "load_context_messages",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        ConversationService,
        "start_stream_turn",
        AsyncMock(return_value=(user_msg, placeholder)),
    )

    finalize_mock = AsyncMock()
    monkeypatch.setattr(ConversationService, "finalize_assistant_stream", finalize_mock)

    # AsyncSessionLocal() doit se comporter comme un context manager async
    monkeypatch.setattr(
        chat_router_module,
        "AsyncSessionLocal",
        lambda: _FakeAsyncContextSession(),
    )

    response = client.post(
        "/chat/stream",
        json={"message": "Dis bonjour"},
    )

    assert response.status_code == 200
    assert response.headers["X-Conversation-Id"] == str(conv.id)
    # C2-fix : header X-Assistant-Message-Id posé en mode persisté nouvelle
    # conv. Vaut l'UUID du placeholder retourné par `start_stream_turn` —
    # le client Flutter le capte en parallèle de X-Conversation-Id pour
    # cibler le message via les endpoints feedback/report.
    assert response.headers["X-Assistant-Message-Id"] == str(placeholder.id)
    # Consomme le body pour que le générateur aille jusqu'au `finally`
    body = response.text
    assert "Bonjour !" in body or "Bon" in body

    finalize_mock.assert_awaited_once()
    kwargs = finalize_mock.await_args.kwargs
    assert kwargs["content"] == "Bonjour !"
    assert kwargs["status"] == "completed"
    assert kwargs["error_code"] is None


def test_chat_stream_persisted_emits_assistant_message_id_for_existing_conversation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C2-fix : header X-Assistant-Message-Id posé aussi en mode « conv
    existante » (`body.conversation_id` fourni).

    Le 1er test (mode nouvelle conv) couvre le flux ensure_conversation
    sans `conversation_id`. Ici on valide le flux où le client passe un
    `conversation_id` existant — `ensure_conversation_for_stream` retourne
    la conv existante mais `start_stream_turn` crée toujours un nouveau
    placeholder assistant pour ce tour, dont l'UUID doit être exposé.

    Garde-fou contrat : si quelqu'un déplaçait l'ajout du header en dehors
    de la branche persistée commune, ce test casserait.
    """
    _install_ai_mocks(monkeypatch)
    _install_fake_stream_handler(
        monkeypatch,
        [
            'event: chunk\ndata: {"delta":"Suite"}\n\n',
            'event: done\ndata: {"reason":"stop"}\n\n',
        ],
    )

    existing_conv_id = uuid.uuid4()
    conv = _make_fake_conversation(expert_id="general")
    conv.id = existing_conv_id
    placeholder = _make_fake_message("assistant", "", "streaming")
    placeholder_id = placeholder.id  # capté avant que l'objet ne soit
    # potentiellement muté par le pipeline downstream
    user_msg = _make_fake_message("user", "Continue", "completed")

    monkeypatch.setattr(
        ConversationService,
        "ensure_conversation_for_stream",
        AsyncMock(return_value=conv),
    )
    monkeypatch.setattr(
        ConversationService,
        "load_context_messages",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        ConversationService,
        "start_stream_turn",
        AsyncMock(return_value=(user_msg, placeholder)),
    )

    finalize_mock = AsyncMock()
    monkeypatch.setattr(ConversationService, "finalize_assistant_stream", finalize_mock)

    monkeypatch.setattr(
        chat_router_module,
        "AsyncSessionLocal",
        lambda: _FakeAsyncContextSession(),
    )

    response = client.post(
        "/chat/stream",
        json={
            "message": "Continue",
            "conversation_id": str(existing_conv_id),
        },
    )

    assert response.status_code == 200
    assert response.headers["X-Conversation-Id"] == str(existing_conv_id)
    assert response.headers["X-Assistant-Message-Id"] == str(placeholder_id)
    assert "X-Session-Id" in response.headers
    # Consomme le body pour s'assurer que le générateur va jusqu'au `finally`
    _ = response.text


# ══════════════════════════════════════════════════════════════
# 9. Router — /chat/stream persisté : flux en erreur
# ══════════════════════════════════════════════════════════════


def test_chat_stream_persisted_finalizes_with_failed_on_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un stream qui finit avec `done reason=error` après un `event: error` →
    `finalize_assistant_stream` reçoit `status='failed'` + `error_code` récupéré."""
    _install_ai_mocks(monkeypatch)
    _install_fake_stream_handler(
        monkeypatch,
        [
            'event: chunk\ndata: {"delta":"Bon"}\n\n',
            'event: error\ndata: {"code":"LLM_UNAVAILABLE","message":"down"}\n\n',
            'event: done\ndata: {"reason":"error"}\n\n',
        ],
    )

    conv = _make_fake_conversation()
    placeholder = _make_fake_message("assistant", "", "streaming")
    user_msg = _make_fake_message("user", "Salut", "completed")

    monkeypatch.setattr(
        ConversationService,
        "ensure_conversation_for_stream",
        AsyncMock(return_value=conv),
    )
    monkeypatch.setattr(
        ConversationService,
        "load_context_messages",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        ConversationService,
        "start_stream_turn",
        AsyncMock(return_value=(user_msg, placeholder)),
    )

    finalize_mock = AsyncMock()
    monkeypatch.setattr(ConversationService, "finalize_assistant_stream", finalize_mock)

    monkeypatch.setattr(
        chat_router_module,
        "AsyncSessionLocal",
        lambda: _FakeAsyncContextSession(),
    )

    response = client.post("/chat/stream", json={"message": "Salut"})
    assert response.status_code == 200
    _ = response.text

    finalize_mock.assert_awaited_once()
    kwargs = finalize_mock.await_args.kwargs
    assert kwargs["status"] == "failed"
    assert kwargs["error_code"] == "LLM_UNAVAILABLE"
    assert kwargs["content"] == "Bon"  # contenu partiel conservé


# ══════════════════════════════════════════════════════════════
# 10. Router — /chat/stream modération bloquante (400)
# ══════════════════════════════════════════════════════════════


def test_chat_stream_returns_400_when_moderation_blocks(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Modération `allowed=False` → 400 CONTENT_FILTERED, pas de stream ouvert."""
    _install_ai_mocks(
        monkeypatch,
        moderation=_FakeModerationService(allowed=False),
    )

    ensure_mock = AsyncMock()
    monkeypatch.setattr(ConversationService, "ensure_conversation_for_stream", ensure_mock)

    response = client.post("/chat/stream", json={"message": "payload flagged"})

    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "CONTENT_FILTERED"
    ensure_mock.assert_not_awaited()


# ══════════════════════════════════════════════════════════════
# 11. Router — /chat/stream rejette un message vide (422)
# ══════════════════════════════════════════════════════════════


def test_chat_stream_rejects_whitespace_only_message(client: TestClient) -> None:
    """Validator Pydantic `message_not_only_whitespace` → 422, avant tout service."""
    response = client.post("/chat/stream", json={"message": "   "})
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# 12. Router — enqueue auto-titre (Lot 5)
# ══════════════════════════════════════════════════════════════
# Hook posé dans `_finalize_in_fresh_session` : après un stream `completed`,
# si la conv a `message_count >= 4` ET `title IS NULL` ET
# `title_generated_at IS NULL`, on enqueue `enqueue_title_generation`. Sinon
# on ne déclenche pas — la sentinelle protège du doublon.
# ══════════════════════════════════════════════════════════════


def _setup_persisted_stream(
    monkeypatch, *, conv: Conversation, conv_after_finalize: Conversation | None
):
    """Câblage commun aux tests d'enqueue de titre."""
    _install_ai_mocks(monkeypatch)
    _install_fake_stream_handler(
        monkeypatch,
        [
            'event: chunk\ndata: {"delta":"Bonjour"}\n\n',
            'event: done\ndata: {"reason":"stop"}\n\n',
        ],
    )

    placeholder = _make_fake_message("assistant", "", "streaming")
    user_msg = _make_fake_message("user", "Salut", "completed")

    monkeypatch.setattr(
        ConversationService,
        "ensure_conversation_for_stream",
        AsyncMock(return_value=conv),
    )
    monkeypatch.setattr(
        ConversationService,
        "load_context_messages",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        ConversationService,
        "start_stream_turn",
        AsyncMock(return_value=(user_msg, placeholder)),
    )
    monkeypatch.setattr(
        ConversationService,
        "finalize_assistant_stream",
        AsyncMock(),
    )
    monkeypatch.setattr(
        chat_router_module,
        "AsyncSessionLocal",
        lambda: _FakeAsyncContextSession(conversation=conv_after_finalize),
    )


def test_chat_stream_enqueues_title_when_threshold_reached(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`message_count=4`, pas de titre, pas de sentinelle → enqueue déclenché."""
    conv = _make_fake_conversation()
    conv_after = _make_fake_conversation()
    conv_after.message_count = 4  # 2 tours user/assistant complets

    _setup_persisted_stream(monkeypatch, conv=conv, conv_after_finalize=conv_after)

    enqueue_mock = AsyncMock()
    monkeypatch.setattr(chat_router_module, "enqueue_title_generation", enqueue_mock)

    response = client.post("/chat/stream", json={"message": "Salut"})
    assert response.status_code == 200
    _ = response.text  # consomme le stream pour passer dans le `finally`

    enqueue_mock.assert_awaited_once_with(conv.id)


def test_chat_stream_skips_title_enqueue_when_deterministic_title_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """**Bug-040 stable fix (2026-05-15)** — Avec le titre déterministe posé
    au INSERT par `ConversationService.ensure_conversation_for_stream`, le
    check `title is None AND title_generated_at is None` est false → l'enqueue
    worker arq est naturellement no-op (gratuit, pas de Redis call gaspillé).

    Test du contrat post-fix : conv avec titre déterministe simulé (comme le
    fait le service réel) → enqueue skip silencieux peu importe message_count.
    """
    conv = _make_fake_conversation()
    conv_after = _make_fake_conversation()
    conv_after.message_count = 2  # au seuil, mais titre déjà posé → skip
    conv_after.title = "Salut"  # titre déterministe simulé (Bug-040 fix)
    conv_after.title_generated_at = datetime(2026, 5, 15, tzinfo=UTC)

    _setup_persisted_stream(monkeypatch, conv=conv, conv_after_finalize=conv_after)

    enqueue_mock = AsyncMock()
    monkeypatch.setattr(chat_router_module, "enqueue_title_generation", enqueue_mock)

    response = client.post("/chat/stream", json={"message": "Salut"})
    assert response.status_code == 200
    _ = response.text

    enqueue_mock.assert_not_awaited()


def test_chat_stream_skips_title_enqueue_when_sentinel_already_set(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`title_generated_at` non-null → enqueue désamorcé même au seuil."""
    conv = _make_fake_conversation()
    conv_after = _make_fake_conversation()
    conv_after.message_count = 6
    conv_after.title_generated_at = datetime(2026, 4, 21, 14, 35, 0, tzinfo=UTC)
    conv_after.title = "Titre déjà posé"

    _setup_persisted_stream(monkeypatch, conv=conv, conv_after_finalize=conv_after)

    enqueue_mock = AsyncMock()
    monkeypatch.setattr(chat_router_module, "enqueue_title_generation", enqueue_mock)

    response = client.post("/chat/stream", json={"message": "Salut"})
    assert response.status_code == 200
    _ = response.text

    enqueue_mock.assert_not_awaited()


def test_chat_stream_skips_title_enqueue_when_status_failed(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stream qui finit en `failed` → pas de titre auto (contenu non valide)."""
    _install_ai_mocks(monkeypatch)
    _install_fake_stream_handler(
        monkeypatch,
        [
            'event: error\ndata: {"code":"LLM_UNAVAILABLE"}\n\n',
            'event: done\ndata: {"reason":"error"}\n\n',
        ],
    )

    conv = _make_fake_conversation()
    conv_after = _make_fake_conversation()
    conv_after.message_count = 8

    placeholder = _make_fake_message("assistant", "", "streaming")
    user_msg = _make_fake_message("user", "X", "completed")

    monkeypatch.setattr(
        ConversationService,
        "ensure_conversation_for_stream",
        AsyncMock(return_value=conv),
    )
    monkeypatch.setattr(
        ConversationService,
        "load_context_messages",
        AsyncMock(return_value=[]),
    )
    monkeypatch.setattr(
        ConversationService,
        "start_stream_turn",
        AsyncMock(return_value=(user_msg, placeholder)),
    )
    monkeypatch.setattr(
        ConversationService,
        "finalize_assistant_stream",
        AsyncMock(),
    )
    monkeypatch.setattr(
        chat_router_module,
        "AsyncSessionLocal",
        lambda: _FakeAsyncContextSession(conversation=conv_after),
    )

    enqueue_mock = AsyncMock()
    monkeypatch.setattr(chat_router_module, "enqueue_title_generation", enqueue_mock)

    response = client.post("/chat/stream", json={"message": "X"})
    assert response.status_code == 200
    _ = response.text

    enqueue_mock.assert_not_awaited()
