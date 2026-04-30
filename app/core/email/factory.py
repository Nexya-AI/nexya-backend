"""
Factory — retourne le bon EmailService selon la config.

Règle : si `settings.brevo_api_key` est vide → MockEmailService,
sinon BrevoEmailService. Singleton process-wide pour réutiliser
le pool HTTP et accumuler les emails mockés pendant les tests.
"""

from __future__ import annotations

import structlog

from app.config import settings
from app.core.email.base import EmailService
from app.core.email.brevo import BrevoEmailService
from app.core.email.mock import MockEmailService

log = structlog.get_logger()

_email_service_singleton: EmailService | None = None


def get_email_service() -> EmailService:
    global _email_service_singleton
    if _email_service_singleton is not None:
        return _email_service_singleton

    if settings.brevo_api_key:
        _email_service_singleton = BrevoEmailService(
            api_key=settings.brevo_api_key,
            sender_email=settings.brevo_sender_email,
            sender_name=settings.brevo_sender_name,
        )
        log.info("email.service.initialized", provider="brevo")
    else:
        _email_service_singleton = MockEmailService()
        log.warning(
            "email.service.initialized",
            provider="mock",
            reason="BREVO_API_KEY vide — emails loggués au lieu d'être envoyés",
        )
    return _email_service_singleton


async def close_email_service() -> None:
    """À appeler dans le lifespan shutdown."""
    global _email_service_singleton
    if _email_service_singleton is not None:
        await _email_service_singleton.close()
        _email_service_singleton = None


def reset_email_service_for_tests() -> None:
    """Tests only — force un nouveau singleton au prochain `get_email_service()`."""
    global _email_service_singleton
    _email_service_singleton = None
