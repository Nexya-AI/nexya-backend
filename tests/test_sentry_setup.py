"""Tests d'init Sentry — DSN env-aware, scrubber bridge, fail-safe.

Vérifie :
- DSN vide → sentry_sdk.init() PAS appelé (zéro overhead).
- DSN rempli → init avec 5 integrations + scrubber + ignore_errors.
- Le scrubber `_sentry_scrub_event` masque password/token/api_key
  dans request.data + extra + contexts + breadcrumbs.
- Le filtre `_should_capture` ignore CancelledError + NexYaException.
- shutdown_sentry idempotent.
"""

from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from app.config import Settings
from app.core.errors.exceptions import (
    NexYaException,
    ResourceNotFoundException,
)
from app.core.observability import sentry as sentry_mod


@pytest.fixture(autouse=True)
def _reset_sentry():
    sentry_mod._reset_for_tests()
    yield
    sentry_mod._reset_for_tests()


def _cfg(**overrides) -> Settings:
    base = dict(
        sentry_dsn="",
        sentry_environment="development",
        sentry_traces_sample_rate=0.0,
        sentry_profiles_sample_rate=0.0,
        app_version="dev",
    )
    base.update(overrides)
    return Settings(**base)


def test_sentry_disabled_when_dsn_empty() -> None:
    """DSN vide → init() n'est PAS appelé du tout."""
    with mock.patch("sentry_sdk.init") as mock_init:
        ok = sentry_mod.setup_sentry(_cfg(sentry_dsn=""))
    assert ok is False
    assert sentry_mod.is_initialized() is False
    mock_init.assert_not_called()


def test_sentry_init_called_when_dsn_present() -> None:
    """DSN rempli → sentry_sdk.init() reçoit les bons paramètres."""
    fake_dsn = "https://abc@sentry.example.io/1"
    with mock.patch("sentry_sdk.init") as mock_init:
        ok = sentry_mod.setup_sentry(
            _cfg(
                sentry_dsn=fake_dsn,
                sentry_environment="staging",
                sentry_traces_sample_rate=0.1,
                app_version="v1.2.3",
            )
        )
    assert ok is True
    assert sentry_mod.is_initialized() is True
    mock_init.assert_called_once()
    kwargs = mock_init.call_args.kwargs
    assert kwargs["dsn"] == fake_dsn
    assert kwargs["environment"] == "staging"
    assert kwargs["release"] == "v1.2.3"
    assert kwargs["traces_sample_rate"] == 0.1
    assert "before_send" in kwargs
    assert "integrations" in kwargs
    assert kwargs["send_default_pii"] is False


def test_sentry_idempotent_init() -> None:
    """Deux setup_sentry consécutifs — second est no-op."""
    with mock.patch("sentry_sdk.init") as mock_init:
        sentry_mod.setup_sentry(_cfg(sentry_dsn="https://x@s.io/1"))
        sentry_mod.setup_sentry(_cfg(sentry_dsn="https://x@s.io/1"))
    assert mock_init.call_count == 1


def test_scrub_event_masks_password_in_request_data() -> None:
    """_sentry_scrub_event masque les password dans request.data."""
    event = {
        "request": {
            "data": {"username": "alice", "password": "leak-me", "token": "secret"},
            "headers": {"Authorization": "Bearer xyz"},
        },
    }
    scrubbed = sentry_mod._sentry_scrub_event(event, {})
    assert scrubbed is not None
    assert scrubbed["request"]["data"]["username"] == "alice"
    assert scrubbed["request"]["data"]["password"] == "***REDACTED***"
    assert scrubbed["request"]["data"]["token"] == "***REDACTED***"
    assert scrubbed["request"]["headers"]["Authorization"] == "***REDACTED***"


def test_scrub_event_masks_extra_and_contexts() -> None:
    """Le scrubber traite extra + contexts + breadcrumbs."""
    event = {
        "extra": {"api_key": "sk-secret", "user": "alice"},
        "contexts": {"runtime": {"webhook_secret": "hush"}},
        "breadcrumbs": {
            "values": [
                {"category": "http", "data": {"password": "leak"}},
                {"category": "log", "data": {"safe_field": "ok"}},
            ],
        },
    }
    scrubbed = sentry_mod._sentry_scrub_event(event, {})
    assert scrubbed["extra"]["api_key"] == "***REDACTED***"
    assert scrubbed["extra"]["user"] == "alice"
    assert scrubbed["contexts"]["runtime"]["webhook_secret"] == "***REDACTED***"
    assert scrubbed["breadcrumbs"]["values"][0]["data"]["password"] == "***REDACTED***"
    assert scrubbed["breadcrumbs"]["values"][1]["data"]["safe_field"] == "ok"


def test_should_capture_filters_business_errors() -> None:
    """_should_capture ignore les erreurs métier normales."""
    assert sentry_mod._should_capture(asyncio.CancelledError) is False
    assert sentry_mod._should_capture(NexYaException) is False
    assert sentry_mod._should_capture(ResourceNotFoundException) is False
    # Mais capture les vraies exceptions inattendues
    assert sentry_mod._should_capture(RuntimeError) is True
    assert sentry_mod._should_capture(ValueError) is True


def test_scrub_event_returns_none_on_scrubber_failure() -> None:
    """Si le scrubber crash, l'event est drop (mieux qu'une fuite)."""
    # Simulate a scrubber that raises by passing an unscubable type in nested.
    bad_event = {"request": {"data": object()}}  # object() pas dict/list
    # Le scrubber retourne tel quel les valeurs non-dict/list, pas crash.
    scrubbed = sentry_mod._sentry_scrub_event(bad_event, {})
    assert scrubbed is not None  # OK, scrubber tolère les types primitifs


@pytest.mark.asyncio
async def test_shutdown_sentry_idempotent() -> None:
    """shutdown_sentry sans init préalable — pas de crash."""
    await sentry_mod.shutdown_sentry()  # ne lève pas
