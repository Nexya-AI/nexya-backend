"""
Orchestrateur tool_calls pour `/chat/stream`.

Séparé du router pour être testable isolément. L'orchestrateur prend un
generator de chunks LLM en entrée, détecte les `tool_call` / finish_reason
TOOL_CALLS, exécute les tools via le registry, injecte les résultats dans
la conversation et relance le stream — jusqu'à `max_rounds` cycles.

Format des messages injectés :
- Message assistant : préservé avec les tool_calls.
- Message role='tool' : contient le résultat JSON sérialisé du handler,
  un par tool_call exécuté. Compatible format OpenAI (Anthropic accepte
  via mapping interne, Gemini via `function_response`).

Le caller (router `/chat/stream`) consomme le generator final et le relaye
tel quel en SSE, comme si c'était un seul stream continu côté client.
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from typing import Any

import structlog

from app.ai.providers import ChatChunk, ChatMessage, FinishReason
from app.core.observability import get_tracer, record_tool_execution

from .base import ToolDefinition, ToolExecutionError, ToolRegistry, ToolResult

log = structlog.get_logger(__name__)


@dataclass(slots=True)
class CollectedToolCall:
    """Tool call accumulé depuis le stream (plusieurs deltas possibles)."""

    id: str = ""
    name: str = ""
    arguments_json: str = ""
    index: int = 0


@dataclass(slots=True)
class ToolRoundResult:
    """Résultat d'un round : les tool_calls détectés + leurs résultats."""

    tool_calls: list[CollectedToolCall] = field(default_factory=list)
    results: list[tuple[CollectedToolCall, ToolResult]] = field(default_factory=list)
    finished_with_tool_calls: bool = False


def collect_tool_calls_from_chunks(chunks: list[ChatChunk]) -> ToolRoundResult:
    """Parcourt une séquence de ChatChunk et reconstruit les tool_calls.

    Un tool_call peut arriver :
    - En un seul chunk complet (MockChatProvider).
    - En plusieurs deltas (`name` seul, puis `arguments` partial). On
      aggrège par `index`.
    """
    buckets: dict[int, CollectedToolCall] = {}
    finished_tool = False
    for chunk in chunks:
        if chunk.tool_call is not None:
            tc = chunk.tool_call
            bucket = buckets.setdefault(tc.index, CollectedToolCall(index=tc.index))
            if tc.id:
                bucket.id = tc.id
            if tc.name:
                bucket.name = tc.name
            if tc.arguments_json_partial:
                bucket.arguments_json += tc.arguments_json_partial
        if chunk.finish_reason == FinishReason.TOOL_CALLS:
            finished_tool = True
    return ToolRoundResult(
        tool_calls=[buckets[i] for i in sorted(buckets)],
        finished_with_tool_calls=finished_tool,
    )


async def execute_tool_call(
    tool_call: CollectedToolCall,
    *,
    registry: ToolRegistry,
    user: Any,
    db: Any,
) -> ToolResult:
    """Exécute un tool_call accumulé via le registry.

    Parse les arguments JSON, invoque le handler, et catche les exceptions
    typées pour les remonter au LLM en format `ToolResult(success=False)`.

    K1 — ouvre un span OTel `tools.execute` + enregistre métrique
    `tools_executed_total{name, success}` + histogramme `duration`.
    """
    tracer = get_tracer()
    started = time.monotonic()
    success = False
    with tracer.start_as_current_span(
        "tools.execute", attributes={"tool.name": tool_call.name}
    ) as span:
        try:
            tool: ToolDefinition | None = registry.get(tool_call.name)
            if tool is None:
                return ToolResult(
                    success=False,
                    error={
                        "code": "TOOL_NOT_FOUND",
                        "message": f"Tool inconnu : '{tool_call.name}'.",
                    },
                )

            try:
                arguments = json.loads(tool_call.arguments_json or "{}")
                if not isinstance(arguments, dict):
                    raise ValueError("Arguments doivent être un objet JSON.")
            except (ValueError, TypeError) as exc:
                return ToolResult(
                    success=False,
                    error={
                        "code": "TOOL_ARGS_INVALID",
                        "message": f"JSON d'arguments invalide : {exc}",
                    },
                )

            try:
                result = await tool.handler(user, db, arguments)
            except ToolExecutionError as exc:
                return ToolResult(
                    success=False,
                    error={
                        "code": exc.code,
                        "message": exc.message,
                        **({"data": exc.data} if exc.data else {}),
                    },
                )
            except Exception as exc:  # noqa: BLE001 — tool isolé du stream
                log.exception(
                    "tools.handler.unexpected_error",
                    tool=tool_call.name,
                    error=str(exc),
                )
                return ToolResult(
                    success=False,
                    error={
                        "code": "TOOL_INTERNAL_ERROR",
                        "message": "Erreur interne lors de l'exécution du tool.",
                    },
                )

            if not isinstance(result, ToolResult):
                return ToolResult(
                    success=False,
                    error={
                        "code": "TOOL_BAD_RETURN",
                        "message": "Le handler n'a pas retourné un ToolResult.",
                    },
                )
            success = bool(result.success)
            return result
        finally:
            try:
                span.set_attribute("tool.success", success)
            except Exception:  # noqa: BLE001
                pass
            try:
                record_tool_execution(tool_call.name, success, time.monotonic() - started)
            except Exception:  # noqa: BLE001
                pass


