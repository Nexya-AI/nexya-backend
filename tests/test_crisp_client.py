"""
Tests N4 — `app.integrations.crisp_client`.

Couvre :
1. MockCrispClient accumule les calls + retourne fake session_id.
2. MockCrispClient `force_fail=True` → retourne None.
3. RealCrispClient refuse construction sans website_id/api_key.
4. RealCrispClient mappe 401/403 → CrispAuthError (interne).
5. RealCrispClient mappe 5xx → CrispUnavailableError (interne).
6. RealCrispClient `create_conversation` fail-safe absolu (jamais raise).
7. Factory `get_crisp_client` retourne Mock si clé absente.
8. Factory retourne Real si clés présentes.
9. Factory singleton (même instance sur 2 appels).
10. `reset_crisp_client_for_tests` re-instancie.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from app.integrations import crisp_client as crisp_module
from app.integrations.crisp_client import (
    CrispAuthError,
    CrispConversationRequest,
    CrispUnavailableError,
    MockCrispClient,
    RealCrispClient,
    get_crisp_client,
    reset_crisp_client_for_tests,
)

# ══════════════════════════════════════════════════════════════
# MockCrispClient
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_crisp_accumulates_calls_and_returns_fake_id() -> None:
    client = MockCrispClient()
    req = CrispConversationRequest(
        nickname="Ivan",
        email="ivan@nexya.ai",
        message="Test message",
        metadata={"category": "payment"},
    )
    sid = await client.create_conversation(req)
    assert sid is not None
    assert sid.startswith("mock-session-")
    assert len(client.calls) == 1
    assert client.calls[0] is req


@pytest.mark.asyncio
async def test_mock_crisp_force_fail_returns_none() -> None:
    client = MockCrispClient(force_fail=True)
    req = CrispConversationRequest(
        nickname="x", email=None, message="m", metadata={}
    )
    sid = await client.create_conversation(req)
    assert sid is None
    # Le call est tout de même tracé pour assertions tests
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_mock_crisp_increments_session_id_counter() -> None:
    client = MockCrispClient()
    req = CrispConversationRequest(nickname="x", email=None, message="m", metadata={})
    sid1 = await client.create_conversation(req)
    sid2 = await client.create_conversation(req)
    assert sid1 != sid2  # counter increments


# ══════════════════════════════════════════════════════════════
# RealCrispClient — construction
# ══════════════════════════════════════════════════════════════


def test_real_crisp_refuses_empty_website_id() -> None:
    with pytest.raises(ValueError):
        RealCrispClient(website_id="", identifier="plugin", api_key="key")


def test_real_crisp_refuses_empty_api_key() -> None:
    with pytest.raises(ValueError):
        RealCrispClient(website_id="abc", identifier="plugin", api_key="")


def test_real_crisp_constructs_with_full_params() -> None:
    client = RealCrispClient(
        website_id="abc-123",
        identifier="plugin",
        api_key="secret-key",
    )
    assert client.name == "crisp"


# ══════════════════════════════════════════════════════════════
# RealCrispClient — mapping erreurs (méthode statique _raise_for_status)
# ══════════════════════════════════════════════════════════════


def test_real_crisp_raises_auth_error_on_401() -> None:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 401
    with pytest.raises(CrispAuthError):
        RealCrispClient._raise_for_status(response)


def test_real_crisp_raises_auth_error_on_403() -> None:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 403
    with pytest.raises(CrispAuthError):
        RealCrispClient._raise_for_status(response)


def test_real_crisp_raises_unavailable_on_500() -> None:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 503
    with pytest.raises(CrispUnavailableError):
        RealCrispClient._raise_for_status(response)


def test_real_crisp_no_raise_on_2xx() -> None:
    response = MagicMock(spec=httpx.Response)
    response.status_code = 200
    RealCrispClient._raise_for_status(response)  # pas de raise


def test_real_crisp_logs_4xx_without_raising() -> None:
    """400 = bad request → log but no raise (best-effort)."""
    response = MagicMock(spec=httpx.Response)
    response.status_code = 400
    response.text = "bad request"
    RealCrispClient._raise_for_status(response)  # pas de raise


# ══════════════════════════════════════════════════════════════
# RealCrispClient — fail-safe create_conversation (jamais raise)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_real_crisp_create_conversation_swallows_exceptions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RealCrispClient(website_id="x", identifier="plugin", api_key="k")

    async def _raise(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("network down")

    monkeypatch.setattr(client, "_create_session", _raise)
    req = CrispConversationRequest(nickname="x", email=None, message="m", metadata={})
    sid = await client.create_conversation(req)
    assert sid is None  # fail-safe — jamais raise


@pytest.mark.asyncio
async def test_real_crisp_create_conversation_returns_session_id_on_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = RealCrispClient(website_id="x", identifier="plugin", api_key="k")

    async def _ok(*args, **kwargs):  # type: ignore[no-untyped-def]
        return "fake-real-session-123"

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(client, "_create_session", _ok)
    monkeypatch.setattr(client, "_post_initial_message", _noop)
    monkeypatch.setattr(client, "_set_meta", _noop)

    req = CrispConversationRequest(nickname="x", email=None, message="m", metadata={})
    sid = await client.create_conversation(req)
    assert sid == "fake-real-session-123"


# ══════════════════════════════════════════════════════════════
# Factory get_crisp_client
# ══════════════════════════════════════════════════════════════


def test_factory_returns_mock_when_api_key_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_crisp_client_for_tests()
    monkeypatch.setattr(crisp_module, "_CLIENT", None)
    from app.config import settings

    monkeypatch.setattr(settings, "crisp_api_key", "")
    monkeypatch.setattr(settings, "crisp_website_id", "")

    client = get_crisp_client()
    assert isinstance(client, MockCrispClient)
    reset_crisp_client_for_tests()


def test_factory_returns_real_when_keys_present(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_crisp_client_for_tests()
    monkeypatch.setattr(crisp_module, "_CLIENT", None)
    from app.config import settings

    monkeypatch.setattr(settings, "crisp_api_key", "fake-key")
    monkeypatch.setattr(settings, "crisp_website_id", "fake-id")
    monkeypatch.setattr(settings, "crisp_identifier", "plugin")

    client = get_crisp_client()
    assert isinstance(client, RealCrispClient)
    reset_crisp_client_for_tests()


def test_factory_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    reset_crisp_client_for_tests()
    a = get_crisp_client()
    b = get_crisp_client()
    assert a is b
    reset_crisp_client_for_tests()


def test_reset_crisp_client_for_tests_re_instantiates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reset_crisp_client_for_tests()
    a = get_crisp_client()
    reset_crisp_client_for_tests()
    b = get_crisp_client()
    assert a is not b
