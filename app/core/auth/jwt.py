"""
JWT RS256 — Encode, decode et blacklist des access tokens.

RS256 (asymétrique) : la clé privée signe, la clé publique vérifie.
Avantage : on peut distribuer la clé publique à des services tiers
(microservices, CDN) pour qu'ils vérifient les tokens sans pouvoir en créer.

Blacklist Redis : quand un utilisateur se déconnecte, son access token
est ajouté à Redis avec un TTL = temps restant avant expiration.
Ainsi la révocation est instantanée, sans attendre l'expiration naturelle.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import jwt
import structlog

from app.config import settings
from app.core.database.redis import get_redis

log = structlog.get_logger()

# ── Constantes ─────────────────────────────────────────────────
ALGORITHM = "RS256"
TOKEN_TYPE_ACCESS = "access"
BLACKLIST_PREFIX = "jwt:blacklist:"


def create_access_token(user_id: uuid.UUID, plan: str = "free") -> str:
    """Crée un access token JWT signé RS256.

    Payload :
    - sub : user_id (identifiant unique)
    - plan : 'free' ou 'pro' (permet au guard de vérifier sans DB)
    - type : 'access' (distingue access/refresh)
    - iat : issued at (timestamp)
    - exp : expiration (15 minutes par défaut)
    - jti : JWT ID unique (pour la blacklist)
    """
    now = datetime.now(timezone.utc)
    jti = str(uuid.uuid4())

    payload = {
        "sub": str(user_id),
        "plan": plan,
        "type": TOKEN_TYPE_ACCESS,
        "iat": now,
        "exp": now + timedelta(minutes=settings.jwt_access_ttl_minutes),
        "jti": jti,
    }

    token = jwt.encode(payload, settings.jwt_private_key, algorithm=ALGORITHM)
    log.debug("jwt.access.created", user_id=str(user_id), jti=jti)
    return token


def decode_access_token(token: str) -> dict:
    """Décode et vérifie un access token JWT.

    Vérifie :
    1. Signature RS256 valide (clé publique)
    2. Token non expiré (exp > now)
    3. Type = 'access' (pas un refresh token)

    Raises :
    - jwt.ExpiredSignatureError → AuthTokenExpiredException dans le guard
    - jwt.InvalidTokenError → AuthTokenInvalidException dans le guard
    """
    payload = jwt.decode(
        token,
        settings.jwt_public_key,
        algorithms=[ALGORITHM],
    )

    if payload.get("type") != TOKEN_TYPE_ACCESS:
        raise jwt.InvalidTokenError("Token type is not 'access'")

    return payload


async def blacklist_token(jti: str, exp: datetime) -> None:
    """Ajoute un access token à la blacklist Redis.

    Le token est stocké avec un TTL = temps restant avant expiration.
    Après expiration, Redis le supprime automatiquement — pas de nettoyage nécessaire.
    """
    now = datetime.now(timezone.utc)
    ttl = int((exp - now).total_seconds())

    if ttl <= 0:
        return  # déjà expiré, inutile de blacklister

    redis = get_redis()
    key = f"{BLACKLIST_PREFIX}{jti}"
    await redis.setex(key, ttl, "1")
    log.info("jwt.blacklisted", jti=jti, ttl=ttl)


async def is_token_blacklisted(jti: str) -> bool:
    """Vérifie si un access token est dans la blacklist Redis."""
    redis = get_redis()
    key = f"{BLACKLIST_PREFIX}{jti}"
    return await redis.exists(key) > 0
