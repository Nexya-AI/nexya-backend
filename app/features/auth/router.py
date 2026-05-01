"""
Router Auth — 10 endpoints pour authentification et gestion de compte.

Discipline NEXYA :
- Les routers ne contiennent AUCUNE logique métier — ils délèguent au service
- Toutes les réponses sont encapsulées dans NexyaResponse[T]
- Rate limiting IP sur les endpoints publics (login, register)
- Guards (get_current_user) sur les endpoints protégés

Contexte forensic (A3 hardening) :
- À chaque endpoint authentifiant ou sensible, on extrait `client_ip`,
  `user_agent`, `X-Device-Id` depuis la requête et on les forwarde au
  service. Le service les trace dans `auth_events` (jamais ici — le
  router ne parle pas à la DB).
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.guards import get_current_user
from app.core.auth.jwt import decode_access_token
from app.core.database.postgres import get_db
from app.core.security.rate_limiter import (
    rate_limit_forgot_password_ip,
    rate_limit_login,
    rate_limit_refresh,
    rate_limit_register,
    rate_limit_register_daily_ip,
    rate_limit_reset_password_ip,
)
from app.features.auth import service as auth_service
from app.features.auth.models import User
from app.features.auth.schemas import (
    ChangePasswordRequest,
    DeviceTokenRequest,
    ForgotPasswordRequest,
    ForgotPasswordResponse,
    LoginRequest,
    RefreshRequest,
    RegisterRequest,
    ResetPasswordRequest,
    ResetPasswordResponse,
    TokenResponse,
    UpdateProfileRequest,
    UserProfile,
)
from app.shared.schemas import NexyaResponse

router = APIRouter(tags=["auth"])
bearer_scheme = HTTPBearer(auto_error=False)


# ══════════════════════════════════════════════════════════════
# Helpers — extraction contexte forensic
# ══════════════════════════════════════════════════════════════


def _client_ip(request: Request) -> str | None:
    """Extrait l'IP client en prenant en compte `X-Forwarded-For` (proxy).

    `None` si on ne peut rien extraire — le service stockera `NULL` dans
    `auth_events.ip`. On ne met pas de sentinelle "unknown" ici pour que
    les requêtes analytiques puissent distinguer "pas d'IP" de "IP 'unknown'".
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip() or None
    return request.client.host if request.client else None


def _user_agent(request: Request) -> str | None:
    """Extrait le header User-Agent (tronqué à 256 chars côté service)."""
    ua = request.headers.get("user-agent")
    return ua.strip() if ua else None


def _device_id(request: Request) -> str | None:
    """Extrait le header `X-Device-Id` — UUID stable généré par le Flutter.

    Normalisé plus tard par `normalize_device_id()` dans le service —
    ici on renvoie la valeur brute (ou `None`).
    """
    raw = request.headers.get("x-device-id")
    return raw.strip() if raw else None


# ══════════════════════════════════════════════════════════════
# AUTH — Register / Login / Refresh / Logout
# ══════════════════════════════════════════════════════════════


@router.post("/auth/register", response_model=NexyaResponse[TokenResponse])
async def register(
    body: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TokenResponse]:
    """Inscription d'un nouvel utilisateur.

    Défense en profondeur :
    - Couche 1 : rate limit 5/min/IP (`rate_limit_register`) — bloque les
      raffales courtes.
    - Couche 2 : rate limit 5/jour/IP (`rate_limit_register_daily_ip`) —
      bloque les attaques « slow & low » qui espacent leurs requêtes.
    - Couche 3 : device quota (`device_quotas`) — bloque l'attaque
      distribuée (IPs tournantes, même device_id).
    - Couche 4 : captcha hCaptcha — coupe les bots avant l'INSERT user.
    """
    await rate_limit_register(request)
    await rate_limit_register_daily_ip(request)
    tokens = await auth_service.register(
        body,
        db,
        client_ip=_client_ip(request),
        user_agent=_user_agent(request),
        device_id_raw=_device_id(request),
    )
    return NexyaResponse(success=True, data=tokens)


