"""
JWT RS256 — Tokens d'unsubscribe email one-click (F3 / RGPD / CAN-SPAM).

Contraintes de sécurité et d'UX :

- **TTL long (365 jours)** : un user peut cliquer le lien unsubscribe
  dans un email reçu il y a 6 mois et s'attendre à ce que ça marche
  encore. CAN-SPAM US impose 10 jours minimum, RGPD n'impose pas de
  TTL mais « doit rester possible à tout moment ». Un TTL 365j couvre
  les deux sans exposer au replay un token volé des années plus tard.
- **Pas de fingerprint** (contrairement au reset password JWT) — l'action
  est idempotente : re-appeler `/unsubscribe/{token}` pose `channel='none'`
  deux fois, même résultat. Un replay n'a donc aucun impact négatif.
- **Purpose dédié `email_unsubscribe`** — un access token, un reset
  token, ou n'importe quel autre JWT NEXYA ne peut JAMAIS servir à
  désinscrire. Isolation stricte des usages.
- **Claim `cat` (catégorie)** — le token encode quelle catégorie
  désinscrire. Un lien unsubscribe dans un email `payment_confirmed`
  désinscrit uniquement la catégorie `payments`, pas `tasks`.

Exception : la catégorie `security` ne devrait JAMAIS recevoir de token
unsubscribe (obligation légale de notifier les événements de sécurité).
Le service appelant s'en occupe (ne génère pas de token pour `security`).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import structlog

from app.config import settings
from app.core.errors.exceptions import (
    UnsubscribeTokenExpiredException,
    UnsubscribeTokenInvalidException,
)

log = structlog.get_logger()

ALGORITHM = "RS256"
TOKEN_PURPOSE_UNSUBSCRIBE = "email_unsubscribe"


def create_unsubscribe_token(
    user_id: uuid.UUID,
    category: str,
) -> str:
    """Crée un JWT RS256 d'unsubscribe pour `(user, category)`.

    Le TTL est lu depuis `settings.notification_unsubscribe_token_ttl_days`
    (défaut 365j). Payload :

    - `sub` : user_id (string UUID).
    - `purpose` : 'email_unsubscribe'.
    - `cat` : nom de la catégorie (ex: 'tasks', 'payments', ...).
    - `iat`, `exp`, `jti` : standards.
    """
    now = datetime.now(UTC)
    ttl = timedelta(days=settings.notification_unsubscribe_token_ttl_days)
    payload: dict[str, object] = {
        "sub": str(user_id),
        "purpose": TOKEN_PURPOSE_UNSUBSCRIBE,
        "cat": category,
        "iat": now,
        "exp": now + ttl,
        "jti": str(uuid.uuid4()),
    }
    token = jwt.encode(payload, settings.jwt_private_key, algorithm=ALGORITHM)
    log.info(
        "jwt.unsubscribe.created",
        user_id=str(user_id),
        category=category,
    )
    return token


def decode_unsubscribe_token(token: str) -> dict:
    """Décode et valide un token d'unsubscribe.

    Returns:
        Payload `{sub, purpose, cat, iat, exp, jti}`.

    Raises:
        UnsubscribeTokenExpiredException : exp < now.
        UnsubscribeTokenInvalidException : signature KO, purpose ≠
            `email_unsubscribe`, ou payload malformé (sub/cat absents,
            cat hors enum, ...).
    """
    try:
        payload = jwt.decode(
            token,
            settings.jwt_public_key,
            algorithms=[ALGORITHM],
        )
    except jwt.ExpiredSignatureError as exc:
        raise UnsubscribeTokenExpiredException() from exc
    except jwt.InvalidTokenError as exc:
        raise UnsubscribeTokenInvalidException() from exc

    if payload.get("purpose") != TOKEN_PURPOSE_UNSUBSCRIBE:
        raise UnsubscribeTokenInvalidException()

    if not payload.get("sub"):
        raise UnsubscribeTokenInvalidException()

    category = payload.get("cat")
    if not category or not isinstance(category, str):
        raise UnsubscribeTokenInvalidException()

    # Whitelist stricte : refuse les catégories inconnues (protège d'un
    # JWT forgé avec une catégorie exotique, même si la signature est OK).
    if category not in {"tasks", "payments", "security", "digest", "product"}:
        raise UnsubscribeTokenInvalidException()

    return payload
