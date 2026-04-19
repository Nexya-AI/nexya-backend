"""
Router Auth — 10 endpoints pour authentification et gestion de compte.

Discipline NEXYA :
- Les routers ne contiennent AUCUNE logique métier — ils délèguent au service
- Toutes les réponses sont encapsulées dans NexyaResponse[T]
- Rate limiting IP sur les endpoints publics (login, register)
- Guards (get_current_user) sur les endpoints protégés
"""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.guards import get_current_user
from app.core.auth.jwt import decode_access_token
from app.core.database.postgres import get_db
from app.core.security.rate_limiter import rate_limit_login, rate_limit_register
from app.features.auth import service as auth_service
from app.features.auth.models import User
from app.features.auth.schemas import (
    ChangePasswordRequest,
    DeviceTokenRequest,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    TokenResponse,
    UpdateProfileRequest,
    UserProfile,
)
from app.shared.schemas import NexyaResponse

router = APIRouter(tags=["auth"])
bearer_scheme = HTTPBearer(auto_error=False)


# ══════════════════════════════════════════════════════════════
# AUTH — Register / Login / Refresh / Logout
# ══════════════════════════════════════════════════════════════

@router.post("/auth/register", response_model=NexyaResponse[TokenResponse])
async def register(
    body: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TokenResponse]:
    """Inscription d'un nouvel utilisateur — rate limit 5/min par IP."""
    await rate_limit_register(request)
    tokens = await auth_service.register(body, db)
    return NexyaResponse(success=True, data=tokens)


@router.post("/auth/login", response_model=NexyaResponse[TokenResponse])
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TokenResponse]:
    """Connexion d'un utilisateur existant — rate limit 10/min par IP."""
    await rate_limit_login(request)
    tokens = await auth_service.login(body, db)
    return NexyaResponse(success=True, data=tokens)


@router.post("/auth/refresh", response_model=NexyaResponse[TokenResponse])
async def refresh(
    body: RefreshRequest,
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TokenResponse]:
    """Renouvellement du couple access + refresh via rotation."""
    tokens = await auth_service.refresh(body.refresh_token, db)
    return NexyaResponse(success=True, data=tokens)


@router.post("/auth/logout", response_model=NexyaResponse[dict])
async def logout(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[dict]:
    """Déconnexion — blacklist l'access token + révoque tous les refresh tokens."""
    # Décoder le token pour récupérer jti + exp (pour la blacklist)
    payload = decode_access_token(credentials.credentials)
    access_jti = payload["jti"]
    access_exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)

    await auth_service.logout(current_user, access_jti, access_exp, db)
    return NexyaResponse(success=True, data={"message": "Déconnecté avec succès."})


# ══════════════════════════════════════════════════════════════
# USER — Profile
# ══════════════════════════════════════════════════════════════

@router.get("/user/profile", response_model=NexyaResponse[UserProfile])
async def get_profile(
    current_user: User = Depends(get_current_user),
) -> NexyaResponse[UserProfile]:
    """Profil de l'utilisateur courant."""
    profile = auth_service.get_profile(current_user)
    return NexyaResponse(success=True, data=profile)


@router.put("/user/profile", response_model=NexyaResponse[UserProfile])
async def update_profile(
    body: UpdateProfileRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[UserProfile]:
    """Mise à jour partielle du profil (seuls les champs fournis sont modifiés)."""
    profile = await auth_service.update_profile(current_user, body, db)
    return NexyaResponse(success=True, data=profile)


@router.put("/user/password", response_model=NexyaResponse[dict])
async def change_password(
    body: ChangePasswordRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[dict]:
    """Changement de mot de passe — révoque tous les refresh tokens."""
    await auth_service.change_password(current_user, body, db)
    return NexyaResponse(
        success=True,
        data={"message": "Mot de passe modifié. Veuillez vous reconnecter."},
    )


@router.delete("/user/account", response_model=NexyaResponse[dict])
async def delete_account(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[dict]:
    """Suppression RGPD — anonymise le compte (pas de suppression physique)."""
    payload = decode_access_token(credentials.credentials)
    access_jti = payload["jti"]
    access_exp = datetime.fromtimestamp(payload["exp"], tz=timezone.utc)

    await auth_service.delete_account(current_user, access_jti, access_exp, db)
    return NexyaResponse(success=True, data={"message": "Compte supprimé."})


# ══════════════════════════════════════════════════════════════
# DEVICE TOKENS — FCM (notifications push)
# ══════════════════════════════════════════════════════════════

@router.post("/user/device-token", response_model=NexyaResponse[dict])
async def register_device_token(
    body: DeviceTokenRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[dict]:
    """Enregistrement d'un token FCM pour les notifications push."""
    await auth_service.register_device_token(current_user, body, db)
    return NexyaResponse(success=True, data={"message": "Token enregistré."})


@router.delete("/user/device-token", response_model=NexyaResponse[dict])
async def unregister_device_token(
    body: DeviceTokenRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[dict]:
    """Désactivation d'un token FCM (déconnexion d'un appareil)."""
    await auth_service.unregister_device_token(current_user, body.token, db)
    return NexyaResponse(success=True, data={"message": "Token désactivé."})
