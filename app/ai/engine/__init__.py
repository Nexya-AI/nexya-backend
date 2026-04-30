"""
NEXYA Couche IA — Package `engine`.

Contient les services qui orchestrent un turn chat de bout en bout :

- `SessionStore` (brique B3) : tampon Redis pour les appels LLM en cours/
  récents. Garantit qu'aucune ligne `ai_calls` n'est perdue si le
  StreamHandler crash entre la fin du stream et l'INSERT DB.

- `QueryEngine` (brique B4) : orchestrateur d'un turn chat consolidant
  `StreamHandler.stream()` + observation de l'outcome (parse `done.reason`,
  `error.code`, accumulation du contenu). Point d'entrée unique pour
  toute feature qui exécute un turn LLM (chat router, Planner, future
  feature Voix).
"""

from app.ai.engine.query_engine import (
    DONE_REASON_TO_STATUS,
    QueryEngine,
    StreamOutcome,
    observe_sse_event,
)
from app.ai.engine.session_store import SessionStore, get_session_store

__all__ = [
    "DONE_REASON_TO_STATUS",
    "QueryEngine",
    "SessionStore",
    "StreamOutcome",
    "get_session_store",
    "observe_sse_event",
]
