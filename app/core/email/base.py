"""
Interface abstraite pour l'envoi d'emails transactionnels.

Un seul contrat — plusieurs implémentations : Brevo en prod,
Mock en dev/test (logge au lieu d'envoyer). Les callers métier
ne connaissent jamais le provider concret.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(slots=True)
class EmailMessage:
    """Payload neutre accepté par tous les providers."""

    to_email: str
    to_name: str | None
    subject: str
    html_body: str
    text_body: str
    tags: list[str] = field(default_factory=list)


class EmailService(ABC):
    """Contrat commun — `send()` est la seule méthode obligatoire."""

    @abstractmethod
    async def send(self, message: EmailMessage) -> None:
        """Envoie un email. Lève `EmailSendException` sur échec transport."""

    async def close(self) -> None:  # pragma: no cover — surchargée par Brevo
        """Libère les ressources (clients HTTP, pools). No-op par défaut."""
        return None


class EmailSendException(Exception):
    """Échec transport côté provider — remonter l'info sans fuiter les détails."""
