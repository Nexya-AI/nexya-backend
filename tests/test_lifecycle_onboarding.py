"""Phase 3 — tests emails lifecycle / onboarding.

Couvre :
    - LifecycleEmailService.claim (idempotence INSERT ON CONFLICT)
    - LifecycleEmailService.release (fail-safe)
    - LifecycleEmailService.should_send_lifecycle (gate consentement)
    - _build_unsubscribe_url
    - _process_onboarding_user (gate → claim → envoi → release si échec)
    - send_onboarding_emails (orchestration, kill-switch, agrégation, fail-safe)

Mock-first strict : aucun Redis/DB/Brevo réel.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

import workers.lifecycle_tasks as lt
from app.features.lifecycle.service import LifecycleEmailService

pytestmark = pytest.mark.asyncio


class _FakeSessionCtx:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, *exc):
        return False


def _make_user():
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = "ivan@nexyalabs.com"
    user.display_name = "Ivan"
    user.username = "ivan"
    return user


# ══════════════════════════════════════════════════════════════
# LifecycleEmailService.claim / release / should_send_lifecycle
# ══════════════════════════════════════════════════════════════


async def test_claim_returns_true_when_newly_inserted():
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = uuid.uuid4()  # row insérée
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    assert await LifecycleEmailService.claim(uuid.uuid4(), "onboarding_d1", db) is True
    db.commit.assert_awaited()


async def test_claim_returns_false_on_conflict():
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None  # conflit → déjà envoyé
    db.execute = AsyncMock(return_value=result)
    db.commit = AsyncMock()
    assert await LifecycleEmailService.claim(uuid.uuid4(), "onboarding_d1", db) is False


async def test_release_is_fail_safe():
    db = MagicMock()
    db.execute = AsyncMock(side_effect=RuntimeError("db down"))
    db.commit = AsyncMock()
    # Ne doit pas lever.
    await LifecycleEmailService.release(uuid.uuid4(), "onboarding_d1", db)


async def test_should_send_true_when_channel_email(monkeypatch):
    from app.features.notifications.preferences import NotificationPreferencesService

    monkeypatch.setattr(
        NotificationPreferencesService,
        "get_channel_for_category",
        AsyncMock(return_value="email"),
    )
    assert (
        await LifecycleEmailService.should_send_lifecycle(_make_user(), "product", MagicMock())
        is True
    )


async def test_should_send_false_when_channel_none(monkeypatch):
    from app.features.notifications.preferences import NotificationPreferencesService

    monkeypatch.setattr(
        NotificationPreferencesService,
        "get_channel_for_category",
        AsyncMock(return_value="none"),
    )
    assert (
        await LifecycleEmailService.should_send_lifecycle(_make_user(), "product", MagicMock())
        is False
    )


async def test_should_send_false_on_prefs_error(monkeypatch):
    from app.features.notifications.preferences import NotificationPreferencesService

    monkeypatch.setattr(
        NotificationPreferencesService,
        "get_channel_for_category",
        AsyncMock(side_effect=RuntimeError("boom")),
    )
    # Fail-safe conservateur : on n'envoie pas si on ne peut pas vérifier.
    assert (
        await LifecycleEmailService.should_send_lifecycle(_make_user(), "product", MagicMock())
        is False
    )


# ══════════════════════════════════════════════════════════════
# _build_unsubscribe_url
# ══════════════════════════════════════════════════════════════


def test_build_unsubscribe_url_ok(monkeypatch):
    monkeypatch.setattr(lt, "create_unsubscribe_token", lambda uid, cat: "TOKEN123")
    monkeypatch.setattr(lt.settings, "frontend_unsubscribe_url", "https://app.x/unsubscribe")
    url = lt._build_unsubscribe_url(uuid.uuid4(), "product")
    assert url == "https://app.x/unsubscribe?token=TOKEN123"


def test_build_unsubscribe_url_fail_safe(monkeypatch):
    monkeypatch.setattr(
        lt, "create_unsubscribe_token", MagicMock(side_effect=RuntimeError("kaboom"))
    )
    assert lt._build_unsubscribe_url(uuid.uuid4(), "product") is None


# ══════════════════════════════════════════════════════════════
# _process_onboarding_user
# ══════════════════════════════════════════════════════════════


def _install_process(monkeypatch, *, should_send=True, claimed=True, send_raises=False):
    monkeypatch.setattr(
        LifecycleEmailService, "should_send_lifecycle", AsyncMock(return_value=should_send)
    )
    monkeypatch.setattr(LifecycleEmailService, "claim", AsyncMock(return_value=claimed))
    release = AsyncMock()
    monkeypatch.setattr(LifecycleEmailService, "release", release)
    monkeypatch.setattr(lt, "_build_unsubscribe_url", lambda uid, cat: "https://x/unsub")
    renderer = MagicMock()
    renderer.render = MagicMock(return_value=("<html>", "text"))
    monkeypatch.setattr(lt, "get_template_renderer", lambda: renderer)
    send = AsyncMock()
    if send_raises:
        send.side_effect = lt.EmailSendException("brevo down")
    svc = MagicMock()
    svc.send = send
    monkeypatch.setattr(lt, "get_email_service", lambda: svc)
    return send, release


async def test_process_user_happy_path(monkeypatch):
    send, release = _install_process(monkeypatch)
    out = await lt._process_onboarding_user(
        user=_make_user(),
        email_key="onboarding_d1",
        template_name="onboarding_d1",
        subject="Sujet",
        db=MagicMock(),
    )
    assert out == "sent"
    send.assert_awaited_once()
    release.assert_not_awaited()


async def test_process_user_optout_skips_without_claim(monkeypatch):
    send, release = _install_process(monkeypatch, should_send=False)
    claim = AsyncMock()
    monkeypatch.setattr(LifecycleEmailService, "claim", claim)
    out = await lt._process_onboarding_user(
        user=_make_user(),
        email_key="onboarding_d1",
        template_name="onboarding_d1",
        subject="S",
        db=MagicMock(),
    )
    assert out == "skipped_optout"
    claim.assert_not_awaited()  # on ne claim même pas un désinscrit
    send.assert_not_awaited()


async def test_process_user_race_when_already_claimed(monkeypatch):
    send, _ = _install_process(monkeypatch, claimed=False)
    out = await lt._process_onboarding_user(
        user=_make_user(),
        email_key="onboarding_d1",
        template_name="onboarding_d1",
        subject="S",
        db=MagicMock(),
    )
    assert out == "skipped_race"
    send.assert_not_awaited()


async def test_process_user_send_failure_releases_slot(monkeypatch):
    send, release = _install_process(monkeypatch, send_raises=True)
    user = _make_user()
    out = await lt._process_onboarding_user(
        user=user,
        email_key="onboarding_d1",
        template_name="onboarding_d1",
        subject="S",
        db=MagicMock(),
    )
    assert out == "failed"
    # Le slot est libéré pour réessayer au prochain run.
    release.assert_awaited_once()
    assert release.await_args.args[0] == user.id


# ══════════════════════════════════════════════════════════════
# send_onboarding_emails (orchestration)
# ══════════════════════════════════════════════════════════════


async def test_onboarding_disabled_skips(monkeypatch):
    monkeypatch.setattr(lt.settings, "onboarding_emails_enabled", False)
    out = await lt.send_onboarding_emails({})
    assert out["skipped"] is True
    assert out["reason"] == "disabled"


async def test_onboarding_aggregates_3_stages(monkeypatch):
    monkeypatch.setattr(lt.settings, "onboarding_emails_enabled", True)
    monkeypatch.setattr(lt.settings, "onboarding_window_grace_days", 2)
    monkeypatch.setattr(lt.settings, "onboarding_batch_size", 500)

    # Chaque étape (3) renvoie 2 users.
    users = [_make_user(), _make_user()]
    db = MagicMock()
    result = MagicMock()
    result.scalars.return_value.all.return_value = users
    db.execute = AsyncMock(return_value=result)
    monkeypatch.setattr(lt, "AsyncSessionLocal", lambda: _FakeSessionCtx(db))
    monkeypatch.setattr(lt, "_process_onboarding_user", AsyncMock(return_value="sent"))

    out = await lt.send_onboarding_emails({})

    assert out["skipped"] is False
    # 3 étapes × 2 users = 6 envois.
    assert out["sent"] == 6


async def test_onboarding_stage_exception_is_fail_safe(monkeypatch):
    monkeypatch.setattr(lt.settings, "onboarding_emails_enabled", True)
    # AsyncSessionLocal lève → chaque étape catch, totals à 0, pas de crash.
    monkeypatch.setattr(lt, "AsyncSessionLocal", MagicMock(side_effect=RuntimeError("db down")))
    out = await lt.send_onboarding_emails({})
    assert out["skipped"] is False
    assert out["sent"] == 0
