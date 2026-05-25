"""Tests LOT C (2026-05-23) — enrichissement live de `metadata_json.tool_calls`
sur `GET /chat/conversations/{id}/messages`.

7 scénarios mock-first (pattern aligné `tests/features/chat/`) couvrant
le helper `_build_messages_with_live_task_status` :

1. **Court-circuit aucun tool_call** — messages sans `metadata_json` ou sans
   `metadata_json.tool_calls` → retourne `MessageResponse.model_validate`
   direct, ZÉRO requête SQL supplémentaire (assertion `db.execute` non
   appelé).
2. **Happy path patch lifecycle** — task `status=idle` côté snapshot
   message + `status=completed` côté backend ORM → après enrichissement,
   le `metadata_json.tool_calls[0].data.task.status` est `'completed'`
   et `run_count`/`last_run_at` sont patchés.
3. **Task purgée RGPD physique** — `task_id` dans metadata mais absent du
   SELECT scheduled_tasks → status synthétique `'deleted'`.
4. **Task soft-deleted** — task trouvée mais `deleted_at != None` →
   status synthétique `'deleted'`.
5. **IDOR-safe cross-user** — task forgée appartenant à un autre user
   (absente du SELECT filtré par `user_id`) → status `'deleted'` (jamais
   leak du statut d'une task d'un autre user).
6. **Plusieurs tool_calls dans un même message** — 1 createTask + 1
   updateTask → les 2 sont patchés indépendamment.
7. **Pas de mutation du metadata_json ORM source** — la deep-copy garantit
   que `m.metadata_json` reste intact côté ORM (anti side-effect).

Pattern : `AsyncMock(spec=AsyncSession)` + `MagicMock(spec=Message)` +
`MagicMock(spec=ScheduledTask)` + `MagicMock(spec=User)`. Aucun Postgres
réel (tests sub-seconde).
"""

from __future__ import annotations

import copy
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.features.auth.models import User
from app.features.chat.models import Message
from app.features.chat.router import _build_messages_with_live_task_status
from app.features.planner.models import ScheduledTask

# ════════════════════════════════════════════════════════════════════
# Helpers
# ════════════════════════════════════════════════════════════════════


def _make_user(user_id: uuid.UUID | None = None) -> MagicMock:
    user = MagicMock(spec=User)
    user.id = user_id or uuid.uuid4()
    return user


def _make_message(
    *,
    metadata_json: dict | None = None,
    status: str = "completed",
) -> MagicMock:
    """Crée un mock `Message` ORM minimal compatible avec `MessageResponse
    .model_validate(from_attributes=True)` — peuple les 16 champs requis.
    """
    now = datetime.now(tz=UTC)
    m = MagicMock(spec=Message)
    m.id = uuid.uuid4()
    m.conversation_id = uuid.uuid4()
    m.role = "assistant"
    m.content = "Test content"
    m.status = status
    m.provider = "gemini"
    m.model = "gemini-2.5-flash"
    m.prompt_tokens = 100
    m.completion_tokens = 50
    m.total_tokens = 150
    m.cost_usd = None
    m.error_code = None
    m.finished_at = now
    m.created_at = now
    m.updated_at = now
    m.metadata_json = metadata_json
    return m


def _make_scheduled_task(
    *,
    task_id: uuid.UUID,
    status: str = "completed",
    paused: bool = False,
    next_run_at: datetime | None = None,
    last_run_at: datetime | None = None,
    run_count: int = 0,
    deleted_at: datetime | None = None,
) -> MagicMock:
    """Mock `ScheduledTask` ORM avec les 6 champs lifecycle utilisés par
    le helper."""
    t = MagicMock(spec=ScheduledTask)
    t.id = task_id
    t.status = status
    t.paused = paused
    t.next_run_at = next_run_at
    t.last_run_at = last_run_at
    t.run_count = run_count
    t.deleted_at = deleted_at
    return t


