"""N1 — SuggestionService unit tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors.exceptions import RateLimitAbuseException
from app.features.suggestions.schemas import SuggestionCreate
from app.features.suggestions.service import SuggestionService


def _mk_user(email="user@nexya.ai") -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = email
    return user


def _mk_db_for_submit() -> MagicMock:
    db = MagicMock()
    db.add = MagicMock()
    db.flush = AsyncMock()

    async def _refresh(obj):
        # simule l'attribution post-INSERT
        if not getattr(obj, "id", None):
            obj.id = uuid.uuid4()
        if not getattr(obj, "created_at", None):
            obj.created_at = datetime.now(UTC)

    db.refresh = AsyncMock(side_effect=_refresh)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_submit_happy_path_inserts_and_emails(monkeypatch):
    user = _mk_user()
    db = _mk_db_for_submit()

    rate_limit = AsyncMock()
    monkeypatch.setattr("app.features.suggestions.service.check_user_rate_limit", rate_limit)
    fake_send = AsyncMock()
    fake_service = MagicMock()
    fake_service.send = fake_send
    monkeypatch.setattr(
        "app.features.suggestions.service.get_email_service",
        lambda: fake_service,
    )

    body = SuggestionCreate(
        suggestion_type="feature",
        body="Mode sombre dynamique selon l'heure",
    )
    result = await SuggestionService.submit(user, body, ip="1.2.3.4", user_agent="UA", db=db)

    assert result.suggestion_type == "feature"
    assert result.processing_status == "open"
    db.add.assert_called_once()
    db.commit.assert_awaited_once()
    rate_limit.assert_awaited_once()
    fake_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_rate_limit_429(monkeypatch):
    user = _mk_user()
    db = _mk_db_for_submit()

    async def _raise(*args, **kwargs):
        raise RateLimitAbuseException(retry_after=86400)

    monkeypatch.setattr("app.features.suggestions.service.check_user_rate_limit", _raise)

    body = SuggestionCreate(suggestion_type="bug", body="Crash sur le chat")
    with pytest.raises(RateLimitAbuseException) as exc:
        await SuggestionService.submit(user, body, ip=None, user_agent=None, db=db)
    assert exc.value.code == "RATE_LIMIT_ABUSE"
    db.commit.assert_not_called()


@pytest.mark.asyncio
async def test_submit_email_failsafe_db_still_committed(monkeypatch):
    """L'email Brevo plante → la suggestion DB reste committée."""
    user = _mk_user()
    db = _mk_db_for_submit()

    monkeypatch.setattr("app.features.suggestions.service.check_user_rate_limit", AsyncMock())

    fake_service = MagicMock()
    fake_service.send = AsyncMock(side_effect=RuntimeError("Brevo 503"))
    monkeypatch.setattr(
        "app.features.suggestions.service.get_email_service",
        lambda: fake_service,
    )

    body = SuggestionCreate(suggestion_type="other", body="test")
    result = await SuggestionService.submit(user, body, ip=None, user_agent=None, db=db)
    assert result.suggestion_type == "other"
    db.commit.assert_awaited_once()  # commit OK malgré email KO


@pytest.mark.asyncio
async def test_submit_anonymizes_ip_in_email_template(monkeypatch):
    user = _mk_user()
    db = _mk_db_for_submit()

    monkeypatch.setattr("app.features.suggestions.service.check_user_rate_limit", AsyncMock())

    fake_render = MagicMock(return_value=("<html/>", "txt"))
    fake_renderer = MagicMock()
    fake_renderer.render = fake_render
    monkeypatch.setattr(
        "app.features.suggestions.service.get_template_renderer",
        lambda: fake_renderer,
    )
    monkeypatch.setattr(
        "app.features.suggestions.service.get_email_service",
        lambda: MagicMock(send=AsyncMock()),
    )

    body = SuggestionCreate(suggestion_type="feature", body="Idée X")
    await SuggestionService.submit(user, body, ip="192.168.1.42", user_agent=None, db=db)

    fake_render.assert_called_once()
    ctx = fake_render.call_args.kwargs
    assert ctx["ip_anonymized"] == "192.168.1.0/24"
    assert ctx["user_email"] == user.email
    assert "192.168.1.42" not in ctx["ip_anonymized"]


def test_suggestion_create_validates_4_types():
    """Pydantic accepte les 4 types valides."""
    for t in ("bug", "feature", "expert_domain", "other"):
        body = SuggestionCreate(suggestion_type=t, body="x")
        assert body.suggestion_type == t


def test_suggestion_create_rejects_invalid_type():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SuggestionCreate(suggestion_type="hack", body="x")  # type: ignore[arg-type]


def test_suggestion_create_rejects_empty_body():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SuggestionCreate(suggestion_type="bug", body="")


def test_suggestion_create_rejects_body_2001_chars():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        SuggestionCreate(suggestion_type="bug", body="x" * 2001)


def test_suggestion_create_accepts_body_2000_chars():
    body = SuggestionCreate(suggestion_type="bug", body="x" * 2000)
    assert len(body.body) == 2000
