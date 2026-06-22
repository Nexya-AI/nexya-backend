"""Worker arq — notifications transactionnelles auth (Phase 1 couverture).

Déporte hors du cycle HTTP les envois déclenchés par les flux d'auth, pour
que `register` / `login` / `change_password` / `reset_password` restent
rapides (pas d'I/O réseau email/FCM synchrone dans la requête) :

    - `send_welcome_email`   → email de bienvenue + notification in-app
                               (déclenché par `register`).
    - `send_security_alert`  → alerte sécurité (nouvelle connexion device
                               inconnu, mot de passe modifié) via le
                               `NotificationDispatcher` catégorie `security`.

Pattern enqueue aligné `document_tasks.enqueue_document_generation` /
`chat_tasks.enqueue_title_generation` (lazy-pool fail-silent). Les tâches
elles-mêmes sont **fail-safe absolu** : aucune exception ne remonte au
runtime arq (sinon retry infini sur un échec déterministe). Une panne
Brevo/FCM/DB est loggée et la fonction retourne un dict de stats.

Aucune migration : `security` est déjà une `category` + un `source_kind`
valides (cf. `Notification.__table_args__`), et le template
`account_security_alert` consomme déjà `event_type` / `event_ip` /
`event_user_agent_truncated` / `event_time_utc`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

from app.config import settings
from app.core.database.postgres import AsyncSessionLocal
from app.core.email import EmailMessage, get_email_service, get_template_renderer
from app.core.email.base import EmailSendException
from app.features.auth.models import User
from app.features.notifications.service import (
    NotificationDispatcher,
    NotificationService,
)

if TYPE_CHECKING:
    from arq.connections import ArqRedis

log = structlog.get_logger()

# Cap défensif sur l'UA stocké/affiché dans l'email sécurité (un UA légitime
# dépasse rarement 120 chars ; au-delà c'est souvent un fingerprint anti-bot).
_UA_DISPLAY_MAX_CHARS = 120


# ══════════════════════════════════════════════════════════════
# Pool arq lazy — identique document_tasks / chat_tasks
# ══════════════════════════════════════════════════════════════

_arq_pool: ArqRedis | None = None


async def _get_arq_pool() -> ArqRedis:
    """Pool arq paresseux — créé une seule fois par process."""
    global _arq_pool
    if _arq_pool is None:
        from arq.connections import RedisSettings, create_pool  # noqa: PLC0415

        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _arq_pool


# ══════════════════════════════════════════════════════════════
# ENQUEUE — fire-and-forget fail-silent (appelés depuis auth/service)
# ══════════════════════════════════════════════════════════════


async def enqueue_welcome_email(user_id: uuid.UUID) -> None:
    """Enqueue l'email de bienvenue après une inscription réussie.

    Échec silencieux (log warning + return) si Redis est down — un welcome
    perdu n'est jamais critique, et `register` ne doit JAMAIS échouer parce
    que la file de notifications hoquette.
    """
    try:
        pool = await _get_arq_pool()
        await pool.enqueue_job("send_welcome_email", str(user_id))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "notifications.welcome.enqueue_failed",
            user_id=str(user_id),
            error=str(exc),
            error_type=type(exc).__name__,
        )


async def enqueue_security_alert(
    *,
    user_id: uuid.UUID,
    event_type: str,
    ip: str | None = None,
    user_agent: str | None = None,
    device_id: str | None = None,
) -> None:
    """Enqueue une alerte sécurité (nouvelle connexion / mot de passe modifié).

    `event_type` ∈ {`new_device_login`, `password_changed`}. Tous les
    arguments sont JSON-sérialisables (strings/None) pour transiter via Redis.
    Échec silencieux : une alerte perdue ne doit pas bloquer le login ni le
    changement de mot de passe.
    """
    try:
        pool = await _get_arq_pool()
        await pool.enqueue_job(
            "send_security_alert",
            str(user_id),
            event_type,
            ip,
            user_agent,
            device_id,
            datetime.now(tz=UTC).isoformat(),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "notifications.security_alert.enqueue_failed",
            user_id=str(user_id),
            event_type=event_type,
            error=str(exc),
            error_type=type(exc).__name__,
        )


# ══════════════════════════════════════════════════════════════
# Helpers de contenu (purs, testables sans DB)
# ══════════════════════════════════════════════════════════════


def build_security_alert_content(event_type: str) -> tuple[str, str]:
    """Retourne `(title, body)` FR pour une alerte sécurité.

    `title` = objet de l'email + titre de la notif in-app.
    `body`  = corps affiché dans le template `account_security_alert`.
    """
    if event_type == "new_device_login":
        return (
            "Nouvelle connexion à ton compte NEXYA",
            "Une connexion à ton compte vient d'être détectée depuis un appareil "
            "que nous ne connaissions pas encore. Si c'est bien toi, tu peux ignorer "
            "ce message. Sinon, change ton mot de passe immédiatement et contacte-nous.",
        )
    if event_type == "password_changed":
        return (
            "Ton mot de passe NEXYA a été modifié",
            "Le mot de passe de ton compte vient d'être changé. Si tu es à l'origine "
            "de ce changement, aucune action n'est nécessaire. Sinon, ton compte est "
            "peut-être compromis : contacte-nous immédiatement.",
        )
    # Fallback générique — ne devrait pas arriver (event_type contrôlé côté caller).
    return (
        "Activité de sécurité sur ton compte NEXYA",
        "Une activité importante vient d'être détectée sur ton compte.",
    )


def _format_utc_human(iso: str) -> str:
    """Formate un ISO datetime en 'YYYY-MM-DD HH:MM UTC' lisible.

    Fail-safe : retourne l'ISO brut si le parsing échoue (jamais de crash
    pour un simple affichage).
    """
    try:
        dt = datetime.fromisoformat(iso)
        return dt.strftime("%Y-%m-%d %H:%M UTC")
    except (ValueError, TypeError):
        return iso


# ══════════════════════════════════════════════════════════════
# WORKER — send_welcome_email
# ══════════════════════════════════════════════════════════════


async def send_welcome_email(ctx: dict[str, Any], user_id: str) -> dict[str, Any]:
    """Envoie l'email de bienvenue + crée une notification in-app.

    Fail-safe absolu : chaque étape (email, notif in-app) est indépendante
    et capturée. Un échec d'envoi email n'empêche pas la notif in-app, et
    aucun échec ne remonte au runtime arq.
    """
    try:
        uid = uuid.UUID(user_id)
    except (ValueError, TypeError):
        log.warning("notifications.welcome.bad_user_id", user_id=user_id)
        return {"skipped": True, "reason": "bad_user_id"}

    async with AsyncSessionLocal() as db:
        user = await db.get(User, uid)
        if user is None or user.deleted_at is not None or not user.is_active:
            return {"skipped": True, "reason": "user_missing_or_inactive"}

        user_name = user.display_name or user.username or user.email.split("@")[0]

        # ── Étape 1 : email de bienvenue (transactionnel, direct) ──
        # Pas via le dispatcher : `welcome` n'a pas de catégorie RGPD avec
        # template, et un email de bienvenue est transactionnel (consécutif
        # à la création de compte), pas marketing → envoyé sans consentement.
        email_sent = False
        try:
            renderer = get_template_renderer()
            html_body, text_body = renderer.render(
                "welcome",
                user_name=user_name,
                # Footer partagé : `unsubscribe_url=None` → le `{% if %}` masque
                # la ligne (un email de bienvenue n'est pas désinscriptible).
                unsubscribe_url=None,
            )
            message = EmailMessage(
                to_email=user.email,
                to_name=user_name,
                subject="Bienvenue sur NEXYA 👋",
                html_body=html_body,
                text_body=text_body,
                tags=["welcome"],
            )
            await get_email_service().send(message)
            email_sent = True
        except EmailSendException as exc:
            log.warning(
                "notifications.welcome.email_failed",
                user_id=user_id,
                error=str(exc),
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "notifications.welcome.email_unexpected",
                user_id=user_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        # ── Étape 2 : notification in-app (timeline) ───────────────
        # Crée une row directement (pas via dispatcher : on ne veut pas que
        # la préférence `product` puisse skipper la bienvenue). Le
        # `channel_used` reflète le canal réellement utilisé.
        in_app_created = False
        try:
            await NotificationService.create(
                user_id=uid,
                category="product",
                title="Bienvenue sur NEXYA 👋",
                body=(
                    "Ton compte est prêt. Pose ta première question, choisis un mode "
                    "expert, ou montre une photo à NEXYA."
                ),
                data={"deep_link": "nexya://home"},
                channel_used="email" if email_sent else "skipped",
                source_task_id=None,
                source_kind="product",
                push_message_id=None,
                email_message_id=None,
                attempts_push=0,
                attempts_email=1 if email_sent else 0,
                db=db,
            )
            in_app_created = True
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "notifications.welcome.in_app_failed",
                user_id=user_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    log.info(
        "notifications.welcome.done",
        user_id=user_id,
        email_sent=email_sent,
        in_app_created=in_app_created,
    )
    return {
        "skipped": False,
        "email_sent": email_sent,
        "in_app_created": in_app_created,
    }


# ══════════════════════════════════════════════════════════════
# WORKER — send_security_alert
# ══════════════════════════════════════════════════════════════


async def send_security_alert(
    ctx: dict[str, Any],
    user_id: str,
    event_type: str,
    ip: str | None,
    user_agent: str | None,
    device_id: str | None,
    occurred_at_iso: str,
) -> dict[str, Any]:
    """Dispatche une alerte sécurité via le `NotificationDispatcher`.

    Catégorie `security` → canal par défaut `email` (non-désinscriptible),
    + push si l'user a choisi `push`/`both`. Le template
    `account_security_alert` consomme `event_type` / `event_ip` /
    `event_user_agent_truncated` / `event_time_utc`.

    Fail-safe absolu : le dispatcher ne lève jamais, et toute autre erreur
    est capturée (l'alerte est importante mais un crash worker en boucle
    serait pire).
    """
    try:
        uid = uuid.UUID(user_id)
    except (ValueError, TypeError):
        log.warning("notifications.security_alert.bad_user_id", user_id=user_id)
        return {"skipped": True, "reason": "bad_user_id"}

    try:
        async with AsyncSessionLocal() as db:
            user = await db.get(User, uid)
            if user is None or user.deleted_at is not None or not user.is_active:
                return {"skipped": True, "reason": "user_missing_or_inactive"}

            title, body = build_security_alert_content(event_type)
            data: dict[str, Any] = {
                "event_type": event_type,
                "event_ip": ip or "",
                "event_user_agent_truncated": (user_agent or "")[:_UA_DISPLAY_MAX_CHARS],
                "event_time_utc": _format_utc_human(occurred_at_iso),
                "deep_link": "nexya://settings/privacy",
            }

            await NotificationDispatcher.dispatch(
                user=user,
                category="security",
                title=title,
                body=body,
                data=data,
                source_kind="security",
                db=db,
            )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "notifications.security_alert.failed",
            user_id=user_id,
            event_type=event_type,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return {"skipped": True, "reason": "dispatch_error"}

    log.info(
        "notifications.security_alert.done",
        user_id=user_id,
        event_type=event_type,
    )
    return {"skipped": False, "event_type": event_type}
