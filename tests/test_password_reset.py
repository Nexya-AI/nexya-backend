"""
Tests — Session A1 : Reset password + email transactionnel.

Couverture :
1. JWT password_reset — create/decode round-trip
2. JWT password_reset — fingerprint invalide après changement de hash
3. JWT password_reset — token expiré → ResetTokenExpiredException
4. JWT password_reset — purpose ≠ password_reset → ResetTokenInvalidException
5. Template renderer — password_reset.html + .txt rendent avec le contexte
6. MockEmailService — accumule les messages envoyés
7. Router /auth/forgot-password — 200 générique même si email inexistant (anti-enumeration)
8. Router /auth/forgot-password — 200 + envoi quand email existe
9. Router /auth/forgot-password — 422 si email malformé
10. Router /auth/reset-password — 400 RESET_TOKEN_INVALID si token bidon
11. Router /auth/reset-password — 400 RESET_TOKEN_EXPIRED si token expiré
12. Router /auth/reset-password — 422 si new_password faible
13. Router /auth/reset-password — 200 happy-path (monkeypatch service)
14. Router /auth/reset-password — 429 si rate limit dépassé

Discipline : aucun Redis, aucune DB. Monkeypatch + AsyncMock.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.auth.password_reset import (
    TOKEN_PURPOSE_RESET,
    create_password_reset_token,
    decode_password_reset_token,
    verify_password_hash_fingerprint,
)
from app.core.database.postgres import get_db
from app.core.email.base import EmailMessage
from app.core.email.mock import MockEmailService
from app.core.email.renderer import TemplateRenderer
from app.core.errors.exceptions import (
    ResetTokenExpiredException,
    ResetTokenInvalidException,
)
from app.features.auth import service as auth_service
from app.features.auth.models import User
from app.main import app

_FAKE_USER_ID = uuid.UUID("6b59c0a7-1b2c-4d5e-8f7a-9b0c1d2e3f99")
_FAKE_HASH_A = "$2b$12$KIXxxxxxxxxxxxxxxxxxxAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
_FAKE_HASH_B = "$2b$12$KIXyyyyyyyyyyyyyyyyyyBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"


# ══════════════════════════════════════════════════════════════
# JWT — password_reset tokens
# ══════════════════════════════════════════════════════════════


def test_password_reset_token_round_trip() -> None:
    token = create_password_reset_token(_FAKE_USER_ID, _FAKE_HASH_A)
    payload = decode_password_reset_token(token)
    assert payload["sub"] == str(_FAKE_USER_ID)
    assert payload["purpose"] == TOKEN_PURPOSE_RESET
    assert len(payload["pwh_fp"]) == 16
    assert "jti" in payload
    verify_password_hash_fingerprint(payload, _FAKE_HASH_A)


def test_password_reset_fingerprint_mismatch_after_password_change() -> None:
    token = create_password_reset_token(_FAKE_USER_ID, _FAKE_HASH_A)
    payload = decode_password_reset_token(token)
    with pytest.raises(ResetTokenInvalidException):
        verify_password_hash_fingerprint(payload, _FAKE_HASH_B)


def test_password_reset_token_expired_raises() -> None:
    # On forge un token déjà expiré en encodant manuellement avec exp dans le passé
    now = datetime.now(UTC)
    payload = {
        "sub": str(_FAKE_USER_ID),
        "purpose": TOKEN_PURPOSE_RESET,
        "pwh_fp": "deadbeefdeadbeef",
        "iat": now - timedelta(minutes=30),
        "exp": now - timedelta(minutes=1),
        "jti": str(uuid.uuid4()),
    }
    token = pyjwt.encode(payload, settings.jwt_private_key, algorithm="RS256")

    with pytest.raises(ResetTokenExpiredException):
        decode_password_reset_token(token)


def test_password_reset_token_wrong_purpose_rejected() -> None:
    now = datetime.now(UTC)
    payload = {
        "sub": str(_FAKE_USER_ID),
        "purpose": "access",  # wrong on purpose
        "pwh_fp": "deadbeefdeadbeef",
        "iat": now,
        "exp": now + timedelta(minutes=15),
        "jti": str(uuid.uuid4()),
    }
    token = pyjwt.encode(payload, settings.jwt_private_key, algorithm="RS256")

    with pytest.raises(ResetTokenInvalidException):
        decode_password_reset_token(token)


def test_password_reset_token_malformed_rejected() -> None:
    with pytest.raises(ResetTokenInvalidException):
        decode_password_reset_token("definitely.not.a.valid.jwt")


# ══════════════════════════════════════════════════════════════
# Email — renderer + MockEmailService
# ══════════════════════════════════════════════════════════════


def test_renderer_password_reset_html_and_txt() -> None:
    renderer = TemplateRenderer()
    html, text = renderer.render(
        "password_reset",
        user_name="Ivan",
        reset_url="https://app.nexya.ai/reset-password?token=abc",
        expires_minutes=15,
        # F3 — footer partiel inclus exige `unsubscribe_url`. La catégorie
        # `security` (dont reset-password fait partie) n'est pas
        # désinscriptible par obligation légale → None masque la ligne.
        unsubscribe_url=None,
    )

    assert "Ivan" in html
    assert "https://app.nexya.ai/reset-password?token=abc" in html
    assert "15 minutes" in html
    assert "NEXYA" in html

    assert "Ivan" in text
    assert "https://app.nexya.ai/reset-password?token=abc" in text
    assert "15 minutes" in text


def test_renderer_password_reset_without_user_name() -> None:
    # user_name vide ne doit pas planter le rendu (champ optionnel)
    renderer = TemplateRenderer()
    html, text = renderer.render(
        "password_reset",
        user_name="",
        reset_url="https://app.nexya.ai/reset-password?token=xyz",
        expires_minutes=15,
        # F3 — cf. commentaire test ci-dessus.
        unsubscribe_url=None,
    )
    assert "Bonjour," in html
    assert "Bonjour," in text


@pytest.mark.asyncio
async def test_mock_email_service_accumulates() -> None:
    service = MockEmailService()
    msg = EmailMessage(
        to_email="test@nexya.ai",
        to_name="Test",
        subject="S",
        html_body="<p>h</p>",
        text_body="t",
        tags=["password_reset"],
    )
    await service.send(msg)
    await service.send(msg)
    assert len(service.sent) == 2
    service.clear()
    assert service.sent == []


# ══════════════════════════════════════════════════════════════
# Router — POST /auth/forgot-password
# ══════════════════════════════════════════════════════════════


def _noop_rate_limit_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Neutralise les 2 rate limiters IP du scénario forgot/reset."""
    from app.features.auth import router as auth_router_mod

    async def _noop(request) -> None:  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(auth_router_mod, "rate_limit_forgot_password_ip", _noop)
    monkeypatch.setattr(auth_router_mod, "rate_limit_reset_password_ip", _noop)


