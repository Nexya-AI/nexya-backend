"""Worker arq — emails lifecycle (Phase 3).

`send_onboarding_emails` : cron quotidien qui envoie la séquence d'onboarding
J+1 / J+3 / J+7 aux nouveaux inscrits. Idempotent (table `lifecycle_emails`),
gate consentement (catégorie `product`), fail-safe absolu.

Pattern de ciblage : pour chaque étape (N jours, clé), on sélectionne les users
créés dans la fenêtre `]now-(N+grace) ; now-N]` qui n'ont pas encore reçu cette
clé. La fenêtre :
  - évite le mass-backfill au 1er déploiement (les vieux comptes sont hors
    fenêtre),
  - rattrape un cron manqué (grace = quelques jours de tolérance),
  - combinée au « claim » UNIQUE, garantit zéro doublon.

Réengagement + digest hebdo réutiliseront cette même mécanique (table +
claim + gate) avec d'autres clés/critères — c'est la fondation lifecycle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog
from sqlalchemy import select

from app.config import settings
from app.core.auth.unsubscribe_tokens import create_unsubscribe_token
from app.core.database.postgres import AsyncSessionLocal
from app.core.email import EmailMessage, get_email_service, get_template_renderer
from app.core.email.base import EmailSendException
from app.features.auth.models import User
from app.features.lifecycle.models import LifecycleEmail
from app.features.lifecycle.service import LifecycleEmailService

log = structlog.get_logger()

# (jours après inscription, clé idempotente, template, objet de l'email)
_ONBOARDING_STAGES: list[tuple[int, str, str, str]] = [
    (1, "onboarding_d1", "onboarding_d1", "Découvre tes experts NEXYA 🎯"),
    (3, "onboarding_d3", "onboarding_d3", "NEXYA t'écoute et te voit 🎙️📸"),
    (7, "onboarding_d7", "onboarding_d7", "Déjà une semaine — 3 astuces de pro 🚀"),
]

# Catégorie de préférence/consentement pour l'onboarding (lifecycle produit).
_ONBOARDING_CATEGORY = "product"


def _build_unsubscribe_url(user_id, category: str) -> str | None:
    """URL de désinscription pour le footer (lifecycle = désinscriptible)."""
    try:
        token = create_unsubscribe_token(user_id, category)
        base = (settings.frontend_unsubscribe_url or "").rstrip("?&")
        if not base:
            return None
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}token={token}"
    except Exception as exc:  # noqa: BLE001
        log.warning("lifecycle.unsubscribe_url_failed", error=str(exc))
        return None


async def send_onboarding_emails(ctx: dict[str, Any]) -> dict[str, Any]:
    """Cron quotidien — envoie les emails d'onboarding J+1 / J+3 / J+7.

    Fail-safe absolu : aucune exception ne remonte au runtime arq (sinon retry
    en boucle). Chaque étape + chaque user sont isolés.
    """
    if not settings.onboarding_emails_enabled:
        return {"skipped": True, "reason": "disabled"}

    grace = settings.onboarding_window_grace_days
    batch = settings.onboarding_batch_size
    now = datetime.now(tz=UTC)

    totals = {"sent": 0, "skipped_optout": 0, "skipped_race": 0, "failed": 0}

    for n_days, email_key, template_name, subject in _ONBOARDING_STAGES:
        lower = now - timedelta(days=n_days + grace)
        upper = now - timedelta(days=n_days)
        try:
            async with AsyncSessionLocal() as db:
                stmt = (
                    select(User)
                    .where(
                        User.is_active.is_(True),
                        User.deleted_at.is_(None),
                        User.created_at > lower,
                        User.created_at <= upper,
                        ~select(LifecycleEmail.id)
                        .where(
                            LifecycleEmail.user_id == User.id,
                            LifecycleEmail.email_key == email_key,
                        )
                        .exists(),
                    )
                    .limit(batch)
                )
                users = list((await db.execute(stmt)).scalars().all())

                for user in users:
                    outcome = await _process_onboarding_user(
                        user=user,
                        email_key=email_key,
                        template_name=template_name,
                        subject=subject,
                        db=db,
                    )
                    totals[outcome] = totals.get(outcome, 0) + 1
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "lifecycle.onboarding.stage_failed",
                email_key=email_key,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    log.info("lifecycle.onboarding.done", **totals)
    return {"skipped": False, **totals}


async def _process_onboarding_user(
    *,
    user: User,
    email_key: str,
    template_name: str,
    subject: str,
    db,
) -> str:
    """Traite un user pour une étape. Retourne le bucket de stats.

    Ordre : gate consentement → claim atomique → envoi → release si échec.
    """
    # 1. Gate consentement (désinscrit → on ne claim même pas).
    if not await LifecycleEmailService.should_send_lifecycle(user, _ONBOARDING_CATEGORY, db):
        return "skipped_optout"

    # 2. Claim atomique du slot (anti-doublon + anti-race multi-workers).
    if not await LifecycleEmailService.claim(user.id, email_key, db):
        return "skipped_race"

    # 3. Envoi. Échec → release pour réessayer au prochain run.
    user_name = user.display_name or user.username or user.email.split("@")[0]
    unsubscribe_url = _build_unsubscribe_url(user.id, _ONBOARDING_CATEGORY)
    try:
        renderer = get_template_renderer()
        html_body, text_body = renderer.render(
            template_name,
            user_name=user_name,
            unsubscribe_url=unsubscribe_url,
        )
        message = EmailMessage(
            to_email=user.email,
            to_name=user_name,
            subject=subject,
            html_body=html_body,
            text_body=text_body,
            tags=["lifecycle", email_key],
        )
        await get_email_service().send(message)
    except EmailSendException as exc:
        log.warning("lifecycle.onboarding.send_failed", email_key=email_key, error=str(exc))
        await LifecycleEmailService.release(user.id, email_key, db)
        return "failed"
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "lifecycle.onboarding.send_unexpected",
            email_key=email_key,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        await LifecycleEmailService.release(user.id, email_key, db)
        return "failed"

    return "sent"
