"""
FCM — contrat abstrait.

L'ABC `FCMProvider` expose une seule méthode `send_push(token, title, body,
data)`. Un provider (Firebase réel, Mock) retourne un `FCMResult` plat
indiquant le succès et l'identifiant du message, ou lève une exception
typée pour les échecs.

La hiérarchie d'erreurs distingue trois cas opérationnels :

- `FCMUnregisteredError` — le token a expiré ou a été invalidé côté client.
  Le caller doit le soft-delete en DB pour ne plus l'utiliser (housekeeping
  automatique côté worker Planner).
- `FCMInvalidArgumentError` — payload malformé (bug NEXYA). Non-retryable.
- `FCMUnavailableError` — le service FCM est indisponible (5xx, 429,
  timeout). Retryable selon politique du caller ; le worker Planner se
  contente de log + continue.

Pattern miroir des providers `ChatProvider`/`VoiceProvider`/`EmbeddingsProvider`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class FCMResult:
    """Résultat d'un envoi FCM — toujours retourné par `send_push`.

    `success=False` avec `error_code`/`error_message` n'est pas utilisé par
    les providers actuels (on préfère lever une exception typée), mais
    l'API reste ouverte pour un futur provider non-blocking.
    """

    success: bool
    message_id: str | None = None
    error_code: str | None = None
    error_message: str | None = None


class FCMError(Exception):
    """Erreur de base pour tout échec FCM."""

    def __init__(
        self,
        message: str,
        *,
        token: str | None = None,
        status_code: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.token = token
        self.status_code = status_code
        self.retryable = retryable


class FCMUnregisteredError(FCMError):
    """Token expiré ou désinstallé. À soft-delete côté DB."""

    def __init__(self, message: str, *, token: str | None = None) -> None:
        super().__init__(message, token=token, status_code=404, retryable=False)


class FCMInvalidArgumentError(FCMError):
    """Payload FCM malformé — bug applicatif côté NEXYA."""

    def __init__(self, message: str, *, token: str | None = None) -> None:
        super().__init__(message, token=token, status_code=400, retryable=False)


class FCMUnavailableError(FCMError):
    """FCM est injoignable (429/5xx/timeout). Retryable."""

    def __init__(
        self,
        message: str,
        *,
        token: str | None = None,
        status_code: int | None = None,
    ) -> None:
        super().__init__(message, token=token, status_code=status_code, retryable=True)


class FCMProvider(ABC):
    """Contrat qu'un provider FCM doit remplir."""

    name: str = ""

    @abstractmethod
    async def send_push(
        self,
        token: str,
        *,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> FCMResult:
        """Envoie une notification push à `token`.

        Contrat :
        - Retourne `FCMResult(success=True, message_id=...)` en cas de succès.
        - Lève `FCMUnregisteredError` si le token est expiré / invalide.
        - Lève `FCMInvalidArgumentError` si le payload est malformé.
        - Lève `FCMUnavailableError` si FCM répond 5xx/429/timeout.
        - Le dict `data` est envoyé tel quel (strings uniquement côté FCM).
        """
        raise NotImplementedError

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self.name!r})"