def _make_db_returning_tasks(tasks: list[MagicMock]) -> MagicMock:
    """`AsyncSession` mock qui retourne `tasks` au prochain `execute()`."""
    db = MagicMock(spec=AsyncSession)
    result = MagicMock()
    result.scalars.return_value.all.return_value = tasks
    db.execute = AsyncMock(return_value=result)
    return db


def _tool_call_payload(task_id: str, *, title: str = "T", initial_status: str = "idle") -> dict:
    """Payload minimal d'un tool_call `create_task` dans `metadata_json.tool_calls`."""
    return {
        "id": "call_test",
        "name": "create_task",
        "success": True,
        "data": {
            "task": {
                "id": task_id,
                "title": title,
                "status": initial_status,  # snapshot figé au moment du SSE
                "paused": False,
                "expert_id": "general",
                "next_run_at": "2026-05-23T08:00:00+00:00",
            },
        },
        "error": None,
    }


# ════════════════════════════════════════════════════════════════════
# Tests
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_returns_messages_unchanged_when_no_tool_calls():
    """Messages sans metadata_json ou sans tool_calls → court-circuit
    (zéro requête SQL supplémentaire)."""
    user = _make_user()
    msg_no_meta = _make_message(metadata_json=None)
    msg_empty_tool_calls = _make_message(metadata_json={"tool_calls": []})
    msg_no_tool_calls_key = _make_message(metadata_json={"foo": "bar"})

    db = MagicMock(spec=AsyncSession)
    db.execute = AsyncMock()

    result = await _build_messages_with_live_task_status(
        [msg_no_meta, msg_empty_tool_calls, msg_no_tool_calls_key], user, db
    )

    assert len(result) == 3
    # CRITICAL : pas de SQL exécuté quand aucune tâche à enrichir
    db.execute.assert_not_called()


@pytest.mark.asyncio
async def test_patches_lifecycle_fields_from_live_task():
    """Happy path : task `idle` côté metadata → backend renvoie `completed`
    + run_count=1 + last_run_at → tous les champs lifecycle sont patchés."""
    user = _make_user()
    task_uuid = uuid.uuid4()
    last_run = datetime.now(tz=UTC)
    msg = _make_message(metadata_json={"tool_calls": [_tool_call_payload(str(task_uuid))]})
    live_task = _make_scheduled_task(
        task_id=task_uuid,
        status="completed",
        paused=False,
        next_run_at=None,  # once exécutée → next null
        last_run_at=last_run,
        run_count=1,
    )

    db = _make_db_returning_tasks([live_task])
    result = await _build_messages_with_live_task_status([msg], user, db)

    assert len(result) == 1
    patched_task = result[0].metadata_json["tool_calls"][0]["data"]["task"]
    assert patched_task["status"] == "completed"
    assert patched_task["paused"] is False
    assert patched_task["next_run_at"] is None
    assert patched_task["last_run_at"] == last_run.isoformat()
    assert patched_task["run_count"] == 1


@pytest.mark.asyncio
async def test_marks_task_deleted_when_purged_from_db():
    """Task référencée dans metadata mais absente du SELECT scheduled_tasks
    (purge RGPD physique) → status='deleted' synthétique."""
    user = _make_user()
    ghost_uuid = uuid.uuid4()
    msg = _make_message(metadata_json={"tool_calls": [_tool_call_payload(str(ghost_uuid))]})

    db = _make_db_returning_tasks([])  # task purgée
    result = await _build_messages_with_live_task_status([msg], user, db)

    patched_task = result[0].metadata_json["tool_calls"][0]["data"]["task"]
    assert patched_task["status"] == "deleted"


