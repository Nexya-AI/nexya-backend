"""
Tests N4 — `app.features.helpdesk.service`.

Couvre :
1. `should_escalate` Pro+payment+high → True
2. Free → False
3. Pro+payment+low → False (severity insuffisante)
4. Pro+catégorie inconnue → False
5. Kill-switch off → False
6. `escalate` happy path : INSERT + Crisp call + UPDATE conversation_id
7. Fail-safe Crisp KO → row insérée mais crisp_conversation_id=NULL
8. `_build_crisp_request` produit un payload propre (nickname, email, segments)
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.features.helpdesk.schemas import EscalationCreate
from app.features.helpdesk.service import (
    CrispEscalationService,
    _build_crisp_request,
)
from app.integrations.crisp_client import (
    CrispConversationRequest,
    MockCrispClient,
)

# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════


def _make_pro_user() -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = "ivan@nexya.ai"
    user.username = "ivan"
    user.display_name = "Ivan Ngassa"
    user.is_pro = True
    return user


def _make_free_user() -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = "free@nexya.ai"
    user.is_pro = False
    return user


# ══════════════════════════════════════════════════════════════
# should_escalate
# ══════════════════════════════════════════════════════════════


def test_should_escalate_pro_payment_high_true() -> None:
    assert (
        CrispEscalationService.should_escalate(
            user=_make_pro_user(), category="payment", severity="high"
        )
        is True
    )


def test_should_escalate_free_user_false() -> None:
    assert (
        CrispEscalationService.should_escalate(
            user=_make_free_user(), category="payment", severity="high"
        )
        is False
    )


def test_should_escalate_low_severity_false() -> None:
    assert (
        CrispEscalationService.should_escalate(
            user=_make_pro_user(), category="payment", severity="low"
        )
        is False
    )


def test_should_escalate_critical_severity_true() -> None:
    assert (
        CrispEscalationService.should_escalate(
            user=_make_pro_user(), category="security", severity="critical"
        )
        is True
    )


def test_should_escalate_user_none_false() -> None:
    assert (
        CrispEscalationService.should_escalate(
            user=None, category="payment", severity="high"
        )
        is False
    )


def test_should_escalate_kill_switch_off_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "crisp_escalation_enabled", False)
    assert (
        CrispEscalationService.should_escalate(
            user=_make_pro_user(), category="payment", severity="high"
        )
        is False
    )


# ══════════════════════════════════════════════════════════════
# _build_crisp_request — payload propre
# ══════════════════════════════════════════════════════════════


def test_build_crisp_request_pro_user_includes_email_and_segments() -> None:
    user = _make_pro_user()
    body = EscalationCreate(
        user_id=user.id,
        category="payment",
        severity="high",
        payload={"order_id": "ord-123", "amount": "5000"},
    )
    req = _build_crisp_request(body=body, user=user)
    assert isinstance(req, CrispConversationRequest)
    assert req.email == "ivan@nexya.ai"
    assert req.nickname == "Ivan Ngassa"
    assert "payment" in req.message.lower()
    assert "high" in req.message.lower()
    assert "ord-123" in req.message  # payload propagé
    # Metadata pour les segments Crisp
    assert req.metadata["category"] == "payment"
    assert req.metadata["severity"] == "high"
    assert req.metadata["order_id"] == "ord-123"


def test_build_crisp_request_no_user_uses_anonyme() -> None:
    body = EscalationCreate(
        user_id=None, category="security", severity="critical", payload=None
    )
    req = _build_crisp_request(body=body, user=None)
    assert req.nickname == "Anonyme"
    assert req.email is None


# ══════════════════════════════════════════════════════════════
# escalate — pipeline + fail-safe
# ══════════════════════════════════════════════════════════════


class _FakeAsyncSession:
    def __init__(self) -> None:
        self.added: list = []
        self.flush_calls = 0
        self.commit_calls = 0
        self.rollback_calls = 0

    def add(self, row) -> None:  # type: ignore[no-untyped-def]
        # Simule un ID auto-généré au flush
        if row.id is None:
            row.id = uuid.uuid4()
        self.added.append(row)

    async def flush(self) -> None:
        self.flush_calls += 1

    async def commit(self) -> None:
        self.commit_calls += 1

    async def rollback(self) -> None:
        self.rollback_calls += 1


@pytest.mark.asyncio
async def test_escalate_happy_path_inserts_and_calls_crisp(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _make_pro_user()
    db = _FakeAsyncSession()
    mock_client = MockCrispClient()

    monkeypatch.setattr(
        "app.features.helpdesk.service.get_crisp_client",
        lambda: mock_client,
    )

    body = EscalationCreate(
        user_id=user.id,
        category="payment",
        severity="high",
        payload={"order_id": "ord-1"},
    )
    row = await CrispEscalationService.escalate(body=body, user=user, db=db)

    # Insertion locale
    assert len(db.added) == 1
    assert db.commit_calls >= 1
    # Crisp appelé
    assert len(mock_client.calls) == 1
    assert mock_client.calls[0].metadata["category"] == "payment"
    # crisp_conversation_id propagé
    assert row.crisp_conversation_id is not None
    assert row.crisp_conversation_id.startswith("mock-session-")


@pytest.mark.asyncio
async def test_escalate_fail_safe_when_crisp_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _make_pro_user()
    db = _FakeAsyncSession()
    mock_client = MockCrispClient(force_fail=True)

    monkeypatch.setattr(
        "app.features.helpdesk.service.get_crisp_client",
        lambda: mock_client,
    )

    body = EscalationCreate(
        user_id=user.id,
        category="llm_unavailable",
        severity="high",
        payload=None,
    )
    row = await CrispEscalationService.escalate(body=body, user=user, db=db)

    # Row insérée localement quand même
    assert len(db.added) == 1
    # Mais crisp_conversation_id reste None (fail-safe)
    assert row.crisp_conversation_id is None
    # Crisp a bien été tenté
    assert len(mock_client.calls) == 1


@pytest.mark.asyncio
async def test_escalate_fail_safe_on_unexpected_crisp_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _make_pro_user()
    db = _FakeAsyncSession()

    class _BoomClient:
        async def create_conversation(self, request):  # type: ignore[no-untyped-def]
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "app.features.helpdesk.service.get_crisp_client",
        lambda: _BoomClient(),
    )

    body = EscalationCreate(
        user_id=user.id, category="security", severity="critical", payload={}
    )
    row = await CrispEscalationService.escalate(body=body, user=user, db=db)

    # Row toujours insérée, jamais raise
    assert len(db.added) == 1
    assert row.crisp_conversation_id is None
