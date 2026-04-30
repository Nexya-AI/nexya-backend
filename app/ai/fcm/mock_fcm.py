"""
MockFCMProvider — accumule les appels en mémoire pour dev/test.

Utilisé quand `FCM_SERVICE_ACCOUNT_JSON` et `FCM_SERVICE_ACCOUNT_FILE` sont
vides (mode dev/CI sans clé Firebase). Retourne un succès déterministe
pour qu'un pipeline de push complet soit testable sans toucher FCM, et
expose deux flags `force_fail`/`force_unregistered` pour scripter les
erreurs dans les tests.
"""

from __future__ import annotations

from uuid import uuid4

import structlog

from .base import (
    FCMProvider,
    FCMResult,
    FCMUnavailableError,
    FCMUnregisteredError,
)

log = structlog.get_logger(__name__)


class MockFCMProvider(FCMProvider):
    """Provider factice — log + accumule les appels."""

    name: str = "mock-fcm"

    def __init__(
        self,
        *,
        force_fail: bool = False,
        force_unregistered: bool = False,
    ) -> None:
        self._force_fail = force_fail
        self._force_unregistered = force_unregistered
        self.calls: list[dict] = []

    async def send_push(
        self,
        token: str,
        *,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> FCMResult:
        call_payload = {
            "token": token,
            "title": title,
            "body": body,
            "data": dict(data) if data else {},
        }
        self.calls.append(call_payload)

        if self._force_unregistered:
            raise FCMUnregisteredError(
                "Mock: token marqué comme non enregistré.",
                token=token,
            )
        if self._force_fail:
            raise FCMUnavailableError(
                "Mock: FCM indisponible.",
                token=token,
            )

        message_id = f"mock-{uuid4().hex[:16]}"
        log.debug(
            "fcm.mock.send_push",
            token=token[:8] + "…",
            title=title,
            message_id=message_id,
        )
        return FCMResult(success=True, message_id=message_id)