@router.post("/auth/login", response_model=NexyaResponse[TokenResponse])
async def login(
    body: LoginRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TokenResponse]:
    """Connexion d'un utilisateur existant — rate limit 10/min par IP."""
    await rate_limit_login(request)
    tokens = await auth_service.login(
        body,
        db,
        client_ip=_client_ip(request),
        user_agent=_user_agent(request),
        device_id_raw=_device_id(request),
    )
    return NexyaResponse(success=True, data=tokens)


@router.post(
    "/auth/forgot-password",
    response_model=NexyaResponse[ForgotPasswordResponse],
)
async def forgot_password(
    body: ForgotPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ForgotPasswordResponse]:
    """Demande un email de réinitialisation — rate limit 10/h par IP.

    **Anti-enumeration** : retourne toujours 200 avec un message générique,
    qu'un compte existe ou non pour cet email. Un attaquant ne peut donc
    pas utiliser cet endpoint pour deviner quelles adresses sont inscrites.
    """
    await rate_limit_forgot_password_ip(request)
    await auth_service.forgot_password(
        body,
        db,
        client_ip=_client_ip(request),
        user_agent=_user_agent(request),
    )
    return NexyaResponse(success=True, data=ForgotPasswordResponse())


@router.post(
    "/auth/reset-password",
    response_model=NexyaResponse[ResetPasswordResponse],
)
async def reset_password(
    body: ResetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ResetPasswordResponse]:
    """Reset le mot de passe à l'aide du token reçu par email.

    Le token est un JWT RS256 signé (TTL 15 min, purpose=password_reset,
    fingerprint du hash actuel). Révoque tous les refresh tokens en succès.
    Rate limit 5/h par IP pour bloquer les tentatives de bruteforce de token.
    """
    await rate_limit_reset_password_ip(request)
    await auth_service.reset_password(
        body,
        db,
        client_ip=_client_ip(request),
        user_agent=_user_agent(request),
    )
    return NexyaResponse(success=True, data=ResetPasswordResponse())


@router.post("/auth/refresh", response_model=NexyaResponse[TokenResponse])
async def refresh(
    body: RefreshRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TokenResponse]:
    """Renouvellement du couple access + refresh via rotation.

    Rate limit IP : **20/min**. Un attaquant qui obtient un refresh
    token leaké ne peut pas l'exploiter pour spammer la rotation
    au-delà de 20× par minute par IP source — borne le brute-force
    JWT immédiatement à 1200 access tokens / heure / IP.
    """
    await rate_limit_refresh(request)
    tokens = await auth_service.refresh(body.refresh_token, db)
    return NexyaResponse(success=True, data=tokens)


@router.post("/auth/logout", response_model=NexyaResponse[dict])
async def logout(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[dict]:
    """Déconnexion — blacklist l'access token + révoque tous les refresh tokens."""
    # Décoder le token pour récupérer jti + exp (pour la blacklist)
    payload = decode_access_token(credentials.credentials)
    access_jti = payload["jti"]
    access_exp = datetime.fromtimestamp(payload["exp"], tz=UTC)

    await auth_service.logout(
        current_user,
        access_jti,
        access_exp,
        db,
        client_ip=_client_ip(request),
        user_agent=_user_agent(request),
    )
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
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[dict]:
    """Changement de mot de passe — révoque tous les refresh tokens."""
    await auth_service.change_password(
        current_user,
        body,
        db,
        client_ip=_client_ip(request),
        user_agent=_user_agent(request),
    )
    return NexyaResponse(
        success=True,
        data={"message": "Mot de passe modifié. Veuillez vous reconnecter."},
    )


@router.delete("/user/account", response_model=NexyaResponse[dict])
async def delete_account(
    request: Request,
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[dict]:
    """Suppression RGPD — anonymise le compte (pas de suppression physique)."""
    payload = decode_access_token(credentials.credentials)
    access_jti = payload["jti"]
    access_exp = datetime.fromtimestamp(payload["exp"], tz=UTC)

    await auth_service.delete_account(
        current_user,
        access_jti,
        access_exp,
        db,
        client_ip=_client_ip(request),
        user_agent=_user_agent(request),
    )
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
