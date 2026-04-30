"""
Contrat des tools LLM — `ToolDefinition` + `ToolRegistry`.

Un `ToolDefinition` décrit un tool exposé au LLM :
- `name` : identifiant unique (camelCase/snake_case autorisés).
- `description` : phrase courte expliquant au LLM QUAND appeler ce tool.
- `parameters_schema` : JSON Schema des arguments attendus.
- `handler` : coroutine `async def handler(user, db, arguments) -> dict`.

Le `ToolRegistry` est un singleton process-wide rempli au boot du lifespan
par `registry_init.register_planner_tools()`. Les endpoints `/chat/stream`
consomment `build_openai_tools()` pour construire le payload `tools` envoyé
au LLM (format OpenAI natif, compatible Anthropic via mapping interne).

`ToolResult` normalise la sortie :
- `success=True, data={...}`  → sérialisé et ré-injecté au LLM.
- `success=False, error={code, message}` → ré-injecté aussi, mais le LLM
  sait que l'appel a raté et peut expliquer à l'user.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Types
# ═══════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class ToolResult:
    """Résultat normalisé d'un handler de tool."""

    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: dict[str, Any] | None = None

    def to_payload(self) -> dict[str, Any]:
        if self.success:
            return {"success": True, "data": self.data}
        return {"success": False, "error": self.error or {}}


class ToolExecutionError(Exception):
    """Erreur typée levée par un handler de tool.

    Le caller (router `/chat/stream`) catche cette exception et construit
    un `ToolResult(success=False, error={code, message})` à injecter au
    LLM au tour suivant.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = data or {}


ToolHandler = Callable[[Any, Any, dict[str, Any]], Awaitable[ToolResult]]


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """Définition immutable d'un tool.

    `parameters_schema` doit être un dict JSON Schema (draft 2020-12 ou
    compatible). Les providers LLM (OpenAI, Anthropic, Gemini) acceptent
    tous le format `{"type": "object", "properties": {...}, "required": [...]}`.
    """

    name: str
    description: str
    parameters_schema: dict[str, Any]
    handler: ToolHandler

    def to_openai_tool(self) -> dict[str, Any]:
        """Format `tools` OpenAI Chat Completions (natif, also compatible
        avec Anthropic `tools` kwarg et Gemini `function_declarations`)."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters_schema,
            },
        }


# ═══════════════════════════════════════════════════════════════════
# Registry
# ═══════════════════════════════════════════════════════════════════


class ToolRegistry:
    """Catalogue en mémoire des tools disponibles."""

    def __init__(self) -> None:
        self._tools: dict[str, ToolDefinition] = {}

    def register(self, tool: ToolDefinition) -> None:
        if tool.name in self._tools:
            log.warning(
                "tools.registry.override",
                tool=tool.name,
            )
        self._tools[tool.name] = tool

    def get(self, name: str) -> ToolDefinition | None:
        return self._tools.get(name)

    def all(self) -> list[ToolDefinition]:
        return list(self._tools.values())

    def names(self) -> list[str]:
        return list(self._tools.keys())

    def build_openai_tools(self) -> list[dict[str, Any]]:
        return [t.to_openai_tool() for t in self._tools.values()]

    def clear(self) -> None:
        self._tools.clear()


# ═══════════════════════════════════════════════════════════════════
# Singleton
# ═══════════════════════════════════════════════════════════════════


_registry: ToolRegistry | None = None


def get_tool_registry() -> ToolRegistry:
    """Singleton process-wide. Peuplé au boot via `registry_init`."""
    global _registry
    if _registry is None:
        _registry = ToolRegistry()
    return _registry


def reset_tool_registry_for_tests() -> None:
    """Repart d'un registry vide. À utiliser en fixture test."""
    global _registry
    _registry = ToolRegistry()
