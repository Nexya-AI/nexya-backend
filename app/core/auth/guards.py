"""
Guards d'authentification — dépendances FastAPI injectées via Depends().

get_current_user : extrait et vérifie le JWT du header Authorization.
require_pro      : vérifie que l'utilisateur a un plan Pro actif.

Usage dans un endpoint :
    @router.get("/protected")
    async def protected_route(
        current_user: User = Depends(get_current_user),
    ):
        ...

    @router.get("/pro-only")
    async def pro_route(
        current_user: User = Depends(require_pro),
    ):
        ...
"""

from __future__ import annotations

import jwt
import structlog
from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.auth.jwt import decode_access_token, is_token_blacklisted
from app.core.database.postgres import get_db
from app.core.errors.exceptions import (
    AuthTokenExpiredException,
    AuthTokenInvalidException,
    PermissionDeniedException,
    PlanRequiredException,
)
from app.features.auth.models import User

log = structlog.get_logger()

# ── Schéma Bearer HTTP ─────────────────────────────────────────
# auto_error=False : on gère nous-mêmes l'erreur pour un message propre
bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Extrait l'utilisateur courant depuis le JWT Bearer.

    Pipeline de vérification :
    1. Header Authorization présent ?
    2. JWT décodable et signature valide ?
    3. Token non blacklisté (Redis) ?
    4. Utilisateur existe en DB et est actif ?

    Chaque étape lève une exception typée si échec.
    """
    # 1. Header présent ?
    if credentials is None:
        raise AuthTokenInvalidException()

    token = credentials.credentials

    # 2. Décoder le JWT
    try:
        payload = decode_access_token(token)
    except jwt.ExpiredSignatureError:
        raise AuthTokenExpiredException()
    except jwt.InvalidTokenError:
        raise AuthTokenInvalidException()

    # 3. Token blacklisté ?
    jti = payload.get("jti")
    if jti and await is_token_blacklisted(jti):
        log.warning("auth.token_blacklisted", jti=jti)
        raise AuthTokenInvalidException()

    # 4. Chercher l'utilisateur en DB
    user_id = payload.get("sub")
    if not user_id:
        raise AuthTokenInvalidException()

    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        log.warning("auth.user_not_found", user_id=user_id)
        raise AuthTokenInvalidException()

    return user


async def require_pro(
    current_user: User = Depends(get_current_user),
) -> User:
    """Vérifie que l'utilisateur a un plan Pro actif.

    Utilisé pour les features réservées (quota élevé, voix premium, etc.).
    """
    if not current_user.is_pro:
        raise PlanRequiredException()
    return current_user


async def require_admin(
    current_user: User = Depends(get_current_user),
) -> User:
    """Vérifie que l'utilisateur est dans la liste `RGPD_ADMIN_EMAILS`.

    V1 : ACL simple par liste d'emails (DPO Ivan en V1, prévoir DPO
    externe pour 950k users en V2). Pas de table `admin_users` ni
    de RBAC complexe — V2 si besoin réel multi-admin.

    Utilisé par l'endpoint admin `/rgpd/admin/ai-act-registry`.
    Production safety guard refuse le boot en prod si la liste est
    vide (un endpoint admin sans ACL = fuite catastrophique du
    registre AI Act complet).
    """
    from app.config import settings  # local import — évite le cycle

    if not current_user.email or current_user.email.lower() not in {
        e.lower() for e in settings.rgpd_admin_emails
    }:
        raise PermissionDeniedException()
    return current_user
