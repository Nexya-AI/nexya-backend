"""
MockEmailService — utilisé en dev/test quand BREVO_API_KEY est vide.

Logge le payload de l'email au lieu de l'envoyer. Accumule aussi
les messages envoyés dans une liste in-memory pour que les tests
puissent asserter ce qui a été "envoyé".
"""

from __future__ import annotations

import structlog

from app.core.email.base import EmailMessage, EmailService

log = structlog.get_logger()


class MockEmailService(EmailService):
    """Faux provider — loggue + garde en mémoire les emails 'envoyés'."""

    def __init__(self) -> None:
        self.sent: list[EmailMessage] = []

    async def send(self, message: EmailMessage) -> None:
        self.sent.append(message)
        log.info(
            "email.mock.sent",
            to=message.to_email,
            subject=message.subject,
            tags=message.tags,
            preview=message.text_body[:200],
        )

    def clear(self) -> None:
        """Utilitaire test — réinitialise la file."""
        self.sent.clear()