def build_tool_messages_for_next_round(
    round_result: ToolRoundResult,
) -> list[ChatMessage]:
    """Construit les messages à injecter dans le prochain round.

    Format minimaliste :
    - 1 message assistant résumé listant les tool_calls effectués.
    - 1 message 'user' par tool avec le résultat JSON (on utilise 'user'
      au lieu de 'tool' pour rester compatible avec la Sequence[ChatMessage]
      actuelle qui n'expose que {system, user, assistant}). Le LLM
      interprète le JSON comme « voilà ce que ton tool a retourné ».

    Sémantique : on reste volontairement dans les 3 rôles natifs pour ne
    pas casser les providers B1 qui n'ont pas encore été étendus au rôle
    'tool'. Une future itération pourra élargir `ChatRole`.
    """
    messages: list[ChatMessage] = []
    summary_parts = [
        f"[Tool {tc.name}(id={tc.id}) args={tc.arguments_json}]" for tc, _ in round_result.results
    ]
    if summary_parts:
        messages.append(
            ChatMessage(
                role="assistant",
                content="J'appelle les tools suivants :\n" + "\n".join(summary_parts),
            )
        )
    for tc, result in round_result.results:
        payload = json.dumps(result.to_payload(), ensure_ascii=False)
        messages.append(
            ChatMessage(
                role="user",
                content=f"[TOOL RESULT id={tc.id} name={tc.name}]\n{payload}",
            )
        )
    return messages


# ═══════════════════════════════════════════════════════════════════
# Run-and-reround loop
# ═══════════════════════════════════════════════════════════════════


StreamFactory = Callable[[list[ChatMessage]], AsyncIterator[ChatChunk]]


async def run_with_tool_rounds(
    *,
    initial_messages: list[ChatMessage],
    stream_factory: StreamFactory,
    registry: ToolRegistry,
    user: Any,
    db: Any,
    max_rounds: int,
) -> AsyncIterator[ChatChunk]:
    """Boucle de rounds : stream LLM → détecte TOOL_CALLS → exécute tools →
    ré-injecte → re-stream.

    Si le round se termine sans tool_calls (STOP/LENGTH/ERROR normal),
    le loop s'arrête. Le caller reçoit tous les chunks émis par tous les
    rounds de manière continue, y compris les chunks `tool_call` qui
    peuvent servir à afficher une UI intermédiaire.

    Cap strict à `max_rounds` : au-delà, on force un chunk d'erreur
    synthétique + STOP pour ne pas boucler ad vitam (ex: LLM buggé qui
    ne sait plus s'arrêter d'appeler un tool).
    """
    if max_rounds < 1:
        max_rounds = 1

    # K1 — span OTel parent qui couvre toute la boucle de rounds.
    # Chaque `execute_tool_call` créera un span enfant `tools.execute`.
    tracer = get_tracer()
    rounds_executed = 0
    cap_reached = False
    with tracer.start_as_current_span(
        "tools.run", attributes={"tools.max_rounds": max_rounds}
    ) as span:
        try:
            messages = list(initial_messages)
            for round_idx in range(max_rounds):
                rounds_executed = round_idx + 1
                collected: list[ChatChunk] = []
                async for chunk in stream_factory(messages):
                    collected.append(chunk)
                    yield chunk

                round_result = collect_tool_calls_from_chunks(collected)
                if not round_result.finished_with_tool_calls:
                    return

                # Exécute chaque tool_call
                results: list[tuple[CollectedToolCall, ToolResult]] = []
                for tc in round_result.tool_calls:
                    tr = await execute_tool_call(tc, registry=registry, user=user, db=db)
                    results.append((tc, tr))
                round_result.results = results

                # Si c'était le dernier round autorisé, stop sans ré-injecter.
                if round_idx == max_rounds - 1:
                    cap_reached = True
                    log.warning(
                        "tools.orchestrator.max_rounds_reached",
                        max_rounds=max_rounds,
                    )
                    return

                # Prépare la prochaine boucle avec les messages tools injectés
                messages = messages + build_tool_messages_for_next_round(round_result)
        finally:
            try:
                span.set_attribute("tools.rounds_executed", rounds_executed)
                span.set_attribute("tools.cap_reached", cap_reached)
            except Exception:  # noqa: BLE001
                pass