@pytest.fixture
def client_with_fake_db(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """TestClient avec `get_db` overridé (session non consultée — service patché)."""
    fake_session = MagicMock()
    app.dependency_overrides[get_db] = lambda: fake_session  # generator not needed
    _noop_rate_limit_deps(monkeypatch)
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.pop(get_db, None)


def test_forgot_password_happy_path_calls_service(
    client_with_fake_db: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_service, "forgot_password", called)

    response = client_with_fake_db.post(
        "/auth/forgot-password",
        json={"email": "free@nexya.ai"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert "lien de réinitialisation" in body["data"]["message"]
    assert called.await_count == 1


def test_forgot_password_unknown_email_returns_200_anti_enumeration(
    client_with_fake_db: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Le service no-op silencieusement quand le compte n'existe pas —
    # le router ne doit jamais lever 404
    async def _silent_service(body, db, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(auth_service, "forgot_password", _silent_service)

    response = client_with_fake_db.post(
        "/auth/forgot-password",
        json={"email": "ghost-account-12345@example.com"},
    )
    assert response.status_code == 200
    assert response.json()["success"] is True


def test_forgot_password_invalid_email_422(
    client_with_fake_db: TestClient,
) -> None:
    response = client_with_fake_db.post(
        "/auth/forgot-password",
        json={"email": "not-an-email"},
    )
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# Router — POST /auth/reset-password
# ══════════════════════════════════════════════════════════════


def test_reset_password_invalid_token_returns_400(
    client_with_fake_db: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_invalid(body, db, **kwargs):  # type: ignore[no-untyped-def]
        raise ResetTokenInvalidException()

    monkeypatch.setattr(auth_service, "reset_password", _raise_invalid)

    response = client_with_fake_db.post(
        "/auth/reset-password",
        json={"token": "bogus.jwt.token", "new_password": "StrongPass123"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "RESET_TOKEN_INVALID"


def test_reset_password_expired_token_returns_400(
    client_with_fake_db: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _raise_expired(body, db, **kwargs):  # type: ignore[no-untyped-def]
        raise ResetTokenExpiredException()

    monkeypatch.setattr(auth_service, "reset_password", _raise_expired)

    response = client_with_fake_db.post(
        "/auth/reset-password",
        json={"token": "expired.jwt.token", "new_password": "StrongPass123"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "RESET_TOKEN_EXPIRED"


def test_reset_password_weak_password_returns_422(
    client_with_fake_db: TestClient,
) -> None:
    response = client_with_fake_db.post(
        "/auth/reset-password",
        json={"token": "aaaaaaaaaa.bb.cc", "new_password": "tooweak"},
    )
    assert response.status_code == 422


def test_reset_password_happy_path(
    client_with_fake_db: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_service, "reset_password", called)

    response = client_with_fake_db.post(
        "/auth/reset-password",
        json={"token": "some-valid.jwt.token", "new_password": "BrandNewPass123"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert "mot de passe" in body["data"]["message"].lower()
    assert called.await_count == 1


def test_reset_password_rate_limit_returns_429(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Rate limiter lève → 429 propagé par le handler global."""
    from app.core.errors.exceptions import RateLimitIPException
    from app.features.auth import router as auth_router_mod

    async def _allow(request) -> None:  # forgot-password laissé passer
        return None

    async def _block(request) -> None:  # reset-password bloqué
        raise RateLimitIPException(retry_after=3600)

    monkeypatch.setattr(auth_router_mod, "rate_limit_forgot_password_ip", _allow)
    monkeypatch.setattr(auth_router_mod, "rate_limit_reset_password_ip", _block)

    fake_session = MagicMock()
    app.dependency_overrides[get_db] = lambda: fake_session
    try:
        with TestClient(app) as client:
            response = client.post(
                "/auth/reset-password",
                json={"token": "aaaaaaaaaa.bb.cc", "new_password": "StrongPass123"},
            )
        assert response.status_code == 429
        body = response.json()
        assert body["code"] == "RATE_LIMIT_IP"
        assert body["data"]["retry_after"] == 3600
    finally:
        app.dependency_overrides.pop(get_db, None)


# ══════════════════════════════════════════════════════════════
# Service — forgot_password anti-enumeration no-op
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_service_forgot_password_no_account_is_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si l'user n'existe pas, le service ne doit RIEN envoyer."""
    from app.features.auth.schemas import ForgotPasswordRequest

    fake_db = MagicMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none = MagicMock(return_value=None)
    fake_db.execute = AsyncMock(return_value=result_mock)

    mock_service = MockEmailService()

    def _get_mock():  # type: ignore[no-untyped-def]
        return mock_service

    monkeypatch.setattr("app.features.auth.service.get_email_service", _get_mock)

    await auth_service.forgot_password(
        ForgotPasswordRequest(email="ghost@nexya.ai"),
        fake_db,
    )

    assert mock_service.sent == []


@pytest.mark.asyncio
async def test_service_forgot_password_existing_user_sends_email(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.auth.schemas import ForgotPasswordRequest

    fake_user = MagicMock(spec=User)
    fake_user.id = _FAKE_USER_ID
    fake_user.email = "free@nexya.ai"
    fake_user.display_name = "Ivan"
    fake_user.username = "ivan"
    fake_user.password_hash = _FAKE_HASH_A

    fake_db = MagicMock()
    result_mock = MagicMock()
    result_mock.scalar_one_or_none = MagicMock(return_value=fake_user)
    fake_db.execute = AsyncMock(return_value=result_mock)
    # log_auth_event commit la ligne audit à la fin du flow — on neutralise
    # pour ne pas dépendre d'une vraie DB dans ce test unitaire.
    fake_db.flush = AsyncMock()
    fake_db.commit = AsyncMock()

    mock_service = MockEmailService()
    monkeypatch.setattr("app.features.auth.service.get_email_service", lambda: mock_service)

    # Rate limiter email : neutralisé
    async def _noop(email: str) -> None:
        return None

    monkeypatch.setattr("app.features.auth.service.rate_limit_forgot_password_email", _noop)

    # Audit forensic : neutralisé pour ce test (couvert par test_auth_hardening_a3)
    async def _noop_audit(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr("app.features.auth.service.log_auth_event", _noop_audit)

    await auth_service.forgot_password(
        ForgotPasswordRequest(email="free@nexya.ai"),
        fake_db,
    )

    assert len(mock_service.sent) == 1
    sent = mock_service.sent[0]
    assert sent.to_email == "free@nexya.ai"
    assert sent.subject.startswith("Réinitialisation")
    assert "password_reset" in sent.tags
    # Le token doit être présent dans le corps HTML et le corps texte
    assert "reset-password?token=" in sent.html_body
    assert "reset-password?token=" in sent.text_body
