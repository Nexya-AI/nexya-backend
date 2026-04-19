"""
AuthService — logique métier d'authentification.

Toute la logique est ici. Le router ne fait qu'appeler le service.
Le service orchestre : DB (SQLAlchemy) + Redis (blacklist) + bcrypt (hash).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import bcrypt
import structlog
from sqlalchemy import select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth.jwt import blacklist_token, create_access_token
from app.core.auth.refresh import (
    create_refresh_token,
    revoke_all_user_tokens,
    verify_and_rotate_refresh_token,
)
from app.core.errors.exceptions import (
    AuthCredentialsInvalidException,
    AuthEmailAlreadyExistsException,
    AuthRefreshExpiredException,
    AuthUsernameAlreadyExistsException,
    ResourceNotFoundException,
)
from app.features.auth.models import DeviceToken, User
from app.features.auth.schemas import (
    ChangePasswordRequest,
    DeviceTokenRequest,
    LoginRequest,
    RegisterRequest,
    TokenResponse,
    UpdateProfileRequest,
    UserProfile,
)

log = structlog.get_logger()


def _hash_password(password: str) -> str:
    """Hash bcrypt — lent par design (résistant au brute-force).

    Note importante : bcrypt tronque silencieusement à 72 bytes.
    On tronque EXPLICITEMENT ici pour éviter qu'un utilisateur croie
    qu'un mot de passe de 100 caractères est plus sûr qu'un de 72.
    La validation Pydantic limite déjà à 128 caractères côté entrée.
    """
    password_bytes = password.encode("utf-8")[:72]
    hashed = bcrypt.hashpw(password_bytes, bcrypt.gensalt())
    return hashed.decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    """Vérifie un mot de passe contre son hash bcrypt."""
    password_bytes = password.encode("utf-8")[:72]
    return bcrypt.checkpw(password_bytes, password_hash.encode("utf-8"))


def _build_token_response(access_token: str, refresh_token: str) -> TokenResponse:
    """Construit la réponse token standardisée."""
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.jwt_access_ttl_minutes * 60,
    )


# ══════════════════════════════════════════════════════════════
# REGISTER
# ══════════════════════════════════════════════════════════════

async def register(body: RegisterRequest, db: AsyncSession) -> TokenResponse:
    """Inscrit un nouvel utilisateur.

    1. Vérifie que l'email (et le username si fourni) n'existent pas
    2. Hash le mot de passe avec bcrypt
    3. Crée l'utilisateur en DB
    4. Génère le couple access + refresh token
    5. Retourne les tokens au client
    """
    # Vérifier unicité email + username
    conditions = [User.email == body.email.lower()]
    if body.username:
        conditions.append(User.username == body.username.lower())

    result = await db.execute(
        select(User).where(or_(*conditions))
    )
    existing = result.scalar_one_or_none()

    if existing is not None:
        # Distinguer email vs username pour un message utilisateur correct
        if existing.email == body.email.lower():
            raise AuthEmailAlreadyExistsException()
        raise AuthUsernameAlreadyExistsException()

    # Créer l'utilisateur
    user = User(
        email=body.email.lower(),
        username=body.username,
        password_hash=_hash_password(body.password),
        display_name=body.display_name or body.username or body.email.split("@")[0],
    )
    db.add(user)
    await db.flush()  # flush pour obtenir user.id avant le commit

    # Générer les tokens
    access_token = create_access_token(user.id, user.plan)
    refresh_token = await create_refresh_token(user.id, db)

    log.info("auth.register.success", user_id=str(user.id), email=user.email)
    return _build_token_response(access_token, refresh_token)


# ══════════════════════════════════════════════════════════════
# LOGIN
# ══════════════════════════════════════════════════════════════

async def login(body: LoginRequest, db: AsyncSession) -> TokenResponse:
    """Connecte un utilisateur existant.

    1. Cherche l'utilisateur par email
    2. Vérifie le mot de passe avec bcrypt
    3. Génère le couple access + refresh token
    """
    result = await db.execute(
        select(User).where(
            User.email == body.email.lower(),
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )
    user = result.scalar_one_or_none()

    if user is None or not _verify_password(body.password, user.password_hash):
        # Message volontairement vague — ne pas révéler si l'email existe
        raise AuthCredentialsInvalidException()

    access_token = create_access_token(user.id, user.plan)
    refresh_token = await create_refresh_token(user.id, db)

    log.info("auth.login.success", user_id=str(user.id))
    return _build_token_response(access_token, refresh_token)


# ══════════════════════════════════════════════════════════════
# REFRESH
# ══════════════════════════════════════════════════════════════

async def refresh(raw_refresh_token: str, db: AsyncSession) -> TokenResponse:
    """Renouvelle le couple access + refresh token via rotation.

    L'ancien refresh token est révoqué, un nouveau est créé.
    """
    old_token, new_raw_token = await verify_and_rotate_refresh_token(raw_refresh_token, db)

    if old_token is None:
        raise AuthRefreshExpiredException()

    # Charger l'utilisateur pour le nouveau access token
    result = await db.execute(
        select(User).where(
            User.id == old_token.user_id,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise AuthRefreshExpiredException()

    access_token = create_access_token(user.id, user.plan)

    log.info("auth.refresh.success", user_id=str(user.id))
    return _build_token_response(access_token, new_raw_token)


# ══════════════════════════════════════════════════════════════
# LOGOUT
# ══════════════════════════════════════════════════════════════

async def logout(
    user: User,
    access_jti: str,
    access_exp: datetime,
    db: AsyncSession,
) -> None:
    """Déconnecte l'utilisateur.

    1. Blacklist l'access token en cours (Redis)
    2. Révoque tous les refresh tokens de l'utilisateur (DB)
    """
    await blacklist_token(access_jti, access_exp)
    await revoke_all_user_tokens(user.id, db)
    log.info("auth.logout.success", user_id=str(user.id))


# ══════════════════════════════════════════════════════════════
# PROFILE
# ══════════════════════════════════════════════════════════════

def get_profile(user: User) -> UserProfile:
    """Retourne le profil de l'utilisateur courant."""
    return UserProfile.model_validate(user)


