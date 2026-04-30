"""
Tools LLM pour le Planner — create_task, list_tasks, update_task, pause_task.

Chaque handler délègue au `TaskSchedulerService` (F1) après avoir parsé les
arguments JSON fournis par le LLM. Les erreurs métier typées
(`TasksQuotaExceededException`, `TaskScheduleInvalidException`,
`ResourceNotFoundException`, `ValidationException`) sont traduites en
`ToolResult(success=False, error={code, message})` pour que le LLM puisse
l'expliquer à l'user plutôt que de crasher le stream.

Le handler prend toujours (user, db, arguments) — la signature est
strictement contrainte par l'ABC `ToolHandler` dans `base.py`.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from pydantic import ValidationError

from app.core.errors.exceptions import (
    NexYaException,
    ResourceNotFoundException,
    TaskScheduleInvalidException,
    TasksQuotaExceededException,
    ValidationException,
)
from app.features.planner.schemas import (
    TaskCreate,
    TaskUpdate,
)
from app.features.planner.service import TaskSchedulerService

from .base import ToolDefinition, ToolRegistry, ToolResult

log = structlog.get_logger(__name__)


# ══════════════════════════════════════════════════════════════
# Schémas JSON Schema des tools
# ══════════════════════════════════════════════════════════════

_SCHEDULE_CONFIG_SCHEMA = {
    "type": "object",
    "description": (
        "Configuration du schedule. 4 types supportés (champ `type` obligatoire) : "
        "'once' avec `at` (ISO 8601 UTC future), "
        "'interval_minutes' avec `minutes` (>=5), "
        "'daily' avec `hour` (0-23) et `minute` (0-59), "
        "'weekly' avec `weekday` (0=lundi..6=dimanche), `hour`, `minute`."
    ),
    "properties": {
        "type": {
            "type": "string",
            "enum": ["once", "interval_minutes", "daily", "weekly"],
        },
        "at": {"type": "string", "format": "date-time"},
        "minutes": {"type": "integer", "minimum": 5},
        "hour": {"type": "integer", "minimum": 0, "maximum": 23},
        "minute": {"type": "integer", "minimum": 0, "maximum": 59},
        "weekday": {"type": "integer", "minimum": 0, "maximum": 6},
    },
    "required": ["type"],
}


_CREATE_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": 200,
            "description": "Titre court de la tâche (<=200 chars).",
        },
        "prompt": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4000,
            "description": "Prompt complet que l'IA exécutera au moment du schedule.",
        },
        "expert_id": {
            "type": "string",
            "description": "Expert NEXYA à utiliser (défaut 'general').",
            "default": "general",
        },
        "schedule": _SCHEDULE_CONFIG_SCHEMA,
        "timezone": {"type": "string", "default": "UTC"},
        "auto_delete_after_run": {"type": "boolean", "default": False},
    },
    "required": ["title", "prompt", "schedule"],
    "additionalProperties": False,
}


_LIST_TASKS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "status": {
            "type": "string",
            "enum": ["idle", "pending", "running", "completed", "failed", "paused"],
        },
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 20},
    },
    "additionalProperties": False,
}


_UPDATE_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "format": "uuid"},
        "title": {"type": "string", "minLength": 1, "maxLength": 200},
        "prompt": {"type": "string", "minLength": 1, "maxLength": 4000},
        "schedule": _SCHEDULE_CONFIG_SCHEMA,
    },
    "required": ["task_id"],
    "additionalProperties": False,
}


_PAUSE_TASK_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_id": {"type": "string", "format": "uuid"},
    },
    "required": ["task_id"],
    "additionalProperties": False,
}


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════


def _nexya_error_to_tool_error(exc: NexYaException) -> dict[str, Any]:
    """Traduit une NexYaException en payload error pour le LLM."""
    return {
        "code": getattr(exc, "code", "ERROR"),
        "message": getattr(exc, "message", None) or str(exc),
    }


def _parse_uuid(raw: Any, *, field: str) -> uuid.UUID:
    try:
        return uuid.UUID(str(raw))
    except (ValueError, TypeError) as exc:
        raise ValidationException(f"Argument '{field}' doit être un UUID valide.") from exc


def _serialize_task(task) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "title": task.title,
        "prompt": task.prompt,
        "expert_id": task.expert_id,
        "schedule_type": task.schedule_type,
        "schedule_config": task.schedule_config,
        "timezone": task.timezone,
        "next_run_at": task.next_run_at.isoformat() if task.next_run_at else None,
        "status": task.status,
        "active": task.active,
        "paused": task.paused,
    }


# ══════════════════════════════════════════════════════════════
# Handlers
# ══════════════════════════════════════════════════════════════


async def create_task_handler(user, db, arguments: dict[str, Any]) -> ToolResult:
    try:
        body = TaskCreate.model_validate(arguments)
    except ValidationError as exc:
        return ToolResult(
            success=False,
            error={
                "code": "VALIDATION_ERROR",
                "message": "Arguments invalides pour create_task.",
                "details": exc.errors()[:3],
            },
        )
    try:
        task = await TaskSchedulerService.create_task(user, body, db)
    except (
        TasksQuotaExceededException,
        TaskScheduleInvalidException,
        ValidationException,
    ) as exc:
        return ToolResult(success=False, error=_nexya_error_to_tool_error(exc))
    log.info(
        "tools.planner.create_task.ok",
        user_id=str(user.id),
        task_id=str(task.id),
    )
    return ToolResult(success=True, data={"task": _serialize_task(task)})


async def list_tasks_handler(user, db, arguments: dict[str, Any]) -> ToolResult:
    status = arguments.get("status")
    limit = arguments.get("limit", 20)
    try:
        page = await TaskSchedulerService.list_for_user(
            user, db, cursor=None, limit=limit, status=status
        )
    except ValidationException as exc:
        return ToolResult(success=False, error=_nexya_error_to_tool_error(exc))
    return ToolResult(
        success=True,
        data={
            "tasks": [_serialize_task(t) for t in page.items],
            "count": len(page.items),
        },
    )


async def update_task_handler(user, db, arguments: dict[str, Any]) -> ToolResult:
    task_id = _parse_uuid(arguments.get("task_id"), field="task_id")
    payload = {k: v for k, v in arguments.items() if k != "task_id"}
    try:
        body = TaskUpdate.model_validate(payload)
    except ValidationError as exc:
        return ToolResult(
            success=False,
            error={
                "code": "VALIDATION_ERROR",
                "message": "Arguments invalides pour update_task.",
                "details": exc.errors()[:3],
            },
        )
    try:
        task = await TaskSchedulerService.update_task(task_id, user, body, db)
    except (
        ResourceNotFoundException,
        TaskScheduleInvalidException,
        ValidationException,
    ) as exc:
        return ToolResult(success=False, error=_nexya_error_to_tool_error(exc))
    return ToolResult(success=True, data={"task": _serialize_task(task)})


async def pause_task_handler(user, db, arguments: dict[str, Any]) -> ToolResult:
    task_id = _parse_uuid(arguments.get("task_id"), field="task_id")
    try:
        task = await TaskSchedulerService.pause_task(task_id, user, db)
    except ResourceNotFoundException as exc:
        return ToolResult(success=False, error=_nexya_error_to_tool_error(exc))
    return ToolResult(success=True, data={"task": _serialize_task(task)})


# ══════════════════════════════════════════════════════════════
# Registration
# ══════════════════════════════════════════════════════════════


def build_planner_tools() -> list[ToolDefinition]:
    return [
        ToolDefinition(
            name="create_task",
            description=(
                "Crée une tâche IA planifiée pour l'utilisateur. Utilise-le "
                "quand l'user demande un rappel ou une action récurrente "
                "(ex: « rappelle-moi demain 18h »). Choisis le bon type de "
                "schedule parmi once / interval_minutes / daily / weekly."
            ),
            parameters_schema=_CREATE_TASK_SCHEMA,
            handler=create_task_handler,
        ),
        ToolDefinition(
            name="list_tasks",
            description=(
                "Liste les tâches planifiées de l'utilisateur. Utilise-le "
                "quand l'user demande à voir ses rappels."
            ),
            parameters_schema=_LIST_TASKS_SCHEMA,
            handler=list_tasks_handler,
        ),
        ToolDefinition(
            name="update_task",
            description=(
                "Modifie une tâche planifiée existante (titre, prompt ou "
                "schedule). Nécessite le task_id."
            ),
            parameters_schema=_UPDATE_TASK_SCHEMA,
            handler=update_task_handler,
        ),
        ToolDefinition(
            name="pause_task",
            description=(
                "Met en pause une tâche planifiée (arrête les exécutions "
                "futures sans la supprimer). Nécessite le task_id."
            ),
            parameters_schema=_PAUSE_TASK_SCHEMA,
            handler=pause_task_handler,
        ),
    ]


def register_planner_tools(registry: ToolRegistry | None = None) -> None:
    """Enregistre les 4 tools Planner dans le registry singleton.

    Appelé depuis le lifespan `main.py` au démarrage de l'app.
    """
    from .base import get_tool_registry  # noqa: PLC0415

    reg = registry or get_tool_registry()
    for tool in build_planner_tools():
        reg.register(tool)
    log.info("tools.planner.registered", count=len(reg.all()))
