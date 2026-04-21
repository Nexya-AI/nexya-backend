"""
Singletons de la Couche IA — LlmRouter + StreamHandler.

Ce module existe pour deux raisons :

1. **Casser la dépendance circulaire**. Avant, les singletons vivaient dans
   `app/main.py` et étaient importés par `app/features/chat/router.py` pour
   servir `/chat/stream`. Mais `main.py` importe lui-même `chat_router`,
   d'où une boucle d'imports qui casserait au moindre refactor.

2. **Point unique de vérité**. Tout code qui a besoin du router IA ou du
   handler SSE passe par `get_ai_router()` / `get_stream_handler()`. Un
   seul endroit à mocker dans les tests, un seul endroit à modifier si la
   factory change (ex. brancher un retry layer custom par environnement).

Les singletons sont construits au premier appel (lazy) et réutilisés ensuite.
Le lifespan FastAPI les force à se construire tôt au démarrage pour que les
logs d'initialisation apparaissent dans les premiers ms de l'app.
"""

from __future__ import annotations

from app.ai.router import LlmRouter, build_default_router
from app.ai.streaming import StreamHandler

_AI_ROUTER: LlmRouter | None = None
_STREAM_HANDLER: StreamHandler | None = None


def get_ai_router() -> LlmRouter:
    """Retourne le singleton LlmRouter (câblé par `build_default_router`).

    Construit au premier appel — tous les appels suivants renvoient la
    même instance. Thread-safe uniquement sous asyncio (pas de locking
    manuel, acceptable puisque FastAPI exécute sur une boucle unique).
    """
    global _AI_ROUTER
    if _AI_ROUTER is None:
        _AI_ROUTER = build_default_router()
    return _AI_ROUTER


def get_stream_handler() -> StreamHandler:
    """Retourne le singleton StreamHandler (router + retry + breakers)."""
    global _STREAM_HANDLER
    if _STREAM_HANDLER is None:
        _STREAM_HANDLER = StreamHandler(router=get_ai_router())
    return _STREAM_HANDLER


def reset_runtime_for_tests() -> None:
    """Réinitialise les singletons — réservé aux tests.

    Permet à un test de substituer un router en mock sans polluer la
    session suivante. À NE PAS appeler depuis le code applicatif."""
    global _AI_ROUTER, _STREAM_HANDLER
    _AI_ROUTER = None
    _STREAM_HANDLER = None