@pytest.mark.asyncio
async def test_marks_task_deleted_when_soft_deleted():
    """Task trouvée mais `deleted_at != None` → status='deleted' également."""
    user = _make_user()
    task_uuid = uuid.uuid4()
    msg = _make_message(metadata_json={"tool_calls": [_tool_call_payload(str(task_uuid))]})
    soft_deleted_task = _make_scheduled_task(
        task_id=task_uuid,
        status="completed",
        deleted_at=datetime.now(tz=UTC),
    )

    db = _make_db_returning_tasks([soft_deleted_task])
    result = await _build_messages_with_live_task_status([msg], user, db)

    patched_task = result[0].metadata_json["tool_calls"][0]["data"]["task"]
    assert patched_task["status"] == "deleted"


@pytest.mark.asyncio
async def test_idor_safe_cross_user_task_marked_deleted():
    """Un user qui forge un task_id appartenant à un autre user dans son
    metadata_json (théoriquement impossible mais défense en profondeur) ne
    doit JAMAIS voir le statut réel de cette task. Le SELECT filtré par
    `user_id = current_user.id` la renvoie absente → status='deleted'."""
    user = _make_user()
    cross_user_task_uuid = uuid.uuid4()
    msg = _make_message(
        metadata_json={"tool_calls": [_tool_call_payload(str(cross_user_task_uuid))]}
    )

    # Le SELECT backend filtre par user_id → renvoie [] même si la task
    # existe ailleurs.
    db = _make_db_returning_tasks([])
    result = await _build_messages_with_live_task_status([msg], user, db)

    patched_task = result[0].metadata_json["tool_calls"][0]["data"]["task"]
    assert patched_task["status"] == "deleted"
    # Pas de leak des autres champs (paused/run_count/etc. inchangés depuis
    # le snapshot — seul status bascule à 'deleted').


@pytest.mark.asyncio
async def test_handles_multiple_tool_calls_in_same_message():
    """1 message avec 2 tool_calls (createTask + updateTask) → les 2
    sont patchés indépendamment depuis leurs statuts backend respectifs."""
    user = _make_user()
    task_a = uuid.uuid4()
    task_b = uuid.uuid4()
    msg = _make_message(
        metadata_json={
            "tool_calls": [
                _tool_call_payload(str(task_a), title="A", initial_status="idle"),
                _tool_call_payload(str(task_b), title="B", initial_status="idle"),
            ]
        }
    )
    live_a = _make_scheduled_task(task_id=task_a, status="completed", run_count=1)
    live_b = _make_scheduled_task(task_id=task_b, status="running")

    db = _make_db_returning_tasks([live_a, live_b])
    result = await _build_messages_with_live_task_status([msg], user, db)

    tool_calls = result[0].metadata_json["tool_calls"]
    assert len(tool_calls) == 2
    statuses = {tc["data"]["task"]["id"]: tc["data"]["task"]["status"] for tc in tool_calls}
    assert statuses[str(task_a)] == "completed"
    assert statuses[str(task_b)] == "running"


@pytest.mark.asyncio
async def test_does_not_mutate_original_metadata_json():
    """Garde-fou anti side-effect : la deep-copy doit garantir que
    `m.metadata_json` reste strictement identique à son état initial après
    l'enrichissement (pas de mutation accidentelle de l'ORM)."""
    user = _make_user()
    task_uuid = uuid.uuid4()
    original_metadata = {"tool_calls": [_tool_call_payload(str(task_uuid), initial_status="idle")]}
    snapshot_before = copy.deepcopy(original_metadata)
    msg = _make_message(metadata_json=original_metadata)
    live_task = _make_scheduled_task(task_id=task_uuid, status="completed", run_count=1)

    db = _make_db_returning_tasks([live_task])
    await _build_messages_with_live_task_status([msg], user, db)

    # L'ORM source DOIT rester intact (status='idle' figé au moment du SSE)
    assert original_metadata == snapshot_before
    assert original_metadata["tool_calls"][0]["data"]["task"]["status"] == "idle", (
        "Le metadata_json ORM source a été muté (anti-pattern deep-copy raté)"
    )
