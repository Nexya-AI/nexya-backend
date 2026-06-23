"""Tests unitaires — worker `execute_scheduled_task` (F1)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.experts import get_expert_config
from app.ai.providers.base import ProviderUnavailableError
from app.core.errors.exceptions import RateLimitExceededException
from app.features.planner.models import ScheduledTask
from workers import scheduler_tasks

_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
_NOW = datetime.now(tz=UTC)


def _make_user() -> Any:
    user = MagicMock()
    user.id = _USER_ID
    user.is_pro = False
    return user


def _make_task(
    *,
    status: str = "idle",
    paused: bool = False,
    deleted: bool = False,
    schedule_type: str = "daily",
    schedule_config: dict | None = None,
    auto_delete_after_run: bool = False,
    retry_count: int = 0,
    max_retries: int = 2,
) -> ScheduledTask:
    task = ScheduledTask(
        user_id=_USER_ID,
        title="T",
        prompt="test",
        expert_id="general",
        schedule_type=schedule_type,
        schedule_config=schedule_config or {"hour": 9, "minute": 0},
        timezone="UTC",
        next_run_at=_NOW - timedelta(seconds=10),
        last_run_at=None,
        status=status,
        active=not deleted,
        paused=paused,
        auto_delete_after_run=auto_delete_after_run,
        retry_count=retry_count,
        max_retries=max_retries,
        run_count=0,
    )
    task.id = uuid.uuid4()
    task.created_at = _NOW
    task.updated_at = _NOW
    task.deleted_at = _NOW if deleted else None
    task.metadata_json = None
    return task


class _FakeDB:
    """AsyncSession fake — route selon stmt."""

    def __init__(self, *, task: ScheduledTask | None, user: Any | None = None) -> None:
        self._task = task
        self._user = user
        self.added: list[Any] = []
        self.executed_stmts: list[Any] = []
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    async def __aenter__(self) -> _FakeDB:
        return self

    async def __aexit__(self, *args) -> bool:
        return False

    async def get(self, model, key):
        if model is ScheduledTask:
            return self._task
        return None

    async def execute(self, stmt, *args, **kwargs):
        self.executed_stmts.append(stmt)
        sql = str(stmt).lower()
        result = MagicMock()
        if "from users" in sql:
            result.scalar_one_or_none.return_value = self._user
        else:
            result.scalar_one_or_none.return_value = None
        return result

    def add(self, obj):
        self.added.append(obj)


@pytest.fixture
def patch_db(monkeypatch: pytest.MonkeyPatch):
    def _patch(*, task: ScheduledTask | None, user: Any | None = None) -> _FakeDB:
        db = _FakeDB(task=task, user=user)

        def _factory():
            return db

        monkeypatch.setattr(scheduler_tasks, "AsyncSessionLocal", _factory)
        return db

    return _patch


# ══════════════════════════════════════════════════════════════
# 1. Task missing → skip
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_skip_on_task_missing(patch_db) -> None:
    patch_db(task=None)
    result = await scheduler_tasks.execute_scheduled_task({}, str(uuid.uuid4()))
    assert result == {"skipped": True, "reason": "missing"}


# ══════════════════════════════════════════════════════════════
# 2. Task deleted → skip
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_skip_on_deleted_task(patch_db) -> None:
    task = _make_task(deleted=True)
    patch_db(task=task)
    result = await scheduler_tasks.execute_scheduled_task({}, str(task.id))
    assert result == {"skipped": True, "reason": "deleted"}


# ══════════════════════════════════════════════════════════════
# 3. Task paused → skip
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_skip_on_paused(patch_db) -> None:
    task = _make_task(paused=True)
    patch_db(task=task)
    result = await scheduler_tasks.execute_scheduled_task({}, str(task.id))
    assert result == {"skipped": True, "reason": "paused"}


# ══════════════════════════════════════════════════════════════
# 4. Task status='running' → skip (re-livraison arq)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_skip_on_already_running(patch_db) -> None:
    task = _make_task(status="running")
    patch_db(task=task)
    result = await scheduler_tasks.execute_scheduled_task({}, str(task.id))
    assert result == {"skipped": True, "reason": "already_running"}


# ══════════════════════════════════════════════════════════════
# 5. User missing (RGPD hard-delete) → skip
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_skip_on_user_missing(patch_db) -> None:
    task = _make_task()
    patch_db(task=task, user=None)
    result = await scheduler_tasks.execute_scheduled_task({}, str(task.id))
    assert result == {"skipped": True, "reason": "user_missing"}


# ══════════════════════════════════════════════════════════════
# 6. Budget chat épuisé → status=skipped, reprogramme, pas de retry
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_budget_exhausted_skipped(patch_db, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _make_task()
    user = _make_user()
    db = patch_db(task=task, user=user)

    class _CappedBudget:
        async def check_and_consume_chat(self, uid, cost=1):
            raise RateLimitExceededException(reset_at=None)

    monkeypatch.setattr(scheduler_tasks, "get_budget_tracker", lambda: _CappedBudget())
    result = await scheduler_tasks.execute_scheduled_task({}, str(task.id))
    assert result["skipped"] is False
    assert result["status"] == "skipped"
    # Un result_row inséré avec status='skipped'.
    result_rows = [r for r in db.added if hasattr(r, "status")]
    assert any(r.status == "skipped" for r in result_rows)


# ══════════════════════════════════════════════════════════════
# 7. Happy path LLM → success INSERT result + recompute next_run_at
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_happy_path_llm_success(patch_db, monkeypatch: pytest.MonkeyPatch) -> None:
    task = _make_task()
    user = _make_user()
    db = patch_db(task=task, user=user)

    class _NoBudget:
        async def check_and_consume_chat(self, uid, cost=1):
            return 1

    monkeypatch.setattr(scheduler_tasks, "get_budget_tracker", lambda: _NoBudget())

    # Mock LLM router + provider stream.
    async def _fake_stream(req):
        yield MagicMock(delta="Bonjour Ivan !", usage=None)
        yield MagicMock(
            delta=None,
            usage=MagicMock(prompt_tokens=50, completion_tokens=100),
        )

    provider = MagicMock()
    provider.name = "gemini"
    provider.stream_chat = _fake_stream

    resolution = MagicMock()
    resolution.provider = provider
    resolution.model = "gemini-2.5-flash"
    # B1 — le worker lit `resolution.config` (ExpertConfig réel) pour
    # assembler la pile qualité. Un MagicMock nu casserait le join du
    # system_prompt (non-str). On fournit la vraie config "general".
    resolution.config = get_expert_config("general")

    router_mock = MagicMock()
    router_mock.resolve = MagicMock(return_value=resolution)
    monkeypatch.setattr(scheduler_tasks, "get_ai_router", lambda: router_mock)

    result = await scheduler_tasks.execute_scheduled_task({}, str(task.id))
    assert result["skipped"] is False
    assert result["status"] == "success"
    # Un result_row 'success' inséré.
    assert any(r.status == "success" for r in db.added if hasattr(r, "status"))


# ══════════════════════════════════════════════════════════════
# 8. Provider unavailable + retry_count < max → retry reschedule +5 min
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_provider_unavailable_triggers_retry(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _make_task(retry_count=0, max_retries=2)
    user = _make_user()
    db = patch_db(task=task, user=user)

    class _NoBudget:
        async def check_and_consume_chat(self, uid, cost=1):
            return 1

    monkeypatch.setattr(scheduler_tasks, "get_budget_tracker", lambda: _NoBudget())

    async def _failing_stream(req):
        raise ProviderUnavailableError("down", provider="gemini", model="gemini-2.5-flash")
        yield  # pragma: no cover

    provider = MagicMock()
    provider.name = "gemini"
    provider.stream_chat = _failing_stream
    resolution = MagicMock()
    resolution.provider = provider
    resolution.model = "gemini-2.5-flash"
    # B1 — le worker lit `resolution.config` (ExpertConfig réel) pour
    # assembler la pile qualité. Un MagicMock nu casserait le join du
    # system_prompt (non-str). On fournit la vraie config "general".
    resolution.config = get_expert_config("general")
    router_mock = MagicMock()
    router_mock.resolve = MagicMock(return_value=resolution)
    monkeypatch.setattr(scheduler_tasks, "get_ai_router", lambda: router_mock)

    result = await scheduler_tasks.execute_scheduled_task({}, str(task.id))
    assert result["status"] == "failed"
    # retry_count incrémenté + next_run_at reprogrammé.
    assert task.retry_count == 1
    assert task.status == "idle"
    assert task.next_run_at is not None


# ══════════════════════════════════════════════════════════════
# 9. Max retries atteint → status='failed', next_run_at=None
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_max_retries_reached_final_failure(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _make_task(retry_count=2, max_retries=2)
    user = _make_user()
    db = patch_db(task=task, user=user)

    class _NoBudget:
        async def check_and_consume_chat(self, uid, cost=1):
            return 1

    monkeypatch.setattr(scheduler_tasks, "get_budget_tracker", lambda: _NoBudget())

    async def _failing_stream(req):
        raise ProviderUnavailableError("down", provider="gemini", model="gemini-2.5-flash")
        yield  # pragma: no cover

    provider = MagicMock()
    provider.name = "gemini"
    provider.stream_chat = _failing_stream
    resolution = MagicMock()
    resolution.provider = provider
    resolution.model = "gemini-2.5-flash"
    # B1 — le worker lit `resolution.config` (ExpertConfig réel) pour
    # assembler la pile qualité. Un MagicMock nu casserait le join du
    # system_prompt (non-str). On fournit la vraie config "general".
    resolution.config = get_expert_config("general")
    router_mock = MagicMock()
    router_mock.resolve = MagicMock(return_value=resolution)
    monkeypatch.setattr(scheduler_tasks, "get_ai_router", lambda: router_mock)

    result = await scheduler_tasks.execute_scheduled_task({}, str(task.id))
    assert result["status"] == "failed"
    assert task.status == "failed"
    assert task.next_run_at is None


# ══════════════════════════════════════════════════════════════
# 10. Once + auto_delete_after_run → soft-delete après succès
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_once_auto_delete_after_success(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    future_at = (_NOW + timedelta(hours=1)).isoformat()
    task = _make_task(
        schedule_type="once",
        schedule_config={"at": future_at},
        auto_delete_after_run=True,
    )
    user = _make_user()
    db = patch_db(task=task, user=user)

    class _NoBudget:
        async def check_and_consume_chat(self, uid, cost=1):
            return 1

    monkeypatch.setattr(scheduler_tasks, "get_budget_tracker", lambda: _NoBudget())

    async def _fake_stream(req):
        yield MagicMock(delta="ok", usage=None)

    provider = MagicMock()
    provider.name = "gemini"
    provider.stream_chat = _fake_stream
    resolution = MagicMock()
    resolution.provider = provider
    resolution.model = "gemini-2.5-flash"
    # B1 — le worker lit `resolution.config` (ExpertConfig réel) pour
    # assembler la pile qualité. Un MagicMock nu casserait le join du
    # system_prompt (non-str). On fournit la vraie config "general".
    resolution.config = get_expert_config("general")
    router_mock = MagicMock()
    router_mock.resolve = MagicMock(return_value=resolution)
    monkeypatch.setattr(scheduler_tasks, "get_ai_router", lambda: router_mock)

    result = await scheduler_tasks.execute_scheduled_task({}, str(task.id))
    assert result["status"] == "success"
    # once sans next_run (at était futur mais le compute_next_run depuis
    # NOW retourne at si > NOW, donc la tâche ne se complète PAS
    # immédiatement sauf si at est passé). Ici le test vérifie juste le
    # chemin "happy" — auto_delete n'est appliqué que si next_run is None.
    # Pour déclencher le completed + auto_delete, il faudrait un `once`
    # dont `at` < NOW, mais Pydantic le rejetterait à la création.
    # On se contente ici de vérifier le pipeline success + recompute.
    assert task.run_count == 1


# ══════════════════════════════════════════════════════════════
# 11. LOT B1 — Pile qualité NEXYA injectée + disable_thinking +
#     max_tokens par-expert (fin du `EXECUTION_MAX_OUTPUT_TOKENS=2048`)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_injects_nexya_quality_stack(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le worker rejoue la recette `_run_link` : le system_prompt envoyé au
    provider contient le contexte temporel (preuve d'injection de la pile),
    `extra.disable_thinking=True`, et `max_tokens=config.max_tokens` (par-expert,
    plus le 2048 codé en dur qui vidait les experts Pro à thinking)."""
    task = _make_task()
    user = _make_user()
    patch_db(task=task, user=user)

    class _NoBudget:
        async def check_and_consume_chat(self, uid, cost=1):
            return 1

    monkeypatch.setattr(scheduler_tasks, "get_budget_tracker", lambda: _NoBudget())

    captured: dict[str, Any] = {}

    async def _capturing_stream(req):
        captured["req"] = req
        yield MagicMock(
            delta="Réponse divine.",
            usage=MagicMock(prompt_tokens=10, completion_tokens=20),
            finish_reason=None,
        )

    config = get_expert_config("general")
    provider = MagicMock()
    provider.name = "gemini"
    provider.stream_chat = _capturing_stream
    resolution = MagicMock()
    resolution.provider = provider
    resolution.model = config.primary_model
    resolution.config = config
    router_mock = MagicMock()
    router_mock.resolve = MagicMock(return_value=resolution)
    monkeypatch.setattr(scheduler_tasks, "get_ai_router", lambda: router_mock)

    result = await scheduler_tasks.execute_scheduled_task({}, str(task.id))

    assert result["status"] == "success"
    req = captured["req"]
    # Pile qualité : le bloc temporel (marqueur distinctif, absent des prompts
    # experts statiques) est injecté dans le system_prompt.
    assert req.system_prompt is not None
    assert "[Contexte temporel" in req.system_prompt
    # max_tokens = cap par-expert (4096 pour general), plus le 2048 codé en dur.
    assert req.max_tokens == config.max_tokens
    assert req.max_tokens != 2048
    # disable_thinking propagé (general = True) → fini le « réponse vide » Pro.
    assert req.extra.get("disable_thinking") is True
    # Température de l'expert, plus le 0.2 hardcodé.
    assert req.temperature == config.temperature


