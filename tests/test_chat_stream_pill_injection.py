"""
Tests intégration — Model Pills sur /chat/stream.

Vérifie le wiring bout-en-bout : `body.model_pill` → résolution via
`resolve_model_for_pill` → propagation dans `StreamContext.pill_*_override`
→ disponible pour `_run_link` (override modèle + thinking sur 1ᵉʳ lien).

Couverture :
1. Mode legacy stateless : pill envoyée → override propagé dans ctx legacy.
2. Mode persisté : pill envoyée → override propagé dans ctx persisté.
3. Pill absente : override reste None (comportement A1+A2 préservé).
4. Pill envoyée sur studio : override (None, None) → comportement legacy.
5. Pill envoyée invalide (Pydantic 422 — anti-injection client buggé).
6. Cooking + pill GEEK : override (gemini-2.5-pro, True) — G2 V8 preserve.
7. Medicine + pill JUSTO : override (gemini-2.5-pro, False) — safety-critical.

Pattern aligné `tests/test_chat_stream_persisted.py` : fakes complets,
zéro DB / zéro Redis / zéro provider IA, capture du `ctx` reçu par le
handler pour assertion directe.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.chat import router as chat_router_module
from app.features.chat.models import Conversation, Message
from app.features.chat.service import ConversationService
from app.main import app

# ══════════════════════════════════════════════════════════════
# Fixtures alignées sur test_chat_stream_persisted.py
# ══════════════════════════════════════════════════════════════

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _make_fake_conversation(expert_id: str = "general") -> Conversation:
    now = datetime(2026, 5, 23, 14, 30, 0, tzinfo=UTC)
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
    now = datetime(2026, 5, 23, 14, 31, 0, tzinfo=UTC)
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


class _FakeBudgetTracker:
    def __init__(self) -> None:
        self.check_and_consume_chat = AsyncMock(return_value=None)
        self.check_and_consume_image = AsyncMock(return_value=None)


class _FakeModerationService:
    def __init__(self, allowed: bool = True) -> None:
        decision = MagicMock()
        decision.allowed = allowed
        self.check = AsyncMock(return_value=decision)


class _FakeAsyncContextSession:
    def __init__(self, conversation: Conversation | None = None) -> None:
        self.session = MagicMock()
        self.session.get = AsyncMock(return_value=conversation)

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def _install_ai_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        chat_router_module, "get_budget_tracker", lambda: _FakeBudgetTracker()
    )
    monkeypatch.setattr(
        chat_router_module, "get_moderation_service", lambda: _FakeModerationService()
    )


def _install_capturing_stream_handler(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Remplace `get_stream_handler` par un fake qui capture le `ctx` reçu.

    Retourne un dict mutable `{ctx: StreamContext | None}` à lire après
    le POST `/chat/stream` pour vérifier les overrides pill.
    """
    captured: dict = {"ctx": None}
    fake_handler = MagicMock()

    async def _fake_stream(request, ctx):
        captured["ctx"] = ctx
        # Émet juste un done propre pour clôturer le SSE proprement.
        yield 'event: done\ndata: {"reason":"stop"}\n\n'

    fake_handler.stream = _fake_stream
    monkeypatch.setattr(chat_router_module, "get_stream_handler", lambda: fake_handler)
    return captured


# ══════════════════════════════════════════════════════════════
# 1. Mode legacy stateless — pill propagée
# ══════════════════════════════════════════════════════════════


