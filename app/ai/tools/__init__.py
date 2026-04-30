"""
NEXYA Couche IA — Tools LLM (function calling).

Cadre permettant au LLM d'invoquer des fonctions côté serveur depuis
`/chat/stream`. Chaque tool est déclaré via `ToolDefinition` et enregistré
dans un `ToolRegistry` singleton au boot de l'app (voir `registry_init.py`).

Utilisation côté `/chat/stream` :

1. Router expose `ChatCompletionRequest.tools = registry.build_openai_tools()`
   au provider.
2. Le LLM répond avec `finish_reason=TOOL_CALLS` et un `ToolCallDelta`.
3. Le router exécute le tool via `ToolRegistry.get(name).handler(user, db, args)`.
4. Le résultat est ré-injecté comme nouveau message role='tool' et le
   stream reprend (max `chat_max_tool_rounds` cycles).
"""

from __future__ import annotations

from .base import (
    ToolDefinition,
    ToolExecutionError,
    ToolRegistry,
    ToolResult,
    get_tool_registry,
    reset_tool_registry_for_tests,
)
from .orchestrator import (
    CollectedToolCall,
    ToolRoundResult,
    build_tool_messages_for_next_round,
    collect_tool_calls_from_chunks,
    execute_tool_call,
    run_with_tool_rounds,
)

__all__ = [
    "ToolDefinition",
    "ToolRegistry",
    "ToolResult",
    "ToolExecutionError",
    "get_tool_registry",
    "reset_tool_registry_for_tests",
    "CollectedToolCall",
    "ToolRoundResult",
    "collect_tool_calls_from_chunks",
    "execute_tool_call",
    "build_tool_messages_for_next_round",
    "run_with_tool_rounds",
]
