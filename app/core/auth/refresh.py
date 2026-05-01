"""
Refresh token — rotation sécurisée avec hash SHA-256.

Flux de rotation :
1. L'utilisateur envoie son refresh token (POST /auth/refresh)
2. On calcule le SHA-256 du token reçu
3. On cherche le hash en DB (refresh_tokens.token_hash)
4. Si trouvé et non révoqué et non expiré → OK
5. On révoque l'ancien (revoked_at = now)
6. On crée un nouveau refresh token + nouveau access token
7. On retourne la nouvelle paire au client

Sécurité :
- Le token n'est JAMAIS stocké en clair — uniquement son hash SHA-256
- Si un attaquant vole la DB, il ne peut pas forger de tokens
- La rotation garantit qu'un token volé ne peut être utilisé qu'une seule fois
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.features.auth.models import RefreshToken

log = structlog.get_logger()


def generate_refresh_token() -> str:
    """Génère un refresh token cryptographiquement sûr (256 bits, URL-safe)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """Hash SHA-256 d'un token — déterministe, rapide, irréversible."""
    return hashlib.sha256(token.encode()).hexdigest()


async def create_refresh_token(
    user_id: uuid.UUID,
    db: AsyncSession,
) -> str:
    """Crée un refresh token en DB et retourne le token brut (envoyé au client).

    Le token brut n'est jamais stocké — seul le hash est en DB.
    Le client doit conserver le token brut et le renvoyer pour refresh.
    """
    raw_token = generate_refresh_token()
    token_hash = hash_token(raw_token)
    expires_at = datetime.now(UTC) + timedelta(days=settings.jwt_refresh_ttl_days)

    db_token = RefreshToken(
        user_id=user_id,
        token_hash=token_hash,
        expires_at=expires_at,
    )
    db.add(db_token)
    await db.flush()

    log.info("refresh_token.created", user_id=str(user_id), expires_at=expires_at.isoformat())
    return raw_token


async def verify_and_rotate_refresh_token(
    raw_token: str,
    db: AsyncSession,
) -> tuple[RefreshToken | None, str]:
    """Vérifie un refresh token, le révoque, et en crée un nouveau.

    Returns:
        tuple[RefreshToken | None, str] : (ancien token DB ou None si invalide, nouveau token brut)
        Le service appelant doit vérifier `old_token is None` pour lever l'exception adéquate.
    """
    token_hash = hash_token(raw_token)
    now = datetime.now(UTC)

    # Chercher le token en DB (non révoqué, non expiré)
    result = await db.execute(
        select(RefreshToken).where(
            RefreshToken.token_hash == token_hash,
            RefreshToken.revoked_at.is_(None),
            RefreshToken.expires_at > now,
        )
    )
    old_token = result.scalar_one_or_none()

    if old_token is None:
        return None, ""

    # Révoquer l'ancien token
    old_token.revoked_at = now
    await db.flush()

    # Créer le nouveau
    new_raw_token = await create_refresh_token(old_token.user_id, db)

    log.info(
        "refresh_token.rotated",
        user_id=str(old_token.user_id),
        old_token_id=str(old_token.id),
    )
    return old_token, new_raw_token


async def revoke_all_user_tokens(user_id: uuid.UUID, db: AsyncSession) -> int:
    """Révoque TOUS les refresh tokens d'un utilisateur (logout total).

    Utilisé lors du logout ou quand on suspecte une compromission.
    Retourne le nombre de tokens révoqués.
    """
    now = datetime.now(UTC)
    result = await db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.user_id == user_id,
            RefreshToken.revoked_at.is_(None),
        )
        .values(revoked_at=now)
    )
    count = result.rowcount
    log.info("refresh_token.revoked_all", user_id=str(user_id), count=count)
    return count