async def update_profile(
    user: User,
    body: UpdateProfileRequest,
    db: AsyncSession,
) -> UserProfile:
    """Met à jour le profil de l'utilisateur.

    Seuls les champs non-None sont mis à jour (partial update).
    """
    # Vérifier unicité du username si modifié
    if body.username and body.username != user.username:
        result = await db.execute(
            select(User).where(
                User.username == body.username,
                User.id != user.id,
            )
        )
        if result.scalar_one_or_none() is not None:
            raise AuthUsernameAlreadyExistsException()

    update_data = body.model_dump(exclude_none=True)
    for field, value in update_data.items():
        setattr(user, field, value)

    user.updated_at = datetime.now(timezone.utc)
    await db.flush()

    log.info("auth.profile.updated", user_id=str(user.id), fields=list(update_data.keys()))
    return UserProfile.model_validate(user)


# ══════════════════════════════════════════════════════════════
# CHANGE PASSWORD
# ══════════════════════════════════════════════════════════════

async def change_password(
    user: User,
    body: ChangePasswordRequest,
    db: AsyncSession,
) -> None:
    """Change le mot de passe de l'utilisateur.

    1. Vérifie l'ancien mot de passe
    2. Hash le nouveau avec bcrypt
    3. Révoque tous les refresh tokens (force la reconnexion partout)
    """
    if not _verify_password(body.current_password, user.password_hash):
        raise AuthCredentialsInvalidException()

    user.password_hash = _hash_password(body.new_password)
    user.updated_at = datetime.now(timezone.utc)
    await db.flush()

    # Révoquer tous les tokens — l'utilisateur devra se reconnecter
    await revoke_all_user_tokens(user.id, db)

    log.info("auth.password.changed", user_id=str(user.id))


# ══════════════════════════════════════════════════════════════
# DELETE ACCOUNT (RGPD — anonymisation)
# ══════════════════════════════════════════════════════════════

async def delete_account(
    user: User,
    access_jti: str,
    access_exp: datetime,
    db: AsyncSession,
) -> None:
    """Supprime (anonymise) le compte utilisateur — RGPD.

    On ne supprime PAS les données — on anonymise :
    - email → deleted_<uuid>@nexya.ai
    - username → None
    - display_name → "Utilisateur supprimé"
    - password_hash → chaîne invalide (impossible de se reconnecter)
    - is_active → False
    - deleted_at → now

    Les conversations et données associées restent en DB
    mais ne sont plus liées à une identité réelle.
    """
    now = datetime.now(timezone.utc)

    user.email = f"deleted_{uuid.uuid4().hex[:12]}@nexya.ai"
    user.username = None
    user.display_name = "Utilisateur supprime"
    user.avatar_url = None
    user.bio = None
    user.password_hash = "DELETED_ACCOUNT"
    user.is_active = False
    user.deleted_at = now
    user.updated_at = now

    await db.flush()

    # Blacklist l'access token + révoquer tous les refresh tokens
    await blacklist_token(access_jti, access_exp)
    await revoke_all_user_tokens(user.id, db)

    log.info("auth.account.deleted", user_id=str(user.id))


# ══════════════════════════════════════════════════════════════
# DEVICE TOKENS (FCM — notifications push)
# ══════════════════════════════════════════════════════════════

async def register_device_token(
    user: User,
    body: DeviceTokenRequest,
    db: AsyncSession,
) -> None:
    """Enregistre un token FCM pour les notifications push.

    Si le token existe déjà (même appareil), on met à jour le user_id
    (cas où l'utilisateur change de compte sur le même appareil).
    """
    result = await db.execute(
        select(DeviceToken).where(DeviceToken.token == body.token)
    )
    existing = result.scalar_one_or_none()

    if existing:
        existing.user_id = user.id
        existing.platform = body.platform
        existing.is_active = True
        existing.updated_at = datetime.now(timezone.utc)
    else:
        device = DeviceToken(
            user_id=user.id,
            token=body.token,
            platform=body.platform,
        )
        db.add(device)

    await db.flush()
    log.info("auth.device_token.registered", user_id=str(user.id), platform=body.platform)


async def unregister_device_token(
    user: User,
    token: str,
    db: AsyncSession,
) -> None:
    """Désactive un token FCM (déconnexion d'un appareil)."""
    result = await db.execute(
        select(DeviceToken).where(
            DeviceToken.token == token,
            DeviceToken.user_id == user.id,
        )
    )
    device = result.scalar_one_or_none()

    if device is None:
        raise ResourceNotFoundException("Token d'appareil")

    device.is_active = False
    device.updated_at = datetime.now(timezone.utc)
    await db.flush()

    log.info("auth.device_token.unregistered", user_id=str(user.id))
