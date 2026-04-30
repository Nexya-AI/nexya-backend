"""
Tests factory FCM — F2.

Couvre :
- Mock retourné si aucune clé Firebase (comportement par défaut).
- Mock retourné si `fcm_mock_enabled=True` même avec clé.
- `reset_fcm_provider_for_tests` bien réinitialise.
"""

from __future__ import annotations

from app.ai.fcm import (
    MockFCMProvider,
    get_fcm_provider,
    reset_fcm_provider_for_tests,
)
from app.ai.fcm.runtime import _mock_warning_emitted  # noqa: F401 — testé indirect


def test_factory_returns_mock_when_no_key(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "fcm_service_account_json", "")
    monkeypatch.setattr(settings, "fcm_service_account_file", "")
    monkeypatch.setattr(settings, "fcm_mock_enabled", False)
    reset_fcm_provider_for_tests()

    provider = get_fcm_provider()
    assert isinstance(provider, MockFCMProvider)


def test_factory_force_mock_via_flag(monkeypatch):
    from app.config import settings

    # Même avec une clé posée, le flag force le Mock.
    monkeypatch.setattr(
        settings,
        "fcm_service_account_json",
        '{"project_id":"p","private_key":"k","client_email":"e"}',
    )
    monkeypatch.setattr(settings, "fcm_mock_enabled", True)
    reset_fcm_provider_for_tests()

    provider = get_fcm_provider()
    assert isinstance(provider, MockFCMProvider)


def test_factory_singleton(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "fcm_service_account_json", "")
    monkeypatch.setattr(settings, "fcm_service_account_file", "")
    monkeypatch.setattr(settings, "fcm_mock_enabled", False)
    reset_fcm_provider_for_tests()

    a = get_fcm_provider()
    b = get_fcm_provider()
    assert a is b


def test_reset_for_tests_creates_new_instance(monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "fcm_service_account_json", "")
    monkeypatch.setattr(settings, "fcm_mock_enabled", False)
    reset_fcm_provider_for_tests()
    first = get_fcm_provider()
    reset_fcm_provider_for_tests()
    second = get_fcm_provider()
    assert first is not second
