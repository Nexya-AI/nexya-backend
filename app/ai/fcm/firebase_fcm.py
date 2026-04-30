"""
FirebaseFCMProvider — client HTTP v1 OAuth2 service account.

FCM HTTP v1 (successeur de l'ancienne Legacy API) exige un access token
OAuth2 signé par le service account du projet Firebase. La signature JWT
(RS256) est faite en local via `google-auth`, puis échangée contre un
access token à `https://oauth2.googleapis.com/token`. Le token est ensuite
présenté en `Authorization: Bearer` sur chaque appel
`POST https://fcm.googleapis.com/v1/projects/{project_id}/messages:send`.

Le provider ne fait pas de retry interne (`max_retries=0` côté httpx) —
c'est le caller qui décide. Dans NEXYA, le worker Planner n'a pas besoin
de retry FCM : une erreur transitoire loggée est préférable à une tâche
qui empile des push répliqués.

Erreurs mappées strictement :
- 404 / `UNREGISTERED` → `FCMUnregisteredError`   (supprimer le token)
- 400 `INVALID_ARGUMENT` → `FCMInvalidArgumentError` (bug NEXYA)
- 429 / 5xx → `FCMUnavailableError` (retryable)
- timeout / connexion → `FCMUnavailableError`
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import structlog

from app.config import settings

from .base import (
    FCMInvalidArgumentError,
    FCMProvider,
    FCMResult,
    FCMUnavailableError,
    FCMUnregisteredError,
)

log = structlog.get_logger(__name__)

_OAUTH_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
_FCM_SEND_URL_TMPL = "https://fcm.googleapis.com/v1/projects/{project_id}/messages:send"


class FirebaseFCMProvider(FCMProvider):
    """Provider FCM réel basé sur HTTP v1 + OAuth2 service account."""

    name: str = "firebase"

    def __init__(
        self,
        *,
        service_account_info: dict[str, Any],
        project_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> None:
        self._service_account_info = service_account_info
        self._project_id = project_id or service_account_info.get("project_id")
        if not self._project_id:
            raise ValueError(
                "FirebaseFCMProvider : `project_id` manquant dans le service "
                "account JSON et non fourni explicitement."
            )
        self._timeout = float(
            timeout_seconds if timeout_seconds is not None else settings.fcm_push_timeout_seconds
        )
        self._credentials = None  # chargé lazy au premier appel

    # ── Credentials ─────────────────────────────────────────────
    def _get_credentials(self):  # pragma: no cover — testé via mock httpx
        """Charge les credentials à la demande (évite l'import google.auth
        au module-load pour que les tests Mock-only ne dépendent pas de
        la lib)."""
        if self._credentials is None:
            from google.oauth2.service_account import (  # noqa: PLC0415
                Credentials,
            )

            self._credentials = Credentials.from_service_account_info(
                self._service_account_info, scopes=[_OAUTH_SCOPE]
            )
        return self._credentials

    def _fetch_access_token(self) -> str:  # pragma: no cover
        from google.auth.transport.requests import Request  # noqa: PLC0415

        creds = self._get_credentials()
        if not creds.valid:
            creds.refresh(Request())
        return creds.token

    # ── Envoi ───────────────────────────────────────────────────
    async def send_push(
        self,
        token: str,
        *,
        title: str,
        body: str,
        data: dict[str, str] | None = None,
    ) -> FCMResult:
        access_token = self._fetch_access_token()
        url = _FCM_SEND_URL_TMPL.format(project_id=self._project_id)

        # Les `data` FCM doivent être des strings. On normalise ici.
        safe_data: dict[str, str] = {}
        if data:
            for k, v in data.items():
                safe_data[str(k)] = str(v) if v is not None else ""

        payload: dict[str, Any] = {
            "message": {
                "token": token,
                "notification": {"title": title, "body": body},
                "data": safe_data,
                "android": {"priority": "high"},
                "apns": {"headers": {"apns-priority": "10"}},
            }
        }

        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=payload, headers=headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise FCMUnavailableError(
                f"Timeout / transport FCM : {exc}",
                token=token,
            ) from exc

        return self._parse_response(response, token=token)

    # ── Parsing ─────────────────────────────────────────────────
    def _parse_response(self, response: httpx.Response, *, token: str) -> FCMResult:
        status = response.status_code

        if 200 <= status < 300:
            try:
                parsed = response.json()
            except json.JSONDecodeError:
                parsed = {}
            message_id = parsed.get("name") if isinstance(parsed, dict) else None
            return FCMResult(success=True, message_id=message_id)

        # Corps d'erreur FCM : {"error": {"code":..., "status":..., ...}}
        try:
            error_body = response.json()
        except json.JSONDecodeError:
            error_body = {}
        err = error_body.get("error", {}) if isinstance(error_body, dict) else {}
        fcm_status = str(err.get("status") or "")
        fcm_message = str(err.get("message") or response.text[:200] or "Erreur FCM")

        if status == 404 or fcm_status == "NOT_FOUND" or fcm_status == "UNREGISTERED":
            raise FCMUnregisteredError(fcm_message, token=token)

        if status == 400 or fcm_status == "INVALID_ARGUMENT":
            raise FCMInvalidArgumentError(fcm_message, token=token)

        if status == 429 or 500 <= status < 600:
            raise FCMUnavailableError(fcm_message, token=token, status_code=status)

        # Dernier garde-fou : traité comme unavailable.
        log.warning(
            "fcm.response.unexpected_status",
            status=status,
            body=response.text[:200],
        )
        raise FCMUnavailableError(fcm_message, token=token, status_code=status)


# ── Chargement du service account JSON ──────────────────────────


def load_service_account_info() -> dict[str, Any] | None:
    """Lit le JSON du service account depuis `fcm_service_account_json`
    (contenu brut) ou `fcm_service_account_file` (chemin). Retourne None
    si aucun n'est défini.
    """
    raw = (settings.fcm_service_account_json or "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("FCM_SERVICE_ACCOUNT_JSON n'est pas un JSON valide.") from exc
        if not isinstance(data, dict):
            raise ValueError("FCM_SERVICE_ACCOUNT_JSON doit être un objet JSON.")
        return data

    file_path = (settings.fcm_service_account_file or "").strip()
    if file_path:
        path = Path(file_path)
        if not path.is_file():
            raise ValueError(f"FCM_SERVICE_ACCOUNT_FILE : fichier introuvable : {file_path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("FCM_SERVICE_ACCOUNT_FILE doit contenir un objet JSON.")
        return data

    return None
