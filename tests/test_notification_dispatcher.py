"""
Tests F3 — NotificationDispatcher (cœur du lot F3).

Le dispatcher est l'orchestrateur dual-channel : lookup préférences,
tentative push, fallback email si push KO, INSERT row timeline. Fail-safe
absolu : ne raise JAMAIS au caller (worker arq qui ne doit pas crasher).

Couverture :
- pref='push' seulement → appelle FCM, pas email, channel_used='push'.
- pref='email' seulement → appelle email, pas FCM, channel_used='email'.
- pref='both' → appelle les deux, channel_used='both' si les deux OK.
- pref='none' → channel_used='skipped', aucun provider appelé.
- Fallback auto : pref='push' + push KO sur tous tokens + fallback_enabled
  → email envoyé, channel_used='email'.
- Fallback disabled : pref='push' + push KO + fallback_disabled → skipped.
- 0 device actif + pref='push' + fallback enabled → email direct.
- UNREGISTERED sur 1 token → soft-delete + continue avec les autres.
- Exception globale dans dispatch → INSERT skipped + log + PAS DE RAISE.
- data payload stringifie les valeurs non-str pour FCM.
- Catégorie 'security' → unsubscribe_url=None dans email.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.fcm import (
    FCMResult,
    FCMUnavailableError,
    FCMUnregisteredError,
)
from app.features.notifications.service import NotificationDispatcher


def _fake_user():
    u = MagicMock()
    u.id = uuid.uuid4()
    u.email = "ivan@example.com"
    u.display_name = "Ivan"
    u.username = "ivan"
    return u


def _fake_db_with_commit():
    db = MagicMock()
    db.add = MagicMock()
    db.commit = AsyncMock()
    db.refresh = AsyncMock()

    async def _execute(*args, **kwargs):
        r = MagicMock()
        r.rowcount = 0
        r.all = MagicMock(return_value=[])
        r.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        r.scalar_one_or_none = MagicMock(return_value=None)
        return r

    db.execute = AsyncMock(side_effect=_execute)
    return db


# ═══════════════════════════════════════════════════════════════════
# pref='none' → skipped, aucun provider
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_pref_none_skipped_no_providers_called(monkeypatch):
    user = _fake_user()
    db = _fake_db_with_commit()

    monkeypatch.setattr(
        "app.features.notifications.service."
        "NotificationPreferencesService.get_channel_for_category",
        AsyncMock(return_value="none"),
    )
    push_provider = MagicMock()
    push_provider.send_push = AsyncMock()
    monkeypatch.setattr(
        "app.features.notifications.service.get_fcm_provider",
        lambda: push_provider,
    )
    email_service = MagicMock()
    email_service.send = AsyncMock()
    monkeypatch.setattr(
        "app.features.notifications.service.get_email_service",
        lambda: email_service,
    )

    captured = {}
    real_create = NotificationDispatcher._persist_row

    async def _spy_persist(*args, **kwargs):
        captured.update(kwargs)
        # Retour factice pour ne pas polluer avec l'INSERT réel
        return MagicMock()

    monkeypatch.setattr(NotificationDispatcher, "_persist_row", staticmethod(_spy_persist))

    await NotificationDispatcher.dispatch(
        user=user,
        category="tasks",
        title="T",
        body="B",
        data=None,
        db=db,
    )
    assert captured["channel_used"] == "skipped"
    push_provider.send_push.assert_not_awaited()
    email_service.send.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════
# pref='push' + push OK sur 2 tokens → channel_used='push', no email
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_pref_push_success_no_email(monkeypatch):
    user = _fake_user()
    db = _fake_db_with_commit()

    monkeypatch.setattr(
        "app.features.notifications.service."
        "NotificationPreferencesService.get_channel_for_category",
        AsyncMock(return_value="push"),
    )
    monkeypatch.setattr(
        "app.features.notifications.service.auth_service.list_active_device_tokens",
        AsyncMock(return_value=["tokA", "tokB"]),
    )

    push_provider = MagicMock()
    push_provider.send_push = AsyncMock(return_value=FCMResult(success=True, message_id="msg-1"))
    monkeypatch.setattr(
        "app.features.notifications.service.get_fcm_provider",
        lambda: push_provider,
    )

    email_service = MagicMock()
    email_service.send = AsyncMock()
    monkeypatch.setattr(
        "app.features.notifications.service.get_email_service",
        lambda: email_service,
    )

    captured = {}

    async def _spy_persist(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(NotificationDispatcher, "_persist_row", staticmethod(_spy_persist))

    await NotificationDispatcher.dispatch(
        user=user,
        category="tasks",
        title="T",
        body="B",
        data={"deep_link": "nexya://task/abc"},
        db=db,
    )
    assert captured["channel_used"] == "push"
    assert captured["attempts_push"] == 2
    assert captured["push_message_id"] == "msg-1"
    email_service.send.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════
# pref='email' → pas de push, email envoyé
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_pref_email_only_sends_email(monkeypatch):
    user = _fake_user()
    db = _fake_db_with_commit()

    monkeypatch.setattr(
        "app.features.notifications.service."
        "NotificationPreferencesService.get_channel_for_category",
        AsyncMock(return_value="email"),
    )

    push_provider = MagicMock()
    push_provider.send_push = AsyncMock()
    monkeypatch.setattr(
        "app.features.notifications.service.get_fcm_provider",
        lambda: push_provider,
    )

    email_service = MagicMock()
    email_service.send = AsyncMock()
    monkeypatch.setattr(
        "app.features.notifications.service.get_email_service",
        lambda: email_service,
    )

    captured = {}

    async def _spy_persist(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(NotificationDispatcher, "_persist_row", staticmethod(_spy_persist))

    await NotificationDispatcher.dispatch(
        user=user,
        category="tasks",
        title="Tâche terminée",
        body="Résultat court",
        data={"task_title": "Ma tâche"},
        db=db,
    )
    assert captured["channel_used"] == "email"
    push_provider.send_push.assert_not_awaited()
    email_service.send.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════
# pref='both' → push + email, channel='both'
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_pref_both_sends_push_and_email(monkeypatch):
    user = _fake_user()
    db = _fake_db_with_commit()

    monkeypatch.setattr(
        "app.features.notifications.service."
        "NotificationPreferencesService.get_channel_for_category",
        AsyncMock(return_value="both"),
    )
    monkeypatch.setattr(
        "app.features.notifications.service.auth_service.list_active_device_tokens",
        AsyncMock(return_value=["tok1"]),
    )

    push_provider = MagicMock()
    push_provider.send_push = AsyncMock(return_value=FCMResult(success=True, message_id="msg-xyz"))
    monkeypatch.setattr(
        "app.features.notifications.service.get_fcm_provider",
        lambda: push_provider,
    )

    email_service = MagicMock()
    email_service.send = AsyncMock()
    monkeypatch.setattr(
        "app.features.notifications.service.get_email_service",
        lambda: email_service,
    )

    captured = {}

    async def _spy_persist(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(NotificationDispatcher, "_persist_row", staticmethod(_spy_persist))

    await NotificationDispatcher.dispatch(
        user=user,
        category="tasks",
        title="T",
        body="B",
        data={},
        db=db,
    )
    assert captured["channel_used"] == "both"
    push_provider.send_push.assert_awaited_once()
    email_service.send.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════
# Fallback auto : pref='push', tous les pushes Unavailable → email
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_push_all_fail_triggers_email_fallback(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "notification_fallback_email_enabled", True)

    user = _fake_user()
    db = _fake_db_with_commit()

    monkeypatch.setattr(
        "app.features.notifications.service."
        "NotificationPreferencesService.get_channel_for_category",
        AsyncMock(return_value="push"),
    )
    monkeypatch.setattr(
        "app.features.notifications.service.auth_service.list_active_device_tokens",
        AsyncMock(return_value=["tok1", "tok2"]),
    )
    monkeypatch.setattr(
        "app.features.notifications.service.auth_service.remove_invalid_token",
        AsyncMock(),
    )

    push_provider = MagicMock()
    push_provider.send_push = AsyncMock(side_effect=FCMUnavailableError("down", token="x"))
    monkeypatch.setattr(
        "app.features.notifications.service.get_fcm_provider",
        lambda: push_provider,
    )

    email_service = MagicMock()
    email_service.send = AsyncMock()
    monkeypatch.setattr(
        "app.features.notifications.service.get_email_service",
        lambda: email_service,
    )

    captured = {}

    async def _spy_persist(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(NotificationDispatcher, "_persist_row", staticmethod(_spy_persist))

    await NotificationDispatcher.dispatch(
        user=user,
        category="tasks",
        title="T",
        body="B",
        data={},
        db=db,
    )
    assert captured["channel_used"] == "email"
    assert captured["attempts_push"] == 2
    email_service.send.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════
# Fallback désactivé : push KO → skipped, pas d'email
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_fallback_disabled_no_email(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "notification_fallback_email_enabled", False)

    user = _fake_user()
    db = _fake_db_with_commit()

    monkeypatch.setattr(
        "app.features.notifications.service."
        "NotificationPreferencesService.get_channel_for_category",
        AsyncMock(return_value="push"),
    )
    monkeypatch.setattr(
        "app.features.notifications.service.auth_service.list_active_device_tokens",
        AsyncMock(return_value=["tok1"]),
    )

    push_provider = MagicMock()
    push_provider.send_push = AsyncMock(side_effect=FCMUnavailableError("down", token="tok1"))
    monkeypatch.setattr(
        "app.features.notifications.service.get_fcm_provider",
        lambda: push_provider,
    )

    email_service = MagicMock()
    email_service.send = AsyncMock()
    monkeypatch.setattr(
        "app.features.notifications.service.get_email_service",
        lambda: email_service,
    )

    captured = {}

    async def _spy_persist(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(NotificationDispatcher, "_persist_row", staticmethod(_spy_persist))

    await NotificationDispatcher.dispatch(
        user=user,
        category="tasks",
        title="T",
        body="B",
        data={},
        db=db,
    )
    assert captured["channel_used"] == "skipped"
    email_service.send.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════
# 0 device actif + pref='push' + fallback ON → email direct
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_no_tokens_fallback_email(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "notification_fallback_email_enabled", True)

    user = _fake_user()
    db = _fake_db_with_commit()

    monkeypatch.setattr(
        "app.features.notifications.service."
        "NotificationPreferencesService.get_channel_for_category",
        AsyncMock(return_value="push"),
    )
    monkeypatch.setattr(
        "app.features.notifications.service.auth_service.list_active_device_tokens",
        AsyncMock(return_value=[]),  # 0 device
    )

    push_provider = MagicMock()
    push_provider.send_push = AsyncMock()
    monkeypatch.setattr(
        "app.features.notifications.service.get_fcm_provider",
        lambda: push_provider,
    )

    email_service = MagicMock()
    email_service.send = AsyncMock()
    monkeypatch.setattr(
        "app.features.notifications.service.get_email_service",
        lambda: email_service,
    )

    captured = {}

    async def _spy_persist(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(NotificationDispatcher, "_persist_row", staticmethod(_spy_persist))

    await NotificationDispatcher.dispatch(
        user=user,
        category="tasks",
        title="T",
        body="B",
        data={},
        db=db,
    )
    assert captured["channel_used"] == "email"
    assert captured["attempts_push"] == 0  # pas de tentative push (0 tokens)
    push_provider.send_push.assert_not_awaited()
    email_service.send.assert_awaited_once()


# ═══════════════════════════════════════════════════════════════════
# UNREGISTERED → soft-delete token + retry avec les autres
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_unregistered_soft_deletes_tokens(monkeypatch):
    user = _fake_user()
    db = _fake_db_with_commit()

    monkeypatch.setattr(
        "app.features.notifications.service."
        "NotificationPreferencesService.get_channel_for_category",
        AsyncMock(return_value="push"),
    )
    monkeypatch.setattr(
        "app.features.notifications.service.auth_service.list_active_device_tokens",
        AsyncMock(return_value=["tokGood", "tokDead"]),
    )
    remove_mock = AsyncMock()
    monkeypatch.setattr(
        "app.features.notifications.service.auth_service.remove_invalid_token",
        remove_mock,
    )

    async def _send(tok, *, title, body, data=None):
        if tok == "tokDead":
            raise FCMUnregisteredError("expired", token=tok)
        return FCMResult(success=True, message_id=f"mock-{tok}")

    push_provider = MagicMock()
    push_provider.send_push = AsyncMock(side_effect=_send)
    monkeypatch.setattr(
        "app.features.notifications.service.get_fcm_provider",
        lambda: push_provider,
    )

    monkeypatch.setattr(
        "app.features.notifications.service.get_email_service",
        lambda: MagicMock(send=AsyncMock()),
    )

    captured = {}

    async def _spy_persist(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(NotificationDispatcher, "_persist_row", staticmethod(_spy_persist))

    await NotificationDispatcher.dispatch(
        user=user,
        category="tasks",
        title="T",
        body="B",
        data={},
        db=db,
    )
    # Le push a réussi sur tokGood → channel_used='push'
    assert captured["channel_used"] == "push"
    # tokDead a été soft-deleted
    remove_mock.assert_awaited_once()
    assert remove_mock.await_args.args[0] == "tokDead"


# ═══════════════════════════════════════════════════════════════════
# Data payload : valeurs non-str converties en str pour FCM
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_stringifies_data_payload(monkeypatch):
    user = _fake_user()
    db = _fake_db_with_commit()

    monkeypatch.setattr(
        "app.features.notifications.service."
        "NotificationPreferencesService.get_channel_for_category",
        AsyncMock(return_value="push"),
    )
    monkeypatch.setattr(
        "app.features.notifications.service.auth_service.list_active_device_tokens",
        AsyncMock(return_value=["tok"]),
    )

    captured_data = {}

    async def _send(tok, *, title, body, data=None):
        captured_data.update(data or {})
        return FCMResult(success=True, message_id="x")

    push_provider = MagicMock()
    push_provider.send_push = AsyncMock(side_effect=_send)
    monkeypatch.setattr(
        "app.features.notifications.service.get_fcm_provider",
        lambda: push_provider,
    )
    monkeypatch.setattr(
        "app.features.notifications.service.get_email_service",
        lambda: MagicMock(send=AsyncMock()),
    )

    async def _spy_persist(*args, **kwargs):
        return MagicMock()

    monkeypatch.setattr(NotificationDispatcher, "_persist_row", staticmethod(_spy_persist))

    task_id = uuid.uuid4()
    await NotificationDispatcher.dispatch(
        user=user,
        category="tasks",
        title="T",
        body="B",
        data={"task_id": task_id, "attempts": 3, "deep_link": "nexya://task/abc"},
        db=db,
    )
    # Toutes les valeurs sont str
    for v in captured_data.values():
        assert isinstance(v, str)
    assert captured_data["task_id"] == str(task_id)
    assert captured_data["attempts"] == "3"


# ═══════════════════════════════════════════════════════════════════
# Catégorie 'security' → unsubscribe_url=None dans email
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_security_category_no_unsubscribe_url(monkeypatch):
    user = _fake_user()
    db = _fake_db_with_commit()

    monkeypatch.setattr(
        "app.features.notifications.service."
        "NotificationPreferencesService.get_channel_for_category",
        AsyncMock(return_value="email"),
    )

    # Intercept render call pour vérifier unsubscribe_url
    captured_render = {}
    fake_renderer = MagicMock()

    def _render(template_name, **ctx):
        captured_render["template"] = template_name
        captured_render.update(ctx)
        return ("<html/>", "text")

    fake_renderer.render = _render
    monkeypatch.setattr(
        "app.features.notifications.service.get_template_renderer",
        lambda: fake_renderer,
    )

    email_service = MagicMock()
    email_service.send = AsyncMock()
    monkeypatch.setattr(
        "app.features.notifications.service.get_email_service",
        lambda: email_service,
    )

    async def _spy_persist(*args, **kwargs):
        return MagicMock()

    monkeypatch.setattr(NotificationDispatcher, "_persist_row", staticmethod(_spy_persist))

    await NotificationDispatcher.dispatch(
        user=user,
        category="security",
        title="Alerte",
        body="Connexion inhabituelle",
        data={"event_type": "unusual_login"},
        db=db,
    )
    assert captured_render["template"] == "account_security_alert"
    # Security = non-désinscriptible
    assert captured_render["unsubscribe_url"] is None


# ═══════════════════════════════════════════════════════════════════
# Dispatch catégorie 'tasks' avec notification_kind='reminder' → template reminder
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_task_reminder_uses_reminder_template(monkeypatch):
    user = _fake_user()
    db = _fake_db_with_commit()

    monkeypatch.setattr(
        "app.features.notifications.service."
        "NotificationPreferencesService.get_channel_for_category",
        AsyncMock(return_value="email"),
    )

    captured = {}
    fake_renderer = MagicMock()

    def _render(template_name, **ctx):
        captured["template"] = template_name
        return ("<html/>", "text")

    fake_renderer.render = _render
    monkeypatch.setattr(
        "app.features.notifications.service.get_template_renderer",
        lambda: fake_renderer,
    )
    monkeypatch.setattr(
        "app.features.notifications.service.get_email_service",
        lambda: MagicMock(send=AsyncMock()),
    )

    async def _spy_persist(*args, **kwargs):
        return MagicMock()

    monkeypatch.setattr(NotificationDispatcher, "_persist_row", staticmethod(_spy_persist))

    await NotificationDispatcher.dispatch(
        user=user,
        category="tasks",
        title="T",
        body="B",
        data={"notification_kind": "reminder"},
        db=db,
    )
    assert captured["template"] == "task_reminder"


# ═══════════════════════════════════════════════════════════════════
# Catégorie sans template (digest/product) → skipped email silencieux
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_digest_no_template_skipped(monkeypatch):
    user = _fake_user()
    db = _fake_db_with_commit()

    monkeypatch.setattr(
        "app.features.notifications.service."
        "NotificationPreferencesService.get_channel_for_category",
        AsyncMock(return_value="email"),
    )

    email_service = MagicMock()
    email_service.send = AsyncMock()
    monkeypatch.setattr(
        "app.features.notifications.service.get_email_service",
        lambda: email_service,
    )

    captured = {}

    async def _spy_persist(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(NotificationDispatcher, "_persist_row", staticmethod(_spy_persist))

    await NotificationDispatcher.dispatch(
        user=user,
        category="digest",
        title="Digest hebdo",
        body="Résumé",
        data={},
        db=db,
    )
    # digest n'a pas de template F3 → email skipped, channel_used='skipped'
    assert captured["channel_used"] == "skipped"
    email_service.send.assert_not_awaited()


# ═══════════════════════════════════════════════════════════════════
# Fail-safe : exception dans _try_push → captured + no raise
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_unexpected_exception_in_push_still_persists(monkeypatch):
    from app.config import settings

    # Fallback désactivé pour isoler le comportement exception-push-seul.
    monkeypatch.setattr(settings, "notification_fallback_email_enabled", False)

    user = _fake_user()
    db = _fake_db_with_commit()

    monkeypatch.setattr(
        "app.features.notifications.service."
        "NotificationPreferencesService.get_channel_for_category",
        AsyncMock(return_value="push"),
    )
    monkeypatch.setattr(
        "app.features.notifications.service.auth_service.list_active_device_tokens",
        AsyncMock(return_value=["tok"]),
    )

    async def _boom(tok, *, title, body, data=None):
        raise RuntimeError("provider bug")

    push_provider = MagicMock()
    push_provider.send_push = AsyncMock(side_effect=_boom)
    monkeypatch.setattr(
        "app.features.notifications.service.get_fcm_provider",
        lambda: push_provider,
    )
    monkeypatch.setattr(
        "app.features.notifications.service.get_email_service",
        lambda: MagicMock(send=AsyncMock()),
    )

    captured = {}

    async def _spy_persist(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(NotificationDispatcher, "_persist_row", staticmethod(_spy_persist))

    # Ne doit PAS lever — fail-safe absolu du dispatcher.
    await NotificationDispatcher.dispatch(
        user=user,
        category="tasks",
        title="T",
        body="B",
        data={},
        db=db,
    )
    # Exception RuntimeError dans le push + fallback désactivé
    # → skipped (aucun canal n'a livré).
    assert captured["channel_used"] == "skipped"
    assert captured["attempts_push"] == 1


# ═══════════════════════════════════════════════════════════════════
# Prefs lookup raise → fallback 'none' + persist skipped + no raise
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_dispatch_prefs_lookup_failure_safe(monkeypatch):
    user = _fake_user()
    db = _fake_db_with_commit()

    async def _boom(*args, **kwargs):
        raise RuntimeError("DB down")

    monkeypatch.setattr(
        "app.features.notifications.service."
        "NotificationPreferencesService.get_channel_for_category",
        _boom,
    )

    captured = {}

    async def _spy_persist(*args, **kwargs):
        captured.update(kwargs)
        return MagicMock()

    monkeypatch.setattr(NotificationDispatcher, "_persist_row", staticmethod(_spy_persist))

    # Pas de raise
    await NotificationDispatcher.dispatch(
        user=user,
        category="tasks",
        title="T",
        body="B",
        data={},
        db=db,
    )
    # preferred_channel fallback 'none' → skipped
    assert captured["channel_used"] == "skipped"
