"""SuggestionService — formulaire user → équipe NEXYA (Session N1).

Pipeline submit :
1. Rate limit pré-flight 5/jour/user (anti-spam) → 429 si dépassé.
2. INSERT `UserSuggestion` (commit).
3. Email fail-safe à `settings.feedback_team_email` via `EmailService`
   (Brevo en prod, Mock en dev). Exception swallowed — l'INSERT DB
   reste valide même si Brevo plante.
4. Log forensic structlog.

Aucun appel LLM — coût $0 hors email Brevo (~$0.0001/email).
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.email import EmailMessage
from app.core.email.factory import get_email_service
from app.core.email.renderer import get_template_renderer
from app.core.errors.exceptions import RateLimitAbuseException
from app.core.security.rate_limiter import check_user_rate_limit
from app.features.auth.models import User
from app.features.rgpd.data_export_service import _anonymize_ip
from app.features.suggestions.models import UserSuggestion
from app.features.suggestions.schemas import SuggestionCreate

log = structlog.get_logger(__name__)


class SuggestionService:
    """Service stateless de soumission de suggestions user → équipe."""

    @staticmethod
    async def submit(
        user: User,
        body: SuggestionCreate,
        *,
        ip: str | None,
        user_agent: str | None,
        db: AsyncSession,
    ) -> UserSuggestion:
        """Soumet une suggestion + envoie l'email équipe (fail-safe).

        Rate limit 5/jour/user via `check_user_rate_limit` — si dépassé,
        lève `RateLimitAbuseException` (429 + retry_after dans data).
        """
        # 1. Rate limit pré-flight
        await check_user_rate_limit(
            user.id,
            action="suggestion",
            max_requests=settings.suggestions_rate_limit_per_day,
            window_seconds=24 * 3600,
            on_exceeded=RateLimitAbuseException,
        )

        # 2. INSERT
        suggestion = UserSuggestion(
            user_id=user.id,
            suggestion_type=body.suggestion_type,
            body=body.body,
            ip_address=ip,
            user_agent=user_agent,
            processing_status="open",
        )
        db.add(suggestion)
        await db.flush()
        await db.refresh(suggestion)
        await db.commit()

        # 3. Email fail-safe à l'équipe
        await SuggestionService._send_team_email(suggestion, user)

        log.info(
            "suggestions.submitted",
            user_id=str(user.id),
            suggestion_id=str(suggestion.id),
            suggestion_type=suggestion.suggestion_type,
            body_length=len(suggestion.body),
        )
        return suggestion

    @staticmethod
    async def _send_team_email(suggestion: UserSuggestion, user: User) -> None:
        """Envoie l'email à l'équipe NEXYA. Fail-safe absolu — toute
        exception est loggée mais ne propage pas (l'INSERT est déjà
        commité, l'échec email ne doit pas casser la submit user).
        """
        try:
            renderer = get_template_renderer()
            html_body, text_body = renderer.render(
                "suggestion_received",
                suggestion_type=suggestion.suggestion_type,
                body=suggestion.body,
                user_email=user.email or "—",
                user_id=str(user.id),
                ip_anonymized=_anonymize_ip(suggestion.ip_address) or "—",
                created_at=(
                    suggestion.created_at.isoformat()
                    if suggestion.created_at
                    else datetime.now(UTC).isoformat()
                ),
                # Footer partial : pas d'unsubscribe URL pour les emails
                # internes équipe (le footer F3 a guard `{% if unsubscribe_url %}`).
                unsubscribe_url=None,
            )
            email = EmailMessage(
                to_email=settings.feedback_team_email,
                to_name="NEXYA Team",
                subject=(
                    f"[NEXYA Suggestion] {suggestion.suggestion_type.upper()} "
                    f"— {suggestion.body[:60]}"
                ),
                html_body=html_body,
                text_body=text_body,
                tags=["suggestion", suggestion.suggestion_type],
            )
            service = get_email_service()
            await service.send(email)
            log.info(
                "suggestions.email_sent",
                suggestion_id=str(suggestion.id),
                to=settings.feedback_team_email,
            )
        except Exception as exc:  # noqa: BLE001 — fail-safe absolu
            log.warning(
                "suggestions.email_failed",
                suggestion_id=str(suggestion.id),
                error=str(exc),
                error_type=type(exc).__name__,
            )
