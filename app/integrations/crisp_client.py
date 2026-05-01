"""
NEXYA Integration — Crisp HTTP client (Phase 18 / N4 volet B).

Wrapper async httpx sur l'API Crisp v1 (https://crisp.chat) pour pousser
des tickets de support automatiquement quand un user Pro rencontre un
incident critique (paiement, LLM down, RGPD, etc.).

Pattern aligné Brevo/hCaptcha/FCM/Vision/Voice/Embeddings :
- ABC `CrispClient` — contrat minimal `create_conversation`.
- `RealCrispClient` — POST `/v1/website/{id}/conversation` avec Basic Auth.
- `MockCrispClient` — accumule les calls pour tests + retourne fake_id.
- Factory `get_crisp_client()` mock-first auto si `CRISP_API_KEY` vide.

**Fail-safe absolu** côté `RealCrispClient.create_conversation` : sur
exception SDK / 401 / 5xx / timeout, retourne `None` (caller décide
quoi faire — `CrispEscalationService` log + INSERT row local sans
`crisp_conversation_id`). Le ticket est tracé localement même si
l'API Crisp est indisponible, un cron retry V2 pourra rejouer.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import httpx
import structlog

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class CrispConversationRequest:
    """Payload pour créer une conversation Crisp.

    `nickname` apparaît dans le panel Crisp — on met l'email user pour
    permettre à l'équipe support de retrouver le compte rapidement.
    `email` ouvre la possibilité d'un follow-up email automatique
    par Crisp si l'incident reste ouvert > 24h.
    """

    nickname: str
    email: str | None
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════
# ERREURS
# ═══════════════════════════════════════════════════════════════════


class CrispError(Exception):
    """Erreur générique côté client Crisp."""


class CrispAuthError(CrispError):
    """401/403 — clé API invalide ou expirée."""


class CrispUnavailableError(CrispError):
    """5xx, timeout, connection reset — Crisp temporairement indisponible."""


# ═══════════════════════════════════════════════════════════════════
# ABC
# ═══════════════════════════════════════════════════════════════════


class CrispClient(ABC):
    """Contrat minimal d'un client Crisp.

    `create_conversation` retourne le `session_id` Crisp (string opaque)
    en cas de succès, `None` en cas d'échec fail-safe (l'appelant ne
    doit JAMAIS recevoir d'exception côté `RealCrispClient`).
    """

    name: str = ""

    @abstractmethod
    async def create_conversation(self, request: CrispConversationRequest) -> str | None:
        """Crée une conversation Crisp. Fail-safe : retourne None sur erreur."""
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════════════
# REAL — httpx Crisp v1
# ═══════════════════════════════════════════════════════════════════


_CRISP_BASE_URL = "https://api.crisp.chat/v1"


class RealCrispClient(CrispClient):
    """Client Crisp réel via httpx async.

    Auth : Basic Auth `identifier:key` où `identifier` est le `plugin_id`
    (ou `user_id`) et `key` est la clé API. Crisp expose deux modes :
    - User Token (admin / agent) — `user_id:user_token`
    - Plugin Token (intégration) — `plugin_id:plugin_token`

    NEXYA utilise un Plugin Token dédié (plus restreint, moins de risque
    si la clé fuite — limite l'accès au website_id ciblé).

    Doc API : https://docs.crisp.chat/api/v1/
    """

    name: str = "crisp"

    def __init__(
        self,
        *,
        website_id: str,
        identifier: str,
        api_key: str,
        timeout_seconds: float = 5.0,
    ) -> None:
        if not website_id or not identifier or not api_key:
            raise ValueError("RealCrispClient exige website_id + identifier + api_key non vides.")
        self._website_id = website_id
        self._identifier = identifier
        self._api_key = api_key
        self._timeout = timeout_seconds

    async def create_conversation(self, request: CrispConversationRequest) -> str | None:
        """POST /v1/website/{id}/conversation puis attache l'initial message.

        Fail-safe absolu : aucune exception ne remonte au caller.
        """
        try:
            session_id = await self._create_session()
            if not session_id:
                return None
            await self._post_initial_message(session_id, request)
            await self._set_meta(session_id, request)
            log.info(
                "crisp.conversation.created",
                session_id=session_id,
                category=request.metadata.get("category"),
            )
            return session_id
        except CrispAuthError:
            log.warning("crisp.auth_error", website_id=self._website_id)
            return None
        except CrispUnavailableError as exc:
            log.warning("crisp.unavailable", error=str(exc))
            return None
        except Exception as exc:  # noqa: BLE001 — fail-safe absolu
            log.warning(
                "crisp.unexpected_error",
                error=str(exc),
                exc_type=type(exc).__name__,
            )
            return None

    async def _create_session(self) -> str | None:
        url = f"{_CRISP_BASE_URL}/website/{self._website_id}/conversation"
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                url,
                auth=(self._identifier, self._api_key),
                headers={"X-Crisp-Tier": "plugin"},
                json={},
            )
        return self._parse_session_id(response)

    async def _post_initial_message(
        self,
        session_id: str,
        request: CrispConversationRequest,
    ) -> None:
        url = f"{_CRISP_BASE_URL}/website/{self._website_id}/conversation/{session_id}/message"
        payload = {
            "type": "text",
            "from": "operator",
            "origin": "chat",
            "content": request.message,
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(
                url,
                auth=(self._identifier, self._api_key),
                headers={"X-Crisp-Tier": "plugin"},
                json=payload,
            )
        self._raise_for_status(response)

    async def _set_meta(
        self,
        session_id: str,
        request: CrispConversationRequest,
    ) -> None:
        """Pose nickname + email + segments dans la fiche conversation Crisp.

        Permet à l'équipe support de retrouver le compte user et de
        filtrer par segment (`payment_bug`, `pro_user`, etc.) directement
        dans le panel Crisp.
        """
        url = f"{_CRISP_BASE_URL}/website/{self._website_id}/conversation/{session_id}/meta"
        meta_payload: dict[str, Any] = {"nickname": request.nickname}
        if request.email:
            meta_payload["email"] = request.email
        # Segments dérivés de la metadata (catégorie + severity pour filtrage panel)
        segments: list[str] = []
        cat = request.metadata.get("category")
        sev = request.metadata.get("severity")
        if cat:
            segments.append(str(cat))
        if sev:
            segments.append(f"severity:{sev}")
        if segments:
            meta_payload["segments"] = segments
        if request.metadata:
            meta_payload["data"] = request.metadata

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            await client.patch(
                url,
                auth=(self._identifier, self._api_key),
                headers={"X-Crisp-Tier": "plugin"},
                json=meta_payload,
            )
        # Pas de raise sur meta — best-effort, le ticket est créé même si meta KO.

    def _parse_session_id(self, response: httpx.Response) -> str | None:
        self._raise_for_status(response)
        try:
            data = response.json()
        except ValueError:
            return None
        # Crisp v1 retourne `{"error": false, "reason": "added", "data": {"session_id": "..."}}`
        if isinstance(data, dict):
            payload = data.get("data") or {}
            if isinstance(payload, dict):
                sid = payload.get("session_id")
                if isinstance(sid, str) and sid:
                    return sid
        return None

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.status_code in (401, 403):
            raise CrispAuthError(f"Crisp auth failed: {response.status_code}")
        if response.status_code >= 500:
            raise CrispUnavailableError(f"Crisp 5xx: {response.status_code}")
        if response.status_code >= 400:
            # 400 = bad request → on log mais pas de raise (best-effort)
            log.warning(
                "crisp.client_error",
                status=response.status_code,
                body=response.text[:200],
            )


# ═══════════════════════════════════════════════════════════════════
# MOCK — accumule les appels pour tests + dev sans clé
# ═══════════════════════════════════════════════════════════════════


class MockCrispClient(CrispClient):
    """Client mock — log un fake conversation_id et accumule les calls.

    Permet :
    1. Dev local sans compte Crisp.
    2. Tests pytest sans réseau.
    3. CI sans secret CRISP_API_KEY.

    `force_fail=True` simule une indisponibilité (retourne None) pour
    tester le fail-safe côté caller (`CrispEscalationService`).
    """

    name: str = "crisp"  # même name que real — caller indistinguable

    def __init__(self, *, force_fail: bool = False) -> None:
        self.force_fail = force_fail
        self.calls: list[CrispConversationRequest] = []
        self._counter = 0

    async def create_conversation(self, request: CrispConversationRequest) -> str | None:
        self.calls.append(request)
        if self.force_fail:
            log.warning("crisp.mock.force_fail", category=request.metadata.get("category"))
            return None
        self._counter += 1
        fake_id = f"mock-session-{self._counter:06d}"
        log.info(
            "crisp.mock.conversation_created",
            session_id=fake_id,
            calls=len(self.calls),
        )
        return fake_id


# ═══════════════════════════════════════════════════════════════════
# FACTORY — singleton lazy
# ═══════════════════════════════════════════════════════════════════


_CLIENT: CrispClient | None = None


def get_crisp_client() -> CrispClient:
    """Retourne le singleton CrispClient.

    Mock-first auto si `crisp_api_key` ou `crisp_website_id` vide.
    Sinon RealCrispClient (clés présentes — Ivan a configuré L2).
    """
    global _CLIENT
    if _CLIENT is not None:
        return _CLIENT

    from app.config import settings

    if not settings.crisp_api_key or not settings.crisp_website_id:
        log.warning(
            "crisp.mock.enabled",
            reason="CRISP_API_KEY ou CRISP_WEBSITE_ID vide — mode mock activé.",
        )
        _CLIENT = MockCrispClient()
        return _CLIENT

    _CLIENT = RealCrispClient(
        website_id=settings.crisp_website_id,
        identifier=settings.crisp_identifier,
        api_key=settings.crisp_api_key,
    )
    log.info("crisp.real.enabled", website_id=settings.crisp_website_id)
    return _CLIENT


def reset_crisp_client_for_tests() -> None:
    """Réinitialise le singleton — réservé aux tests."""
    global _CLIENT
    _CLIENT = None
