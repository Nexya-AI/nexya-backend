"""
Implémentation Brevo (ex-Sendinblue) de l'EmailService.

POST https://api.brevo.com/v3/smtp/email avec header `api-key`.
Doc : https://developers.brevo.com/reference/sendtransacemail
"""

from __future__ import annotations

import httpx
import structlog

from app.core.email.base import EmailMessage, EmailSendException, EmailService

log = structlog.get_logger()

_BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"
_DEFAULT_TIMEOUT_SECONDS = 10.0


class BrevoEmailService(EmailService):
    """Client HTTP async vers l'API Brevo v3."""

    def __init__(
        self,
        *,
        api_key: str,
        sender_email: str,
        sender_name: str,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not api_key:
            raise ValueError("BrevoEmailService requiert une api_key non vide")
        self._sender_email = sender_email
        self._sender_name = sender_name
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={
                "api-key": api_key,
                "accept": "application/json",
                "content-type": "application/json",
            },
        )

    async def send(self, message: EmailMessage) -> None:
        payload: dict[str, object] = {
            "sender": {"email": self._sender_email, "name": self._sender_name},
            "to": [
                {
                    "email": message.to_email,
                    **({"name": message.to_name} if message.to_name else {}),
                }
            ],
            "subject": message.subject,
            "htmlContent": message.html_body,
            "textContent": message.text_body,
        }
        if message.tags:
            payload["tags"] = message.tags

        try:
            response = await self._client.post(_BREVO_SEND_URL, json=payload)
        except httpx.HTTPError as exc:
            log.error("email.brevo.transport_error", error=str(exc))
            raise EmailSendException("Brevo transport error") from exc

        if response.status_code >= 400:
            # Pas de log du payload (contient l'email) — uniquement status + message_id si dispo
            log.error(
                "email.brevo.api_error",
                status=response.status_code,
                body=response.text[:500],
            )
            raise EmailSendException(f"Brevo API returned {response.status_code}")

        log.info(
            "email.brevo.sent",
            to_hash=_hash_email(message.to_email),
            subject=message.subject,
            tags=message.tags,
        )

    async def close(self) -> None:
        await self._client.aclose()


def _hash_email(email: str) -> str:
    """Hash court pour les logs — ne JAMAIS logger l'email brut."""
    import hashlib

    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]
