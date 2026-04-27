"""
Tests N2 — `app.features.auth.router` : intégration du flux complet auth.

Ces tests valident le câblage routeur ↔ service ↔ JWT décodage sur les
endpoints authentifiants qui n'étaient pas déjà couverts par
`test_auth_hardening_a3.py` (register pipeline) ou `test_password_reset.py`
(forgot/reset).

Couvre :
1. `POST /auth/refresh` — délégation à `auth_service.refresh`.
2. `POST /auth/logout` — décodage de l'access token + délégation
   `auth_service.logout(jti, exp)`.
3. `GET /user/profile` — refus 401/403 sans Authorization.
4. `PUT /user/profile` — délégation `update_profile`.
5. `PUT /user/password` — délégation `change_password`.
6. `DELETE /user/account` — RGPD, décodage access token + délégation.
7. `POST /user/device-token` — enregistrement FCM.
8. `DELETE /user/device-token` — désenregistrement FCM.
9. Smoke : tous les endpoints attendus sont bien montés.

Mock-first strict : aucun Postgres, aucun Redis. Service patché via
`monkeypatch.setattr(auth_service, ...)`, `current_user` overridé via
`app.dependency_overrides[get_current_user]`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import jwt as pyjwt
import pytest
from fastapi.testclient import TestClient

from app.config import settings
from app.core.auth.guards import get_current_user
from app.core.auth.jwt import create_access_token
from app.core.database.postgres import get_db
from app.features.auth import service as auth_service
from app.features.auth.models import User
from app.features.auth.schemas import TokenResponse, UserProfile
from app.main import app


# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════


def _make_fake_user(*, is_pro: bool = False) -> MagicMock:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.email = "test@nexya.ai"
    user.username = "testuser"
    user.display_name = "Test User"
    user.bio = None
    user.is_active = True
    user.is_pro = is_pro
    user.plan = "pro" if is_pro else "free"
    user.created_at = datetime.now(UTC)
    user.updated_at = datetime.now(UTC)
    user.avatar_url = None
    return user


def _fake_token_response() -> TokenResponse:
    return TokenResponse(
        access_token="access-fake",
        refresh_token="refresh-fake",
        token_type="bearer",
        expires_in=900,
    )


def _fake_user_profile(user: MagicMock) -> UserProfile:
    return UserProfile(
        id=user.id,
        email=user.email,
        username=user.username,
        display_name=user.display_name,
        avatar_url=None,
        bio=user.bio,
        locale="fr",
        timezone="UTC",
        plan=user.plan,
        plan_expires_at=None,
        voice_id=None,
        data_collection_enabled=False,
        created_at=user.created_at,
    )


@pytest.fixture
def fake_user() -> MagicMock:
    return _make_fake_user()


@pytest.fixture
def authenticated_client(
    fake_user: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """TestClient avec `current_user` + `get_db` overridés."""
    fake_session = MagicMock()
    app.dependency_overrides[get_current_user] = lambda: fake_user
    app.dependency_overrides[get_db] = lambda: fake_session

    # Neutralise les rate limiters auth (les tests d'intégration ne testent
    # pas le rate limiting, qui a ses propres tests dédiés).
    from app.features.auth import router as auth_router_mod

    async def _noop(*args, **kwargs):  # type: ignore[no-untyped-def]
        return None

    monkeypatch.setattr(auth_router_mod, "rate_limit_register", _noop)
    monkeypatch.setattr(auth_router_mod, "rate_limit_register_daily_ip", _noop)
    monkeypatch.setattr(auth_router_mod, "rate_limit_login", _noop)

    with TestClient(app) as client:
        yield client
    app.dependency_overrides.pop(get_current_user, None)
    app.dependency_overrides.pop(get_db, None)


# ══════════════════════════════════════════════════════════════
# 1. Smoke — tous les endpoints sont montés
# ══════════════════════════════════════════════════════════════


def test_all_auth_endpoints_are_mounted_smoke() -> None:
    """Anti-régression : si quelqu'un drop le `app.include_router(auth_router)`
    par erreur, ce test casse."""
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    expected = {
        "/auth/register",
        "/auth/login",
        "/auth/refresh",
        "/auth/logout",
        "/auth/forgot-password",
        "/auth/reset-password",
        "/user/profile",
        "/user/password",
        "/user/account",
        "/user/device-token",
    }
    missing = expected - paths
    assert not missing, f"Endpoints manquants : {missing}"


# ══════════════════════════════════════════════════════════════
# 2. POST /auth/refresh
# ══════════════════════════════════════════════════════════════


def test_refresh_delegates_to_auth_service_refresh(
    authenticated_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le router doit appeler `auth_service.refresh(refresh_token, db)`
    et emballer la réponse dans NexyaResponse."""
    refresh_mock = AsyncMock(return_value=_fake_token_response())
    monkeypatch.setattr(auth_service, "refresh", refresh_mock)

    resp = authenticated_client.post(
        "/auth/refresh",
        json={"refresh_token": "old-refresh-token"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["access_token"] == "access-fake"
    assert refresh_mock.await_count == 1
    # Le token est passé en arg positionnel (cf. signature service)
    assert "old-refresh-token" in refresh_mock.await_args.args


def test_refresh_propagates_service_error_as_nexya_response(
    authenticated_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Une erreur typée du service est convertie en NexyaResponse(success=False)."""
    from app.core.errors.exceptions import AuthRefreshExpiredException

    async def _raise(_token, _db):  # type: ignore[no-untyped-def]
        raise AuthRefreshExpiredException()

    monkeypatch.setattr(auth_service, "refresh", _raise)

    resp = authenticated_client.post(
        "/auth/refresh",
        json={"refresh_token": "expired"},
    )
    assert resp.status_code == 401
    body = resp.json()
    assert body["success"] is False
    assert body["code"] == "AUTH_REFRESH_EXPIRED"


# ══════════════════════════════════════════════════════════════
# 3. POST /auth/logout — décodage access token + délégation
# ══════════════════════════════════════════════════════════════


def test_logout_decodes_access_token_and_calls_service(
    authenticated_client: TestClient,
    fake_user: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le router doit décoder l'access token (récupérer jti + exp) et
    forwarder au service."""
    logout_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_service, "logout", logout_mock)

    # Crée un vrai access token pour que decode_access_token le valide
    access_token = create_access_token(fake_user.id)

    resp = authenticated_client.post(
        "/auth/logout",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "Déconnecté" in body["data"]["message"]
    assert logout_mock.await_count == 1


def test_logout_with_invalid_token_returns_401(
    fake_user: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Sans dépendance overridée, un token bidon doit être rejeté."""
    # Ne pas overrider get_current_user — laisser FastAPI valider
    resp = TestClient(app).post(
        "/auth/logout",
        headers={"Authorization": "Bearer not-a-valid-jwt"},
    )
    assert resp.status_code in (401, 403)


# ══════════════════════════════════════════════════════════════
# 4. GET /user/profile — auth requise
# ══════════════════════════════════════════════════════════════


def test_get_profile_without_auth_returns_401(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pas d'override de get_current_user
    resp = TestClient(app).get("/user/profile")
    assert resp.status_code in (401, 403)


def test_get_profile_with_auth_returns_user_profile(
    authenticated_client: TestClient,
    fake_user: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    profile = _fake_user_profile(fake_user)
    monkeypatch.setattr(auth_service, "get_profile", lambda u: profile)

    resp = authenticated_client.get("/user/profile")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["email"] == fake_user.email
    assert body["data"]["username"] == fake_user.username


# ══════════════════════════════════════════════════════════════
# 5. PUT /user/profile — délégation update_profile
# ══════════════════════════════════════════════════════════════


def test_update_profile_delegates_to_service(
    authenticated_client: TestClient,
    fake_user: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    new_profile = _fake_user_profile(fake_user)
    new_profile_dict = new_profile.model_dump()
    new_profile_dict["display_name"] = "Updated Name"
    new_profile = UserProfile.model_validate(new_profile_dict)

    update_mock = AsyncMock(return_value=new_profile)
    monkeypatch.setattr(auth_service, "update_profile", update_mock)

    resp = authenticated_client.put(
        "/user/profile",
        json={"display_name": "Updated Name"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["data"]["display_name"] == "Updated Name"
    assert update_mock.await_count == 1


# ══════════════════════════════════════════════════════════════
# 6. PUT /user/password
# ══════════════════════════════════════════════════════════════


def test_change_password_delegates_and_returns_200(
    authenticated_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    change_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_service, "change_password", change_mock)

    resp = authenticated_client.put(
        "/user/password",
        json={
            "current_password": "OldPass123!",
            "new_password": "NewStrongPass456!",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "reconnecter" in body["data"]["message"].lower()
    assert change_mock.await_count == 1


# ══════════════════════════════════════════════════════════════
# 7. DELETE /user/account — RGPD
# ══════════════════════════════════════════════════════════════


def test_delete_account_decodes_access_token_and_anonymizes(
    authenticated_client: TestClient,
    fake_user: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    delete_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_service, "delete_account", delete_mock)

    access_token = create_access_token(fake_user.id)

    resp = authenticated_client.delete(
        "/user/account",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert delete_mock.await_count == 1


# ══════════════════════════════════════════════════════════════
# 8. POST/DELETE /user/device-token — FCM
# ══════════════════════════════════════════════════════════════


def test_register_device_token_delegates(
    authenticated_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    register_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_service, "register_device_token", register_mock)

    resp = authenticated_client.post(
        "/user/device-token",
        json={"token": "fcm-token-abc", "platform": "android"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert register_mock.await_count == 1


def test_unregister_device_token_delegates(
    authenticated_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unregister_mock = AsyncMock(return_value=None)
    monkeypatch.setattr(auth_service, "unregister_device_token", unregister_mock)

    resp = authenticated_client.request(
        "DELETE",
        "/user/device-token",
        json={"token": "fcm-token-abc", "platform": "android"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert unregister_mock.await_count == 1
