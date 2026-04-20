"""
NEXYA Couche IA — ModerationService (filet de sécurité contenu).

Rôle : vérifier qu'un texte (input utilisateur ou output modèle) ne
contient pas de contenu illégal / nuisible selon les catégories standard
(violence, sexuel, haine, self-harm, exploitation mineurs, etc.).

Implémentation : appelle l'endpoint OpenAI `/v1/moderations` — gratuit,
rapide (<200ms), multilingue, supporte le français et les langues
africaines. Ce n'est PAS une fuite vers OpenAI puisque le contenu
ne produit pas de réponse générative : juste un classifieur.

Politique d'erreur : **fail-open** sur panne / timeout / absence de clé.

Raisonnement : bloquer tout NEXYA parce que l'API OpenAI Moderation
est down serait un trade-off inacceptable (perte totale de service
pour un simple filet de sécurité). On préfère laisser passer avec un
log d'alerte — la modération post-output du modèle principal
(safety Gemini) reste active en défense en profondeur.

Fail-CLOSED uniquement si l'API renvoie un verdict explicite "flagged".

Désactivation propre : si `settings.openai_api_key` est vide, le service
entre en mode "disabled" — il loggue UNE seule fois et retourne
allowed=True à chaque appel. Ça permet d'exécuter le backend en local
sans clé OpenAI.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Literal

import httpx
import structlog

from app.config import settings

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# CONSTANTES
# ═══════════════════════════════════════════════════════════════════

_OPENAI_MODERATION_URL = "https://api.openai.com/v1/moderations"
_OPENAI_MODERATION_MODEL = "omni-moderation-latest"
_DEFAULT_TIMEOUT_SECONDS = 3.0

ModerationKind = Literal["input", "output"]


# ═══════════════════════════════════════════════════════════════════
# TYPES
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ModerationDecision:
    """Verdict rendu par le service.

    `allowed=True` → le caller peut poursuivre.
    `allowed=False` → le caller DOIT bloquer et remonter l'erreur
    à l'utilisateur (message générique, jamais exposer les catégories
    brutes — ça permettrait du reverse-engineering du filtre).

    `flagged_categories` est remonté pour les logs / métriques ; ne pas
    l'envoyer au frontend tel quel.
    """

    allowed: bool
    flagged_categories: tuple[str, ...] = ()
    reason: str = ""
    raw: dict[str, Any] | None = field(default=None, repr=False)


# ═══════════════════════════════════════════════════════════════════
# SERVICE
# ═══════════════════════════════════════════════════════════════════


class ModerationService:
    """Classifieur de contenu. Thread-safe, utilisable comme singleton.

    Construit avec ou sans clé API — en absence de clé, passe en mode
    "disabled" (tout est allowed, warning loggué une fois).
    """

    def __init__(
        self,
        *,
        api_key: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        self._api_key = (api_key if api_key is not None else settings.openai_api_key) or ""
        self._timeout = timeout_seconds
        self._client: httpx.AsyncClient | None = None
        self._client_lock = asyncio.Lock()
        self._disabled_warning_emitted = False

        if not self._api_key:
            log.warning(
                "ai.moderation.disabled",
                reason="openai_api_key vide — mode disabled, tout contenu autorisé.",
            )
            self._disabled_warning_emitted = True

    @property
    def enabled(self) -> bool:
        return bool(self._api_key)

    async def check(
        self,
        text: str,
        *,
        kind: ModerationKind = "input",
        user_id: str | None = None,
        trace_id: str | None = None,
    ) -> ModerationDecision:
        """Vérifie un texte. Retourne une décision SANS JAMAIS lever.

        Contrats :
        - Texte vide → allowed=True (rien à modérer).
        - Service disabled → allowed=True.
        - Erreur réseau / timeout / 5xx → allowed=True (fail-open) + log ERROR.
        - Réponse 200 sans flag → allowed=True.
        - Réponse 200 avec flag → allowed=False, catégories remontées dans logs.
        - Réponse 4xx OpenAI (clé invalide, payload trop gros) → allowed=True
          (fail-open) + log ERROR — le problème est côté config, pas utilisateur.
        """
        if not text or not text.strip():
            return ModerationDecision(allowed=True, reason="empty_text")

        if not self.enabled:
            return ModerationDecision(allowed=True, reason="moderation_disabled")

        try:
            payload = await self._call_openai(text)
        except (httpx.TimeoutException, httpx.HTTPError) as exc:
            log.error(
                "ai.moderation.transport_error",
                kind=kind,
                user_id=user_id,
                trace_id=trace_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return ModerationDecision(allowed=True, reason="transport_error")
        except Exception as exc:  # noqa: BLE001 — fail-open safety net
            log.error(
                "ai.moderation.unexpected_error",
                kind=kind,
                user_id=user_id,
                trace_id=trace_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return ModerationDecision(allowed=True, reason="unexpected_error")

        return self._interpret(payload, kind=kind, user_id=user_id, trace_id=trace_id)

    async def close(self) -> None:
        """Ferme le client HTTP. À appeler dans le lifespan FastAPI."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ─── Internes ────────────────────────────────────────────────────

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            async with self._client_lock:
                if self._client is None:
                    self._client = httpx.AsyncClient(
                        timeout=self._timeout,
                        headers={
                            "Authorization": f"Bearer {self._api_key}",
                            "Content-Type": "application/json",
                        },
                    )
        return self._client

    async def _call_openai(self, text: str) -> dict[str, Any]:
        client = await self._get_client()
        response = await client.post(
            _OPENAI_MODERATION_URL,
            json={
                "model": _OPENAI_MODERATION_MODEL,
                "input": text,
            },
        )
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise httpx.HTTPError("Moderation response not a JSON object")
        return data

    def _interpret(
        self,
        payload: dict[str, Any],
        *,
        kind: ModerationKind,
        user_id: str | None,
        trace_id: str | None,
    ) -> ModerationDecision:
        results = payload.get("results")
        if not isinstance(results, list) or not results:
            log.warning(
                "ai.moderation.malformed_response",
                kind=kind,
                trace_id=trace_id,
                payload_keys=list(payload.keys()),
            )
            return ModerationDecision(allowed=True, reason="malformed_response", raw=payload)

        first = results[0] if isinstance(results[0], dict) else {}
        flagged = bool(first.get("flagged", False))
        categories = first.get("categories") or {}

        active: tuple[str, ...] = tuple(
            sorted(k for k, v in categories.items() if isinstance(v, bool) and v)
        )

        if flagged:
            log.info(
                "ai.moderation.flagged",
                kind=kind,
                user_id=user_id,
                trace_id=trace_id,
                categories=list(active),
            )
            return ModerationDecision(
                allowed=False,
                flagged_categories=active,
                reason="flagged",
                raw=payload,
            )

        return ModerationDecision(allowed=True, reason="clean", raw=payload)


# ═══════════════════════════════════════════════════════════════════
# SINGLETON — accessible par tous les services
# ═══════════════════════════════════════════════════════════════════

_service: ModerationService | None = None


def get_moderation_service() -> ModerationService:
    """Retourne l'instance partagée. Construite au premier appel.

    Utilise le singleton plutôt que `Depends(...)` car la modération peut
    être invoquée depuis des services de bas niveau (LlmRouter, StreamHandler)
    qui ne sont pas des endpoints FastAPI.
    """
    global _service
    if _service is None:
        _service = ModerationService()
    return _service


async def close_moderation_service() -> None:
    """À câbler dans le lifespan `app/main.py`."""
    global _service
    if _service is not None:
        await _service.close()
        _service = None