def test_legacy_pill_loth_propagates_pro_thinking_off(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """body.model_pill='loth' sur expert general → ctx override
    (gemini-2.5-pro, True) en mode legacy stateless."""
    _install_ai_mocks(monkeypatch)
    captured = _install_capturing_stream_handler(monkeypatch)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Hello",
            "expert_id": "general",
            "model_pill": "loth",
            "history": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 200
    ctx = captured["ctx"]
    assert ctx is not None, "Le handler doit avoir reçu un StreamContext"
    assert ctx.pill_model_override == "gemini-2.5-pro"
    assert ctx.pill_disable_thinking_override is True


def test_legacy_pill_geek_on_computer_propagates_thinking_on(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """expert computer + pill GEEK → (gemini-2.5-pro, False) thinking ON."""
    _install_ai_mocks(monkeypatch)
    captured = _install_capturing_stream_handler(monkeypatch)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Hello",
            "expert_id": "computer",
            "model_pill": "geek",
            "history": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 200
    ctx = captured["ctx"]
    assert ctx.pill_model_override == "gemini-2.5-pro"
    assert ctx.pill_disable_thinking_override is False  # thinking ON


def test_legacy_pill_justo_propagates_flash(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """pill JUSTO → flash (rapidité)."""
    _install_ai_mocks(monkeypatch)
    captured = _install_capturing_stream_handler(monkeypatch)

    client.post(
        "/chat/stream",
        json={
            "message": "Hi",
            "expert_id": "general",
            "model_pill": "justo",
            "history": [{"role": "user", "content": "ping"}],
        },
    )
    ctx = captured["ctx"]
    assert ctx.pill_model_override == "gemini-2.5-flash"
    assert ctx.pill_disable_thinking_override is True


# ══════════════════════════════════════════════════════════════
# 2. Pill absente — comportement legacy strictement préservé
# ══════════════════════════════════════════════════════════════


def test_no_pill_keeps_overrides_none(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """body sans `model_pill` → ctx overrides à None → comportement A1+A2."""
    _install_ai_mocks(monkeypatch)
    captured = _install_capturing_stream_handler(monkeypatch)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Hello",
            "expert_id": "general",
            "history": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 200
    ctx = captured["ctx"]
    assert ctx.pill_model_override is None
    assert ctx.pill_disable_thinking_override is None


# ══════════════════════════════════════════════════════════════
# 3. Safety-critical preservation
# ══════════════════════════════════════════════════════════════


def test_medicine_justo_keeps_thinking_on(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """medicine + JUSTO → Pro + thinking ON (safety-critical garde-fou)."""
    _install_ai_mocks(monkeypatch)
    captured = _install_capturing_stream_handler(monkeypatch)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Symptômes du paludisme ?",
            "expert_id": "medicine",
            "model_pill": "justo",
            "history": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 200
    ctx = captured["ctx"]
    assert ctx.pill_model_override == "gemini-2.5-pro"
    assert ctx.pill_disable_thinking_override is False  # thinking ON même en JUSTO


def test_cooking_geek_preserves_g2_v8_disable_thinking(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """cooking + GEEK → Pro + disable_thinking=True (G2 V8 preserve)."""
    _install_ai_mocks(monkeypatch)
    captured = _install_capturing_stream_handler(monkeypatch)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Recette ndolé",
            "expert_id": "cooking",
            "model_pill": "geek",
            "history": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 200
    ctx = captured["ctx"]
    assert ctx.pill_model_override == "gemini-2.5-pro"
    assert ctx.pill_disable_thinking_override is True  # G2 V8 preserve


# ══════════════════════════════════════════════════════════════
# 4. Expert inconnu + pill → fallback general (helper résout)
# ══════════════════════════════════════════════════════════════


def test_unknown_expert_with_pill_falls_back_to_general(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Expert inconnu (futur slug Flutter pas encore déployé backend) +
    pill → `resolve_model_for_pill` fallback general, le router continue
    sans crash. Pattern aligné `get_expert_config` permissif."""
    _install_ai_mocks(monkeypatch)
    captured = _install_capturing_stream_handler(monkeypatch)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Hello",
            "expert_id": "future_unknown_expert",
            "model_pill": "geek",
            "history": [{"role": "user", "content": "ping"}],
        },
    )
    # Le router AI fallback aussi sur general → 200 OK, ctx peuplé.
    assert response.status_code == 200
    ctx = captured["ctx"]
    # Helper résout depuis general (default mapping) : GEEK = pro + thinking ON
    assert ctx.pill_model_override == "gemini-2.5-pro"
    assert ctx.pill_disable_thinking_override is False


# ══════════════════════════════════════════════════════════════
# 5. Pill invalide → 422 (anti-injection client buggé)
# ══════════════════════════════════════════════════════════════


def test_invalid_pill_returns_422(client: TestClient) -> None:
    """Pydantic rejette pill hors Literal['geek','loth','justo'] → 422.

    Le handler global scrubbe les détails (anti-leak), on vérifie juste
    le status_code + le code d'erreur NEXYA `VALIDATION_ERROR`.
    """
    response = client.post(
        "/chat/stream",
        json={
            "message": "Hello",
            "expert_id": "general",
            "model_pill": "super-power",  # invalide
            "history": [{"role": "user", "content": "ping"}],
        },
    )
    assert response.status_code == 422
    body = response.json()
    assert body.get("success") is False
    assert body.get("code") == "VALIDATION_ERROR"


# ══════════════════════════════════════════════════════════════
# 6. Mode persisté — pill propagée dans ctx persisté
# ══════════════════════════════════════════════════════════════


def test_persisted_pill_propagates_to_stream_context(
    monkeypatch: pytest.MonkeyPatch, client: TestClient
) -> None:
    """Mode persisté (sans `history`) : pill propagée jusqu'au ctx
    qui sera utilisé pour le run_link."""
    _install_ai_mocks(monkeypatch)
    captured = _install_capturing_stream_handler(monkeypatch)

    conv = _make_fake_conversation(expert_id="science")
    placeholder = _make_fake_message("assistant", "", "streaming")
    user_msg = _make_fake_message("user", "Démontre Pythagore", "completed")

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
        lambda: _FakeAsyncContextSession(conversation=conv),
    )
    monkeypatch.setattr(chat_router_module, "enqueue_title_generation", AsyncMock())
    monkeypatch.setattr(
        chat_router_module, "enqueue_memory_extraction", AsyncMock()
    )

    response = client.post(
        "/chat/stream",
        json={
            "message": "Démontre Pythagore",
            "expert_id": "science",
            "model_pill": "loth",
            # Pas de `history` → mode persisté implicite
        },
    )
    assert response.status_code == 200
    ctx = captured["ctx"]
    assert ctx is not None
    assert ctx.pill_model_override == "gemini-2.5-pro"
    assert ctx.pill_disable_thinking_override is True  # LOTH = pro sans thinking
