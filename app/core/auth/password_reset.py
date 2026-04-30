"""
JWT RS256 — Tokens de reset mot de passe.

Contraintes de sécurité :
- TTL court : 15 minutes (window d'attaque minimale)
- Claim `purpose=password_reset` distinct — un access token ne peut jamais
  servir à reset, et réciproquement
- Fingerprint `pwh_fp` = SHA-256[:16] du `password_hash` actuel — si
  l'utilisateur change son mot de passe, tous les tokens précédents
  deviennent implicitement invalides (pas besoin de blacklist DB).

Ce fingerprint protège aussi contre le replay : un token déjà utilisé
invalide son propre fingerprint (car le hash change après reset).
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import jwt
import structlog

from app.config import settings
from app.core.errors.exceptions import (
    ResetTokenExpiredException,
    ResetTokenInvalidException,
)

log = structlog.get_logger()

ALGORITHM = "RS256"
TOKEN_PURPOSE_RESET = "password_reset"
RESET_TOKEN_TTL_MINUTES = 15


def _fingerprint(password_hash: str) -> str:
    """SHA-256[:16] — assez court pour le payload JWT, assez long pour éviter collision."""
    return hashlib.sha256(password_hash.encode("utf-8")).hexdigest()[:16]


def create_password_reset_token(user_id: uuid.UUID, password_hash: str) -> str:
    """Crée un JWT RS256 de reset mot de passe.

    Payload :
    - sub : user_id
    - purpose : 'password_reset'
    - pwh_fp : SHA-256[:16] du hash actuel (invalide le token si le
      mot de passe change)
    - iat, exp, jti : standards
    """
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "purpose": TOKEN_PURPOSE_RESET,
        "pwh_fp": _fingerprint(password_hash),
        "iat": now,
        "exp": now + timedelta(minutes=RESET_TOKEN_TTL_MINUTES),
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, settings.jwt_private_key, algorithm=ALGORITHM)
    log.info("jwt.reset.created", user_id=str(user_id))
    return token


def decode_password_reset_token(token: str) -> dict:
    """Décode et valide un token de reset.

    Raises:
        ResetTokenExpiredException : exp < now
        ResetTokenInvalidException : signature ko, purpose ≠ password_reset,
            ou payload malformé
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_public_key,
            algorithms=[ALGORITHM],
        )
    except jwt.ExpiredSignatureError as exc:
        raise ResetTokenExpiredException() from exc
    except jwt.InvalidTokenError as exc:
        raise ResetTokenInvalidException() from exc

    if payload.get("purpose") != TOKEN_PURPOSE_RESET:
        raise ResetTokenInvalidException()

    if not payload.get("sub") or not payload.get("pwh_fp"):
        raise ResetTokenInvalidException()

    return payload


def verify_password_hash_fingerprint(payload: dict, current_password_hash: str) -> None:
    """Vérifie que le fingerprint du payload correspond au hash actuel.

    Si le mot de passe a déjà changé depuis l'émission du token, le
    fingerprint ne matchera plus → token rejeté. Protège du replay.
    """
    if payload.get("pwh_fp") != _fingerprint(current_password_hash):
        raise ResetTokenInvalidException()
