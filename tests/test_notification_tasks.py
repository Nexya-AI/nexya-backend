"""Tests Phase 1 — notifications transactionnelles auth.

Couvre :
    - `build_security_alert_content` (pur)
    - `_format_utc_human` (pur, fail-safe)
    - `is_new_login_device` (logique détection device inconnu, mock db)
    - `send_welcome_email` worker (email + notif in-app, fail-safe)
    - `send_security_alert` worker (dispatch category='security', fail-safe)
    - `enqueue_*` fail-safe sur Redis down

Mock-first strict : aucun Redis/DB/Brevo/FCM réel.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

import workers.notification_tasks as nt
from app.features.auth.auth_events import is_new_login_device

pytestmark = pytest.mark.asyncio


# ══════════════════════════════════════════════════════════════
# Fakes
# ══════════════════════════════════════════════════════════════


class _FakeSessionCtx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


def _make_user(*, active=True, deleted=False):
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = "ivan@nexyalabs.com"
    user.display_name = "Ivan"
    user.username = "ivan"
    user.is_active = active
    user.deleted_at = object() if deleted else None
    return user


def _make_count_result(total: int, same_device: int):
    """Fabrique le résultat de `(await db.execute(...)).one()` du helper."""
    result = MagicMock()
    result.one.return_value = MagicMock(total=total, same_device=same_device)
    return result


# ══════════════════════════════════════════════════════════════
# build_security_alert_content (pur)
# ══════════════════════════════════════════════════════════════


def test_security_content_new_device():
    title, body = nt.build_security_alert_content("new_device_login")
    assert "connexion" in title.lower()
    assert "appareil" in body.lower()
    assert title and body


def test_security_content_password_changed():
    title, body = nt.build_security_alert_content("password_changed")
    assert "mot de passe" in title.lower()
    assert "compromis" in body.lower()


def test_security_content_fallback_unknown_event():
    title, body = nt.build_security_alert_content("totally_unknown")
    assert title and body  # jamais vide


# ══════════════════════════════════════════════════════════════
# _format_utc_human (pur, fail-safe)
# ══════════════════════════════════════════════════════════════


def test_format_utc_human_valid_iso():
    out = nt._format_utc_human("2026-06-22T14:30:05+00:00")
    assert out == "2026-06-22 14:30 UTC"


def test_format_utc_human_bad_input_returns_raw():
    assert nt._format_utc_human("not-a-date") == "not-a-date"
    assert nt._format_utc_human("") == ""


# ══════════════════════════════════════════════════════════════
# is_new_login_device (logique)
# ══════════════════════════════════════════════════════════════


async def test_new_device_none_device_id_skips_query():
    db = MagicMock()
    db.execute = AsyncMock()
    assert await is_new_login_device(uuid.uuid4(), None, db) is False
    db.execute.assert_not_awaited()


async def test_new_device_unknown_sentinel_skips_query():
    db = MagicMock()
    db.execute = AsyncMock()
    assert await is_new_login_device(uuid.uuid4(), "unknown", db) is False
    db.execute.assert_not_awaited()


async def test_new_device_established_user_new_device_is_true():
    db = MagicMock()
    db.execute = AsyncMock(return_value=_make_count_result(total=3, same_device=0))
    assert await is_new_login_device(uuid.uuid4(), "device-abc", db) is True


async def test_new_device_known_device_is_false():
    db = MagicMock()
    db.execute = AsyncMock(return_value=_make_count_result(total=3, same_device=2))
    assert await is_new_login_device(uuid.uuid4(), "device-abc", db) is False


async def test_new_device_first_ever_auth_is_false():
    db = MagicMock()
    db.execute = AsyncMock(return_value=_make_count_result(total=0, same_device=0))
    assert await is_new_login_device(uuid.uuid4(), "device-abc", db) is False


async def test_new_device_only_this_device_is_false():
    db = MagicMock()
    db.execute = AsyncMock(return_value=_make_count_result(total=1, same_device=1))
    assert await is_new_login_device(uuid.uuid4(), "device-abc", db) is False


# ══════════════════════════════════════════════════════════════
# send_welcome_email worker
# ══════════════════════════════════════════════════════════════


def _install_welcome(monkeypatch, *, user, email_raises=False):
    db = MagicMock()
    db.get = AsyncMock(return_value=user)
    monkeypatch.setattr(nt, "AsyncSessionLocal", lambda: _FakeSessionCtx(db))

    renderer = MagicMock()
    renderer.render = MagicMock(return_value=("<html>", "text"))
    monkeypatch.setattr(nt, "get_template_renderer", lambda: renderer)

    send = AsyncMock()
    if email_raises:
        send.side_effect = nt.EmailSendException("brevo down")
    email_service = MagicMock()
    email_service.send = send
    monkeypatch.setattr(nt, "get_email_service", lambda: email_service)

    create = AsyncMock()
    monkeypatch.setattr(nt.NotificationService, "create", create)
    return send, create


async def test_welcome_happy_path(monkeypatch):
    user = _make_user()
    send, create = _install_welcome(monkeypatch, user=user)

    out = await nt.send_welcome_email({}, str(user.id))

    assert out["skipped"] is False
    assert out["email_sent"] is True
    assert out["in_app_created"] is True
    send.assert_awaited_once()
    create.assert_awaited_once()
    kwargs = create.await_args.kwargs
    assert kwargs["category"] == "product"
    assert kwargs["source_kind"] == "product"
    assert kwargs["channel_used"] == "email"


async def test_welcome_email_failure_still_creates_in_app(monkeypatch):
    user = _make_user()
    send, create = _install_welcome(monkeypatch, user=user, email_raises=True)

    out = await nt.send_welcome_email({}, str(user.id))

    assert out["email_sent"] is False
    assert out["in_app_created"] is True
    create.assert_awaited_once()
    # email KO → channel_used 'skipped'
    assert create.await_args.kwargs["channel_used"] == "skipped"


async def test_welcome_bad_user_id_skips(monkeypatch):
    out = await nt.send_welcome_email({}, "not-a-uuid")
    assert out["skipped"] is True
    assert out["reason"] == "bad_user_id"


async def test_welcome_missing_user_skips(monkeypatch):
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    monkeypatch.setattr(nt, "AsyncSessionLocal", lambda: _FakeSessionCtx(db))
    out = await nt.send_welcome_email({}, str(uuid.uuid4()))
    assert out["skipped"] is True
    assert out["reason"] == "user_missing_or_inactive"


async def test_welcome_deleted_user_skips(monkeypatch):
    user = _make_user(deleted=True)
    db = MagicMock()
    db.get = AsyncMock(return_value=user)
    monkeypatch.setattr(nt, "AsyncSessionLocal", lambda: _FakeSessionCtx(db))
    out = await nt.send_welcome_email({}, str(user.id))
    assert out["skipped"] is True


# ══════════════════════════════════════════════════════════════
# send_security_alert worker
# ══════════════════════════════════════════════════════════════


def _install_security(monkeypatch, *, user, dispatch_raises=False):
    db = MagicMock()
    db.get = AsyncMock(return_value=user)
    monkeypatch.setattr(nt, "AsyncSessionLocal", lambda: _FakeSessionCtx(db))
    dispatch = AsyncMock()
    if dispatch_raises:
        dispatch.side_effect = RuntimeError("boom")
    monkeypatch.setattr(nt.NotificationDispatcher, "dispatch", dispatch)
    return dispatch


async def test_security_alert_dispatches_with_category_security(monkeypatch):
    user = _make_user()
    dispatch = _install_security(monkeypatch, user=user)

    out = await nt.send_security_alert(
        {},
        str(user.id),
        "new_device_login",
        "41.202.0.1",
        "NEXYA-App/1.1.6 Android",
        "device-abc",
        "2026-06-22T14:30:00+00:00",
    )

    assert out["skipped"] is False
    dispatch.assert_awaited_once()
    kwargs = dispatch.await_args.kwargs
    assert kwargs["category"] == "security"
    assert kwargs["source_kind"] == "security"
    assert kwargs["data"]["event_type"] == "new_device_login"
    assert kwargs["data"]["event_ip"] == "41.202.0.1"
    assert kwargs["data"]["event_time_utc"] == "2026-06-22 14:30 UTC"
    assert kwargs["data"]["deep_link"] == "nexya://settings/privacy"


async def test_security_alert_truncates_long_user_agent(monkeypatch):
    user = _make_user()
    dispatch = _install_security(monkeypatch, user=user)
    long_ua = "X" * 500

    await nt.send_security_alert(
        {},
        str(user.id),
        "password_changed",
        None,
        long_ua,
        None,
        "2026-06-22T14:30:00+00:00",
    )

    ua_sent = dispatch.await_args.kwargs["data"]["event_user_agent_truncated"]
    assert len(ua_sent) == nt._UA_DISPLAY_MAX_CHARS


async def test_security_alert_bad_user_id_skips(monkeypatch):
    out = await nt.send_security_alert(
        {}, "not-a-uuid", "password_changed", None, None, None, "2026-06-22T14:30:00+00:00"
    )
    assert out["skipped"] is True
    assert out["reason"] == "bad_user_id"


async def test_security_alert_missing_user_skips(monkeypatch):
    db = MagicMock()
    db.get = AsyncMock(return_value=None)
    monkeypatch.setattr(nt, "AsyncSessionLocal", lambda: _FakeSessionCtx(db))
    out = await nt.send_security_alert(
        {},
        str(uuid.uuid4()),
        "password_changed",
        None,
        None,
        None,
        "2026-06-22T14:30:00+00:00",
    )
    assert out["skipped"] is True


async def test_security_alert_dispatch_error_is_fail_safe(monkeypatch):
    user = _make_user()
    _install_security(monkeypatch, user=user, dispatch_raises=True)
    # Ne doit JAMAIS lever — fail-safe absolu.
    out = await nt.send_security_alert(
        {},
        str(user.id),
        "new_device_login",
        None,
        None,
        None,
        "2026-06-22T14:30:00+00:00",
    )
    assert out["skipped"] is True
    assert out["reason"] == "dispatch_error"


# ══════════════════════════════════════════════════════════════
# enqueue_* fail-safe (Redis down)
# ══════════════════════════════════════════════════════════════


async def test_enqueue_welcome_fail_safe_on_redis_down(monkeypatch):
    monkeypatch.setattr(nt, "_get_arq_pool", AsyncMock(side_effect=RuntimeError("redis down")))
    # Ne doit pas lever.
    await nt.enqueue_welcome_email(uuid.uuid4())


async def test_enqueue_security_alert_fail_safe_on_redis_down(monkeypatch):
    monkeypatch.setattr(nt, "_get_arq_pool", AsyncMock(side_effect=RuntimeError("redis down")))
    await nt.enqueue_security_alert(
        user_id=uuid.uuid4(),
        event_type="new_device_login",
        ip="1.2.3.4",
        user_agent="ua",
        device_id="dev",
    )


async def test_enqueue_welcome_calls_pool(monkeypatch):
    pool = MagicMock()
    pool.enqueue_job = AsyncMock()
    monkeypatch.setattr(nt, "_get_arq_pool", AsyncMock(return_value=pool))
    uid = uuid.uuid4()
    await nt.enqueue_welcome_email(uid)
    pool.enqueue_job.assert_awaited_once_with("send_welcome_email", str(uid))


async def test_enqueue_security_alert_passes_args(monkeypatch):
    pool = MagicMock()
    pool.enqueue_job = AsyncMock()
    monkeypatch.setattr(nt, "_get_arq_pool", AsyncMock(return_value=pool))
    uid = uuid.uuid4()
    await nt.enqueue_security_alert(
        user_id=uid,
        event_type="password_changed",
        ip="1.2.3.4",
        user_agent="ua",
        device_id="dev",
    )
    args = pool.enqueue_job.await_args.args
    assert args[0] == "send_security_alert"
    assert args[1] == str(uid)
    assert args[2] == "password_changed"
    assert args[3] == "1.2.3.4"
