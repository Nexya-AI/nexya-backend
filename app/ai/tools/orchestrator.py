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

from app.ai.providers import ChatChunk, ChatMessage, FinishReason, ToolResultDelta
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
    db_session_factory: Any,
    default_expert_id: str | None = None,
) -> ToolResult:
    """Exécute un tool_call accumulé via le registry.

    Parse les arguments JSON, invoque le handler, et catche les exceptions
    typées pour les remonter au LLM en format `ToolResult(success=False)`.

    `default_expert_id` — expert de la conversation courante. Injecté dans
    les arguments de `create_task` quand le LLM a laissé `expert_id` absent
    ou au défaut 'general' : un rappel créé depuis l'expert Cuisine doit
    s'exécuter sous Cuisine à l'heure dite ET sa carte doit prendre la
    couleur de l'expert. Le LLM ne renseigne presque jamais ce champ
    (c'est un défaut de schéma) — d'où l'injection côté orchestrateur.

    `db_session_factory` est un callable retournant un context manager de
    session DB async (typiquement `AsyncSessionLocal`). Une session
    **fraîche** est ouverte par tool exécuté : le stream `/chat/stream`
    tourne APRÈS le retour de l'endpoint, la session `Depends(get_db)` de
    la requête n'est plus fiable à ce moment-là. Le handler est responsable
    de son propre `commit` ; à la sortie du `async with`, la session est
    fermée (les changements non commités sont rollbackés).

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

            # Héritage de l'expert de la conversation pour `create_task`.
            # [Bug-couleur-carte 2026-05-23] Override **SYSTÉMATIQUE** quand
            # `default_expert_id` est un expert spécialisé (≠ general). Le
            # LLM Gemini Flash francophone passe parfois `"informatique"`/
            # `"cuisine"`/`"langues"` (variations linguistiques FR) au lieu
            # des slugs canoniques `computer`/`cooking`/`language`. La
            # condition `current_expert == "general"` précédente ne couvrait
            # PAS ces variations → DB stockait `expert_id="informatique"` →
            # Flutter `_resolveExpert()` ne matchait aucun `ExpertDomain` →
            # carte bleue par défaut. L'expert de la conv prime TOUJOURS sur
            # ce que le LLM tente de deviner (l'user pourra changer
            # manuellement via le Planner s'il veut). Pour `general` on
            # respecte le default_expert_id seulement si le LLM n'a rien
            # mis (compat conv générale qui pourrait laisser le LLM choisir).
            if tool_call.name == "create_task" and default_expert_id:
                if default_expert_id != "general":
                    # Expert spécialisé : override systématique, le LLM ne
                    # décide pas (anti variations linguistiques).
                    arguments["expert_id"] = default_expert_id
                else:
                    # Conv générale : seulement si LLM n'a rien passé.
                    current_expert = arguments.get("expert_id")
                    if not current_expert or current_expert == "general":
                        arguments["expert_id"] = default_expert_id

            try:
                async with db_session_factory() as db:
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
    db_session_factory: Any,
    max_rounds: int,
    default_expert_id: str | None = None,
) -> AsyncIterator[ChatChunk]:
    """Boucle de rounds : stream LLM → détecte TOOL_CALLS → exécute tools →
    ré-injecte → re-stream.

    Si le round se termine sans tool_calls (STOP/LENGTH/ERROR normal),
    le loop s'arrête. Le caller reçoit tous les chunks émis par tous les
    rounds de manière continue, y compris les chunks `tool_call` qui
    peuvent servir à afficher une UI intermédiaire.

    Cap strict à `max_rounds` : au-delà, on s'arrête sans ré-injecter pour
    ne pas boucler ad vitam (ex: LLM buggé qui ne sait plus s'arrêter
    d'appeler un tool).

    **Anti-double-exécution** : si `stream_factory` lève (provider down)
    APRÈS qu'au moins un tool ait déjà été exécuté, l'exception n'est PAS
    propagée — sinon le `StreamHandler` basculerait sur le provider de
    fallback, qui recréerait un `run_with_tool_rounds` neuf re-streamant le
    round 0 et **ré-exécutant `create_task`** → tâche planifiée en double.
    Dans ce cas on termine proprement : le client garde le résultat du/des
    tool(s) déjà exécuté(s). Au round 0 (aucun tool exécuté), l'exception
    remonte normalement pour laisser jouer la chaîne de fallback.
    """
    if max_rounds < 1:
        max_rounds = 1

    # K1 — span OTel parent qui couvre toute la boucle de rounds.
    # Chaque `execute_tool_call` créera un span enfant `tools.execute`.
    tracer = get_tracer()
    rounds_executed = 0
    cap_reached = False
    tools_executed_total = 0
    with tracer.start_as_current_span(
        "tools.run", attributes={"tools.max_rounds": max_rounds}
    ) as span:
        try:
            messages = list(initial_messages)
            for round_idx in range(max_rounds):
                rounds_executed = round_idx + 1
                collected: list[ChatChunk] = []
                try:
                    async for chunk in stream_factory(messages):
                        collected.append(chunk)
                        yield chunk
                except Exception:  # noqa: BLE001 — voir docstring anti-double-exécution
                    # Round 0, aucun tool exécuté → on laisse remonter pour
                    # que le StreamHandler bascule sur le provider de fallback.
                    if tools_executed_total == 0:
                        raise
                    # Round ≥1, ≥1 tool déjà exécuté → on NE relance PAS
                    # (un fallback ré-exécuterait create_task → doublon).
                    log.warning(
                        "tools.orchestrator.stream_failed_after_tool_executed",
                        round_idx=round_idx,
                        tools_executed=tools_executed_total,
                    )
                    return

                round_result = collect_tool_calls_from_chunks(collected)
                if not round_result.finished_with_tool_calls:
                    return

                # Exécute chaque tool_call (1 session DB fraîche par tool).
                results: list[tuple[CollectedToolCall, ToolResult]] = []
                for tc in round_result.tool_calls:
                    tr = await execute_tool_call(
                        tc,
                        registry=registry,
                        user=user,
                        db_session_factory=db_session_factory,
                        default_expert_id=default_expert_id,
                    )
                    results.append((tc, tr))
                    tools_executed_total += 1
                    # [planner-from-chat LOT 6] — émet le résultat
                    # d'exécution comme ChatChunk dédié. `_run_link` le
                    # traduit en `event: tool_result` SSE → le frontend
                    # affiche la carte de tâche avec les VRAIES données
                    # backend (id, schedule, next_run_at), sans matching
                    # approximatif par titre + date de création.
                    yield ChatChunk(
                        tool_result=ToolResultDelta(
                            id=tc.id,
                            name=tc.name,
                            success=tr.success,
                            data=tr.data,
                            error=tr.error,
                        )
                    )
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
                span.set_attribute("tools.executed_total", tools_executed_total)
            except Exception:  # noqa: BLE001
                pass