# ══════════════════════════════════════════════════════════════
# 12. LOT B2 — output_kind="reminder" → persona nudge + cap 256 +
#     thinking off + output_kind stocké dans le résultat
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_reminder_uses_nudge_persona_and_cap(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    task = _make_task()
    task.metadata_json = {"output_kind": "reminder"}
    user = _make_user()
    db = patch_db(task=task, user=user)

    class _NoBudget:
        async def check_and_consume_chat(self, uid, cost=1):
            return 1

    monkeypatch.setattr(scheduler_tasks, "get_budget_tracker", lambda: _NoBudget())

    captured: dict[str, Any] = {}

    async def _capturing_stream(req):
        captured["req"] = req
        yield MagicMock(delta="C'est l'heure d'étudier !", usage=None, finish_reason=None)

    config = get_expert_config("general")
    provider = MagicMock()
    provider.name = "gemini"
    provider.stream_chat = _capturing_stream
    resolution = MagicMock()
    resolution.provider = provider
    resolution.model = config.primary_model
    resolution.config = config
    router_mock = MagicMock()
    router_mock.resolve = MagicMock(return_value=resolution)
    monkeypatch.setattr(scheduler_tasks, "get_ai_router", lambda: router_mock)

    result = await scheduler_tasks.execute_scheduled_task({}, str(task.id))

    assert result["status"] == "success"
    req = captured["req"]
    # Persona « nudge » injectée (remplace la persona experte).
    assert "RAPPEL court et chaleureux" in req.system_prompt
    # Cap reminder dédié (256), pas le max_tokens de l'expert.
    assert req.max_tokens == scheduler_tasks.REMINDER_MAX_OUTPUT_TOKENS
    assert req.max_tokens == 256
    # Thinking off pour un rappel (aucun raisonnement requis).
    assert req.extra.get("disable_thinking") is True
    # Le résultat porte output_kind="reminder" pour la carte adaptative frontend.
    result_rows = [r for r in db.added if hasattr(r, "status")]
    assert any(
        (getattr(r, "metadata_json", None) or {}).get("output_kind") == "reminder"
        for r in result_rows
    )


# ══════════════════════════════════════════════════════════════
# 13. LOT B3 — output_kind="document" → LLM produit le markdown,
#     generate_from_markdown rend le PDF, le résultat porte le bloc document
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_document_renders_and_stores_block(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le worker produit le Markdown via le LLM (pile qualité), puis le rend en
    PDF via `generate_from_markdown` et stocke `{library_id, filename, format,
    pages, size_bytes, truncated}` dans `result.metadata_json["document"]`."""
    task = _make_task()
    task.title = "Rapport hebdo"
    task.metadata_json = {"output_kind": "document"}
    user = _make_user()
    db = patch_db(task=task, user=user)

    class _NoBudget:
        async def check_and_consume_chat(self, uid, cost=1):
            return 1

    monkeypatch.setattr(scheduler_tasks, "get_budget_tracker", lambda: _NoBudget())

    async def _stream(req):
        yield MagicMock(delta="# Rapport\n\nContenu détaillé.", usage=None, finish_reason=None)

    config = get_expert_config("general")
    provider = MagicMock()
    provider.name = "gemini"
    provider.stream_chat = _stream
    resolution = MagicMock()
    resolution.provider = provider
    resolution.model = config.primary_model
    resolution.config = config
    router_mock = MagicMock()
    router_mock.resolve = MagicMock(return_value=resolution)
    monkeypatch.setattr(scheduler_tasks, "get_ai_router", lambda: router_mock)

    lib_id = uuid.uuid4()
    captured_doc: dict[str, Any] = {}

    async def _fake_generate_from_markdown(
        doc_user, doc_db, *, markdown, title, output_format, template
    ):
        captured_doc["markdown"] = markdown
        captured_doc["title"] = title
        captured_doc["output_format"] = output_format
        captured_doc["template"] = template
        return MagicMock(
            library_id=lib_id,
            filename="nexya_minimal_rapport-hebdo_2026-06-23.pdf",
            pages=2,
            size_bytes=34567,
            truncated=False,
        )

    monkeypatch.setattr(
        "app.features.document_generator.service.DocumentGeneratorService.generate_from_markdown",
        _fake_generate_from_markdown,
    )

    result = await scheduler_tasks.execute_scheduled_task({}, str(task.id))

    assert result["status"] == "success"
    # Le markdown produit par le LLM + le titre de la tâche sont transmis au rendu.
    assert captured_doc["markdown"] == "# Rapport\n\nContenu détaillé."
    assert captured_doc["title"] == "Rapport hebdo"
    assert captured_doc["output_format"] == "pdf"
    assert captured_doc["template"] == "minimal"
    # Le résultat persisté porte le bloc document complet.
    result_rows = [r for r in db.added if hasattr(r, "status")]
    doc_rows = [
        r
        for r in result_rows
        if (getattr(r, "metadata_json", None) or {}).get("document") is not None
    ]
    assert len(doc_rows) == 1
    doc_meta = doc_rows[0].metadata_json["document"]
    assert doc_meta["library_id"] == str(lib_id)
    assert doc_meta["filename"] == "nexya_minimal_rapport-hebdo_2026-06-23.pdf"
    assert doc_meta["format"] == "pdf"
    assert doc_meta["pages"] == 2
    assert doc_meta["size_bytes"] == 34567
    assert doc_meta["truncated"] is False
    assert doc_rows[0].metadata_json["output_kind"] == "document"


# ══════════════════════════════════════════════════════════════
# 14. LOT B3 — échec de rendu document = fail-safe : la tâche reste 'success'
#     (le markdown reste exploitable), aucun bloc document
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_execute_document_render_failure_is_failsafe(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un crash du rendu (WeasyPrint OOM, storage down, quota Library) ne dégrade
    PAS la tâche : le résultat reste 'success' avec le Markdown, et metadata ne
    contient AUCUN bloc document. Le worker ne lève jamais."""
    task = _make_task()
    task.metadata_json = {"output_kind": "document"}
    user = _make_user()
    db = patch_db(task=task, user=user)

    class _NoBudget:
        async def check_and_consume_chat(self, uid, cost=1):
            return 1

    monkeypatch.setattr(scheduler_tasks, "get_budget_tracker", lambda: _NoBudget())

    async def _stream(req):
        yield MagicMock(delta="# Doc\n\nok.", usage=None, finish_reason=None)

    config = get_expert_config("general")
    provider = MagicMock()
    provider.name = "gemini"
    provider.stream_chat = _stream
    resolution = MagicMock()
    resolution.provider = provider
    resolution.model = config.primary_model
    resolution.config = config
    router_mock = MagicMock()
    router_mock.resolve = MagicMock(return_value=resolution)
    monkeypatch.setattr(scheduler_tasks, "get_ai_router", lambda: router_mock)

    async def _boom(doc_user, doc_db, *, markdown, title, output_format, template):
        raise RuntimeError("WeasyPrint OOM")

    monkeypatch.setattr(
        "app.features.document_generator.service.DocumentGeneratorService.generate_from_markdown",
        _boom,
    )

    result = await scheduler_tasks.execute_scheduled_task({}, str(task.id))

    # La tâche reste un succès (le markdown reste exploitable + notifié).
    assert result["status"] == "success"
    result_rows = [r for r in db.added if hasattr(r, "status")]
    assert any(r.status == "success" for r in result_rows)
    # Aucun bloc document (rendu échoué fail-safe), mais output_kind préservé.
    for r in result_rows:
        meta = getattr(r, "metadata_json", None) or {}
        assert meta.get("document") is None
        assert meta.get("output_kind") == "document"
