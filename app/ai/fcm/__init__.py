"""
NEXYA Couche notifications — Firebase Cloud Messaging (FCM).

Ce module fournit une abstraction mock-first au-dessus de FCM HTTP v1. Le
pattern est strictement aligné sur les autres providers NEXYA (Brevo email,
hCaptcha, ObjectStore, VirusScanner, VoiceProvider, VisionProvider,
EmbeddingsProvider) : une ABC + un provider réel (Firebase) + un Mock
déterministe + une factory singleton mock-first.

Règle de bascule :
- `FCM_SERVICE_ACCOUNT_JSON` et `FCM_SERVICE_ACCOUNT_FILE` vides → MockFCMProvider
  (warning unique au boot « FCM en mode Mock — PROD non opérationnel »).
- L'une des deux settings remplie → FirebaseFCMProvider réel.

Le worker Planner (`workers/scheduler_tasks.execute_scheduled_task`) consomme
ce module après l'INSERT `scheduled_task_results` pour envoyer un push sur
chaque `device_token` actif de l'user propriétaire. L'envoi est strictement
fail-safe : une erreur FCM ne casse jamais le worker (log warning puis on
continue).
"""

from __future__ import annotations

from .base import (
    FCMError,
    FCMInvalidArgumentError,
    FCMProvider,
    FCMResult,
    FCMUnavailableError,
    FCMUnregisteredError,
)
from .mock_fcm import MockFCMProvider
from .runtime import get_fcm_provider, reset_fcm_provider_for_tests

__all__ = [
    "FCMProvider",
    "FCMResult",
    "FCMError",
    "FCMUnregisteredError",
    "FCMInvalidArgumentError",
    "FCMUnavailableError",
    "MockFCMProvider",
    "get_fcm_provider",
    "reset_fcm_provider_for_tests",
]
