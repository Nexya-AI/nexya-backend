"""
NEXYA Couche IA — QueryEngine consolidé (brique B4).

Ce module regroupe la logique transversale d'un **turn chat complet** qui
était jusqu'ici disséminée entre `app/features/chat/router.py` (observation
des événements SSE, accumulation du contenu, mapping `done.reason → status`)
et `app/ai/streaming.py` (génération du flux). Il ne remplace pas le
`StreamHandler` — il l'orchestre pour des callers qui veulent un seul point
d'entrée « donne-moi le stream + dis-moi comment ça s'est passé à la fin ».

Pourquoi cette consolidation :

1. **Réutilisable hors du router chat**. Le Planner (worker arq qui rejoue
   une conversation la nuit) et la future feature Voix (transcription →
   assistant → TTS) ont besoin d'exécuter un turn complet sans dupliquer
   le parsing SSE. Un seul endroit pour la sémantique `done.reason`.

2. **Testable isolément**. `observe_sse_event` est une fonction pure :
   ligne SSE + accumulateur → mutation. Pas de dépendance FastAPI / DB.

3. **Chat router allégé**. Il ne contient plus les constantes de mapping
   SSE → SQL et la dataclass accumulateur — juste l'orchestration HTTP +
   la finalisation DB spécifique à la table `messages`.

Ce que ce module NE fait PAS :

- **Pas de finalisation DB**. `messages.status = 'completed'` est un concept
  chat-spécifique. Le caller chat garde sa propre logique dans
  `_finalize_in_fresh_session`. Le Planner aura la sienne pour sa table
  `planner_runs`.
- **Pas de cache**. Le prompt cache reste dans le router chat, c'est une
  optimisation de l'endpoint legacy stateless qui n'a pas sa place au
  niveau QueryEngine.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog
from fastapi import Request

if TYPE_CHECKING:
    from app.ai.streaming import StreamContext, StreamHandler

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# MAPPING done.reason → Message.status (aligné CHECK SQL)
# ═══════════════════════════════════════════════════════════════════

# Un événement SSE `done` porte un `reason` parmi {`stop`, `cancelled`,
# `error`}. Ce mapping détermine la valeur de `Message.status` écrite en
# finalisation. Les valeurs cibles sont alignées 1:1 sur le CHECK SQL de la
# colonne `messages.status` — toute évolution du CHECK doit être répercutée
# ici ET dans `ai_calls.outcome` (qui utilise la même sémantique pour le
# tracking de coût).
DONE_REASON_TO_STATUS: dict[str, str] = {
    "stop": "completed",
    "cancelled": "cancelled",
    "error": "failed",
}


# ═══════════════════════════════════════════════════════════════════
# ACCUMULATEUR D'OUTCOME
# ═══════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class StreamOutcome:
    """Accumulateur mutable rempli au fil des événements SSE.

    - `done_reason` : dernière raison vue dans un `event: done`. Si aucun
      `done` n'est observé (disconnect client avant le dernier event), on
      retombe sur le défaut `'error'` → status `failed`. C'est le bon
      choix : un stream interrompu sans raison explicite est un échec, pas
      un succès silencieux.

    - `error_code` : dernier code d'erreur vu dans un `event: error`.
      Copié tel quel en tant qu'`error_code` du message ou du log
      d'appel IA (ex. `LLM_UNAVAILABLE`, `STREAM_CANCELLED`,
      `CONTENT_FILTERED`).

    - `content_parts` : liste des deltas texte des `event: chunk`.
      Concaténée en `content` final par le caller. On stocke en liste
      plutôt qu'en `str` cumulé pour éviter des `O(N²)` sur les
      concaténations successives de longs streams.

    - `tool_results` : payloads bruts des `event: tool_result`
      (planner-from-chat). Chaque entrée `{id, name, success, data, error}`
      = résultat d'exécution serveur d'un tool. Le caller chat les
      persiste dans `messages.metadata_json` pour que la carte de tâche
      survive à la réouverture de la conversation. Vide pour les streams
      sans tool (cas nominal du chat texte).
    """

    done_reason: str = "error"
    error_code: str | None = None
    content_parts: list[str] = field(default_factory=list)
    tool_results: list[dict] = field(default_factory=list)

    def final_content(self) -> str:
        """Joint les deltas en une unique string — à appeler après le stream."""
        return "".join(self.content_parts)

    def final_status(self) -> str:
        """Retourne le status SQL final (`completed`/`cancelled`/`failed`)."""
        return DONE_REASON_TO_STATUS.get(self.done_reason, "failed")


# ═══════════════════════════════════════════════════════════════════
# PARSING SSE
# ═══════════════════════════════════════════════════════════════════


def observe_sse_event(event: str, outcome: StreamOutcome) -> None:
    """Parse un événement SSE et met à jour l'accumulateur `outcome`.

    Format SSE (cf. `app.ai.streaming._sse`) :

        event: <type>
        data: <json>
        <ligne vide>

    Contrat :

    - Commentaires (`:` en tête) → ignorés silencieusement (c'est le cas
      des keepalives toutes les 15 s, on ne veut pas qu'ils polluent
      `content_parts` ni les logs).
    - `event: chunk` avec `{delta: str, ...}` → append `delta` à
      `content_parts`.
    - `event: done` avec `{reason: str, ...}` → `outcome.done_reason = reason`.
    - `event: error` avec `{code: str, ...}` → `outcome.error_code = code`.
    - Événement malformé (JSON cassé, absence de `data:`, etc.) → log
      warning et skip. On préfère perdre un fragment de trace plutôt que
      faire crasher toute la finalisation — une ligne SSE corrompue ne
      doit jamais faire perdre un placeholder `streaming` en DB.

    Fonction **pure** sur l'entrée (aucun I/O), **impure** par mutation
    de `outcome`. Testable isolément sans fixture réseau.
    """
    if event.startswith(":"):
        return

    event_type: str | None = None
    data_str: str | None = None
    for line in event.split("\n"):
        if line.startswith("event: "):
            event_type = line[len("event: ") :].strip()
        elif line.startswith("data: "):
            data_str = line[len("data: ") :]

    if event_type is None or data_str is None:
        return

    try:
        payload = json.loads(data_str)
    except (ValueError, TypeError):
        log.warning("query_engine.sse_parse_failed", raw=event[:120])
        return

    if event_type == "chunk":
        delta = payload.get("delta")
        if isinstance(delta, str):
            outcome.content_parts.append(delta)
    elif event_type == "done":
        reason = payload.get("reason")
        if isinstance(reason, str):
            outcome.done_reason = reason
    elif event_type == "error":
        code = payload.get("code")
        if isinstance(code, str):
            outcome.error_code = code
    elif event_type == "tool_result":
        # planner-from-chat — résultat d'exécution serveur d'un tool. On
        # accumule le payload entier `{id, name, success, data, error}` ;
        # le caller chat le persiste dans `messages.metadata_json`.
        if isinstance(payload, dict):
            outcome.tool_results.append(payload)


# ═══════════════════════════════════════════════════════════════════
# ORCHESTRATEUR DE TURN
# ═══════════════════════════════════════════════════════════════════


class QueryEngine:
    """Orchestre un turn chat = `StreamHandler.stream()` + observation outcome.

    Pattern d'usage typique :

        engine = QueryEngine(handler=get_stream_handler())
        outcome = StreamOutcome()
        async for event in engine.run(request, ctx, outcome=outcome):
            yield event
        # ici `outcome.final_status()` et `outcome.final_content()` sont prêts

    Le caller est responsable de :

    - construire le `StreamContext` (expert_id, messages, user_id, etc.),
    - persister le placeholder DB AVANT d'appeler `run()` (dans le mode
      persisté chat),
    - finaliser la DB APRÈS `run()` (dans son `finally`).

    Le QueryEngine ne gère ni la DB ni le cache — il orchestre
    uniquement la boucle SSE + l'observation, pour être réutilisable
    par des callers non-chat (Planner, Voix, batch).
    """

    def __init__(self, *, handler: StreamHandler) -> None:
        self._handler = handler

    async def run(
        self,
        request: Request,
        ctx: StreamContext,
        *,
        outcome: StreamOutcome,
    ) -> AsyncIterator[str]:
        """Stream les événements SSE en peuplant `outcome` au passage.

        Yield chaque event string tel quel — le caller le re-yield à son
        `StreamingResponse` FastAPI. Aucun événement n'est réécrit ni
        filtré (y compris les keepalives, qui sont transparents pour
        `observe_sse_event`).

        En cas d'exception ou de cancellation dans la boucle, le caller
        voit la même exception remonter — `outcome` reflète alors
        l'état partiel observé jusque-là (content_parts partiel,
        done_reason=error par défaut).
        """
        async for event in self._handler.stream(request, ctx):
            observe_sse_event(event, outcome)
            yield event
