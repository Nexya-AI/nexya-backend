"""
AuthEvent service — journalisation forensic des événements d'auth.

Chaque action sensible (register réussi/échoué, login réussi/échoué,
reset password, logout, delete account, captcha échoué, quota device
dépassé) insère une ligne dans `auth_events`.

Règles :
- **Jamais de PII en clair** : pas d'email, pas de mot de passe,
  pas de token. Seul l'user_id (UUID opaque) est persisté.
- **user_id nullable** : un register échoué sur un email inexistant
  n'a pas d'user_id, mais on veut la trace (IP + user_agent +
  device_id hashé + event_type='register_failed').
- **Fail-safe** : si l'INSERT échoue (DB down, timeout), on log un
  warning et on continue. L'audit est important mais ne doit
  JAMAIS bloquer une auth légitime.
- **metadata libre** : un dict JSONB pour les codes d'erreur,
  compteurs, identifiants techniques. Typé côté Python mais libre
  côté stockage — on pourra ajouter des champs sans migration.

La granularité est délibérément fine : on préfère 10 lignes
nominales par utilisateur / mois à 1 ligne « tout-en-un » qui rend
les requêtes forensic plus difficiles.
"""

from __future__ import annotations

import uuid
from typing import Literal

import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.models import AuthEvent

log = structlog.get_logger()

# Les valeurs légales pour `event_type` — doivent correspondre 1:1 au
# CHECK SQL `ck_auth_events_event_type`. Un mismatch entre Python et
# SQL = l'INSERT rebondit avec une IntegrityError au runtime.
AuthEventType = Literal[
    "register_success",
    "register_failed",
    "login_success",
    "login_failed",
    "logout",
    "password_change",
    "password_reset_request",
    "password_reset_success",
    "account_delete",
    "captcha_failed",
    "device_quota_exceeded",
    # ── Session J1 — RGPD + AI Act ───────────────────────────
    "consent_granted",  # Article 7 RGPD — consentement accordé
    "consent_revoked",  # Article 7 RGPD — consentement révoqué
    "account_delete_requested",  # Article 17 — demande créée (workflow 2-step)
    "account_delete_cancelled",  # Article 17 — rétractation user avant J+30
    "data_exported",  # Article 15 — preuve d'envoi d'export ZIP
]

# Longueur max pour le user-agent qu'on stocke. Les User-Agents
# légitimes dépassent rarement 200 chars ; les UA très longs sont
# souvent des fingerprints anti-bot, on les tronque sans perte.
_USER_AGENT_MAX_CHARS = 256


async def log_auth_event(
    db: AsyncSession,
    *,
    event_type: AuthEventType,
    user_id: uuid.UUID | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
    device_id: str | None = None,
    metadata: dict | None = None,
) -> None:
    """Insère un événement d'authentification.

    Args:
        db:          session SQLAlchemy async **en dehors** de la
                     transaction principale (on commite séparément
                     pour ne pas perdre l'audit si l'auth échoue).
        event_type:  un des littéraux `AuthEventType`.
        user_id:     UUID de l'user impacté si connu.
        ip:          IP client (déjà extraite de X-Forwarded-For côté caller).
        user_agent:  header User-Agent, tronqué à 256 chars côté Python
                     avant insertion (le CHECK DB limite à 256 aussi).
        device_id:   header X-Device-Id (non hashé — c'est un identifiant
                     opaque généré côté Flutter, pas une PII au sens RGPD).
        metadata:    dict libre stocké en JSONB. Ne jamais y mettre
                     d'email / mot de passe / token.

    **Ne lève jamais d'exception visible côté caller** : un échec
    d'audit ne doit pas cascader en erreur d'auth. Un warning log
    suffit pour que l'ops détecte le problème.
    """
    if user_agent and len(user_agent) > _USER_AGENT_MAX_CHARS:
        user_agent = user_agent[:_USER_AGENT_MAX_CHARS]

    try:
        event = AuthEvent(
            user_id=user_id,
            event_type=event_type,
            ip=ip,
            user_agent=user_agent,
            device_id=device_id,
            metadata_json=metadata,
        )
        db.add(event)
        await db.flush()
        await db.commit()
    except SQLAlchemyError as exc:  # pragma: no cover — défensif
        # Impossible de rollback proprement ici — on a commit avant.
        # On loggue un warning explicite : si ça arrive, c'est que la
        # DB est en vrac, et l'auth qui suit va probablement planter aussi.
        log.warning(
            "auth_event.insert_failed",
            event_type=event_type,
            user_id=str(user_id) if user_id else None,
            error=str(exc),
        )
