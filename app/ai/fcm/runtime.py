"""
Factory singleton FCM mock-first.

Pattern miroir `ai/voice/runtime.py`, `ai/vision/runtime.py`,
`ai/embeddings/runtime.py` : une seule fonction `get_fcm_provider()` qui
retourne `MockFCMProvider` si aucune clé Firebase n'est posée, et
`FirebaseFCMProvider` sinon. Un warning unique est loggé au boot quand le
mode Mock est choisi pour rappeler que la prod n'enverra PAS de push tant
que le service account n'est pas branché.
"""

from __future__ import annotations

import structlog

from app.config import settings

from .base import FCMProvider
from .firebase_fcm import FirebaseFCMProvider, load_service_account_info
from .mock_fcm import MockFCMProvider

log = structlog.get_logger(__name__)

_provider: FCMProvider | None = None
_mock_warning_emitted = False


def get_fcm_provider() -> FCMProvider:
    """Retourne le singleton FCM (Mock si clé vide, Firebase sinon)."""
    global _provider, _mock_warning_emitted

    if _provider is not None:
        return _provider

    force_mock = bool(getattr(settings, "fcm_mock_enabled", False))
    has_key = bool(
        (settings.fcm_service_account_json or "").strip()
        or (settings.fcm_service_account_file or "").strip()
    )

    if force_mock or not has_key:
        if not _mock_warning_emitted:
            log.warning(
                "⚠️ FCM en mode Mock — PROD non opérationnel "
                "(renseigner FCM_SERVICE_ACCOUNT_JSON ou "
                "FCM_SERVICE_ACCOUNT_FILE pour activer Firebase).",
            )
            _mock_warning_emitted = True
        _provider = MockFCMProvider()
        return _provider

    try:
        info = load_service_account_info()
    except Exception as exc:  # noqa: BLE001
        log.error(
            "fcm.service_account.load_failed",
            error=str(exc),
            error_type=type(exc).__name__,
        )
        log.warning(
            "⚠️ FCM fallback Mock — service account invalide.",
        )
        _provider = MockFCMProvider()
        return _provider

    if info is None:
        # Garde-fou : les deux settings vides malgré has_key=True (bug).
        _provider = MockFCMProvider()
        return _provider

    project_id = (settings.fcm_project_id or "").strip() or None
    _provider = FirebaseFCMProvider(service_account_info=info, project_id=project_id)
    log.info(
        "fcm.provider.ready",
        provider="firebase",
        project_id=_provider._project_id,  # noqa: SLF001
    )
    return _provider


def reset_fcm_provider_for_tests() -> None:
    """Réinitialise le singleton + le flag warning (utile pour isolation
    tests qui monkeypatchent les settings)."""
    global _provider, _mock_warning_emitted
    _provider = None
    _mock_warning_emitted = False
