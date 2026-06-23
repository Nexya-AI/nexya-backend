"""
Workers arq — Planner Scheduler (F1).

3 fonctions exposées à WorkerSettings :
- `dispatch_due_tasks(ctx)` — cron chaque minute. Scan les tâches dues
  via `SELECT ... FOR UPDATE SKIP LOCKED LIMIT 50`, marque bulk status
  `pending`, enqueue un job `execute_scheduled_task(task_id)` par
  tâche. Fail-silent sur Redis down.
- `execute_scheduled_task(ctx, task_id)` — worker task. Lock le row,
  consomme budget chat, appelle le LLM via router, stocke le résultat,
  recompute `next_run_at` + `last_run_at` + `run_count`. Retry
  transient (ProviderUnavailableError) jusqu'à `max_retries=2`.
- `cleanup_old_task_results(ctx)` — cron quotidien 04:17 UTC. DELETE
  les résultats > `tasks_results_retention_days` (30 j par défaut).

Stratégie concurrence :
- `SELECT ... FOR UPDATE SKIP LOCKED` permet à plusieurs workers arq
  de scanner en parallèle sans race (chaque worker prend un batch
  disjoint).
- `UPDATE status='pending'` bulk immédiat avant `enqueue_job` = idempotence
  double-check (si le job est redispatché, il voit `status='pending'` et
  ne re-exécute pas).
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import structlog
from sqlalchemy import delete, select, text, update

from app.ai.budget_tracker import get_budget_tracker
from app.ai.experts import ExpertConfig
from app.ai.nexya_preamble import build_nexya_preamble
from app.ai.nexya_temporal import build_temporal_context
from app.ai.providers import (
    ChatCompletionRequest,
    ChatMessage,
    ChatProvider,
)
from app.ai.providers.base import (
    FinishReason,
    ProviderError,
    ProviderUnavailableError,
)
from app.ai.retry import DEFAULT_POLICY, stream_chat_with_retry
from app.ai.runtime import get_ai_router
from app.config import settings
from app.core.database.postgres import AsyncSessionLocal
from app.core.errors.exceptions import RateLimitExceededException
from app.features.auth.models import User
from app.features.experts.context_builder import build_expert_corpus_context
from app.features.memory.context_builder import build_memory_context
from app.features.notifications.service import NotificationDispatcher
from app.features.planner.models import (
    ScheduledTask,
    ScheduledTaskResult,
)
from app.features.planner.output_kind import (
    OUTPUT_KIND_DOCUMENT,
    OUTPUT_KIND_REMINDER,
    extract_output_kind,
)
from app.features.planner.scheduler import compute_next_run

if TYPE_CHECKING:
    from arq.connections import ArqRedis

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

RETRY_DEFER_MINUTES: Final[int] = 5

# Plafond de tokens pour un rappel court et chaleureux (output_kind="reminder",
# LOT B2). Indépendant de l'expert : un nudge tient en 1 à 3 phrases. Pour les
# `generation`/`document`, on utilise `config.max_tokens` (par-expert : 4096 à
# 8192) — fini le `EXECUTION_MAX_OUTPUT_TOKENS=2048` codé en dur qui étranglait
# les longues réponses ET vidait les experts Pro à thinking (le budget partait
# en réflexion avant le 1er token visible).
REMINDER_MAX_OUTPUT_TOKENS: Final[int] = 256

# System prompt « nudge » pour les tâches `output_kind="reminder"`. Remplace la
# persona experte (via `system_override`) tout en gardant le préambule NEXYA +
# le contexte temporel + la mémoire (le rappel reste personnalisé et NEXYA-voiced,
# mais court et chaleureux plutôt qu'une réponse riche).
REMINDER_SYSTEM_PROMPT: Final[str] = (
    "Tu envoies un RAPPEL court et chaleureux à l'utilisateur (tutoiement). "
    "Le message ci-dessous décrit ce qu'il t'a demandé de lui rappeler. "
    "Produis UNIQUEMENT le rappel, en 1 à 3 phrases maximum, sur un ton "
    "motivant et bienveillant — comme un ami attentionné qui pousse gentiment "
    "à agir. Interdits : titre, markdown lourd, liste à puces, préambule "
    "(« Bien sûr ! »). Va droit au but, sois concret, et termine si pertinent "
    "par un micro-encouragement (« tu vas y arriver », « lance-toi »)."
)


# ══════════════════════════════════════════════════════════════
# Pool arq lazy
# ══════════════════════════════════════════════════════════════

_arq_pool: ArqRedis | None = None


async def _get_arq_pool() -> ArqRedis:
    global _arq_pool
    if _arq_pool is None:
        from arq.connections import RedisSettings, create_pool  # noqa: PLC0415

        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _arq_pool


async def enqueue_task_execution(task_id: UUID) -> None:
    """Enqueue `execute_scheduled_task(task_id)`. Fail-silent."""
    try:
        pool = await _get_arq_pool()
        await pool.enqueue_job("execute_scheduled_task", str(task_id))
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "planner.enqueue_failed",
            task_id=str(task_id),
            error=str(exc),
            error_type=type(exc).__name__,
        )


# ══════════════════════════════════════════════════════════════
# CRON — dispatch_due_tasks
# ══════════════════════════════════════════════════════════════


async def dispatch_due_tasks(ctx: dict[str, Any]) -> dict[str, Any]:
    """Scan les tâches dues et enqueue une exécution par tâche.

    Pattern PostgreSQL canonique pour queue-on-DB :
    1. `SELECT id FROM scheduled_tasks WHERE next_run_at <= NOW() AND
       active AND NOT paused AND status NOT IN ('running','completed')
       FOR UPDATE SKIP LOCKED LIMIT 50` — récupère un batch atomique,
       plusieurs workers en parallèle ne se marchent pas dessus.
    2. `UPDATE status='pending'` bulk immédiat dans la même transaction.
    3. `enqueue_job` par tâche après commit — les jobs échouent
       silencieusement si Redis down (fail-silent pattern).
    """
    log.debug("planner.dispatch.tick_start")
    dispatched: list[UUID] = []

    async with AsyncSessionLocal() as db:
        # SELECT FOR UPDATE SKIP LOCKED — PostgreSQL-only, ignoré par SQLite.
        sql = text(
            """
            SELECT id FROM scheduled_tasks
            WHERE deleted_at IS NULL
              AND active = true
              AND paused = false
              AND next_run_at IS NOT NULL
              AND next_run_at <= NOW()
              AND status NOT IN ('running','completed')
            ORDER BY next_run_at ASC
            FOR UPDATE SKIP LOCKED
            LIMIT :batch_size
            """
        ).bindparams(batch_size=settings.tasks_dispatch_batch_size)
        result = await db.execute(sql)
        rows = result.all()
        task_ids: list[UUID] = [row[0] for row in rows]

        if task_ids:
            await db.execute(
                update(ScheduledTask)
                .where(ScheduledTask.id.in_(task_ids))
                .values(
                    status="pending",
                    updated_at=datetime.now(tz=UTC),
                )
            )
            await db.commit()
            dispatched = task_ids

    for tid in dispatched:
        await enqueue_task_execution(tid)

    log.info(
        "planner.dispatch.completed",
        dispatched=len(dispatched),
        batch_size=settings.tasks_dispatch_batch_size,
    )
    return {"dispatched": len(dispatched)}


# ══════════════════════════════════════════════════════════════
# Pile qualité NEXYA (recette `StreamHandler._run_link` rejouée à la main)
# ══════════════════════════════════════════════════════════════
#
# Le worker tourne en arrière-plan (arq) : aucun client SSE, donc on NE
# PEUT PAS appeler `StreamHandler.stream()` (qui exige une `fastapi.Request`
# pour gérer l'annulation + le heartbeat). On rejoue la recette de
# `app/ai/streaming.py::_run_link` à la main, mais simplifiée : pas de tools,
# pas de RAG documents, pas de partner_context, pas de model-pill override
# (le planner n'a pas d'UI de pill). On consomme `stream_chat_with_retry`
# en bouclant les chunks et en concaténant `chunk.delta`.


async def _build_quality_context(
    db: Any,
    user: User,
    config: ExpertConfig,
    *,
    prompt: str,
) -> tuple[str | None, str | None, str | None, str | None]:
    """Construit les 4 blocs contextuels de la pile NEXYA pour le worker.

    Retourne `(nexya_preamble, temporal_block, memory_context, corpus_context)`.
    Tous fail-safe : un bloc qui échoue vaut `None` (le chat / la tâche
    continue sans lui). Doit être appelé DANS une session DB ouverte avec
    `user` non expiré (memory + corpus lisent la base).

    Note coût (RÈGLE G) : `build_memory_context` consomme 1 crédit embeddings
    par exécution (recherche pgvector). Acceptable (~$0.00002/run). Corpus
    n'est interrogé que si `config.corpus_enabled` (cuisine uniquement).

    Fail-safe absolu : chaque builder est déjà fail-safe (→ None sur erreur),
    mais on enveloppe tout dans un try/except de défense en profondeur — ce
    helper est appelé DANS la session 1, hors du try/except du bloc LLM. Une
    exception ici ferait crasher `execute_scheduled_task` → arq retenterait
    le job à l'infini (la tâche reste `idle`, jamais marquée running). On ne
    lève donc JAMAIS : au pire, les 4 blocs valent `None` et le LLM tourne
    avec le seul system_prompt expert (dégradé mais fonctionnel).
    """
    try:
        # (0) Préambule NEXYA — pur, sans DB. Identité + ton + routing. EN TÊTE.
        nexya_preamble = build_nexya_preamble(config.expert_id, user_message=prompt or None)

        # (1) Contexte temporel — pur, sans DB. Le worker n'a pas de fuseau
        # client (la tâche a déjà calculé son `next_run_at`), donc UTC. Le bloc
        # donne au LLM la notion de « maintenant » (utile si le prompt dit
        # « résume l'actu d'aujourd'hui »).
        temporal_block = build_temporal_context()

        # (2) Mémoire IA (D3) — faits durables de l'user, fail-safe → None.
        memory_context = await build_memory_context(user, db, query=prompt)

        # (3) Corpus expert (G1/G2) — gated `config.corpus_enabled` (cuisine V1).
        corpus_context: str | None = None
        if config.corpus_enabled:
            corpus_context = await build_expert_corpus_context(
                expert_slug=config.expert_id,
                query=prompt,
                db=db,
            )

        return nexya_preamble, temporal_block, memory_context, corpus_context
    except Exception as exc:  # noqa: BLE001 — défense en profondeur worker
        log.warning(
            "planner.execute.quality_context_failed",
            expert_id=getattr(config, "expert_id", None),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None, None, None, None


def _assemble_system_prompt(
    config: ExpertConfig,
    *,
    nexya_preamble: str | None,
    temporal_block: str | None,
    memory_context: str | None,
    corpus_context: str | None,
    system_override: str | None = None,
) -> str:
    """Concatène les blocs dans l'ordre canonique `_run_link` (sans tools/rag).

    `system_override` remplace `config.system_prompt` — utilisé par le mode
    `reminder` (LOT B2) qui injecte un prompt « nudge » dédié tout en gardant
    le préambule + le contexte temporel + la mémoire.
    """
    expert_block = system_override if system_override is not None else (config.system_prompt or None)
    parts = [
        nexya_preamble,
        temporal_block,
        memory_context,
        corpus_context,
        expert_block,
    ]
    return "\n\n".join(p for p in parts if p)


async def _run_nexya_completion(
    *,
    provider: ChatProvider,
    model: str,
    system_prompt: str,
    prompt: str,
    temperature: float,
    max_tokens: int | None,
    disable_thinking: bool,
    user_id: str | None,
    expert_id: str | None,
) -> tuple[str, int, int, FinishReason | None]:
    """Appelle le LLM via `stream_chat_with_retry` et collecte la réponse.

    Retourne `(texte, tokens_in, tokens_out, finish_reason)`. Ne catche AUCUNE
    exception : le caller (`execute_scheduled_task`) gère la classification
    `ProviderUnavailableError` (retry transient) vs `ProviderError` (échec dur)
    pour rester fail-safe absolu (le worker ne lève jamais).
    """
    extra: dict[str, Any] = {}
    if disable_thinking:
        # G2 V1.1 / fix 2026-05-22 « réponse vide » — sans ça, Gemini Pro
        # consomme tout le budget en réflexion avant le 1er token visible.
        extra["disable_thinking"] = True

    req = ChatCompletionRequest(
        messages=[ChatMessage(role="user", content=prompt)],
        system_prompt=system_prompt,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        user_id=user_id,
        expert_id=expert_id,
        extra=extra,
    )

    parts: list[str] = []
    tokens_in = 0
    tokens_out = 0
    finish: FinishReason | None = None
    async for chunk in stream_chat_with_retry(provider, req, policy=DEFAULT_POLICY):
        if chunk.delta:
            parts.append(chunk.delta)
        if chunk.usage is not None:
            tokens_in = max(tokens_in, int(chunk.usage.prompt_tokens or 0))
            tokens_out = max(tokens_out, int(chunk.usage.completion_tokens or 0))
        if chunk.finish_reason is not None:
            finish = chunk.finish_reason
    return "".join(parts), tokens_in, tokens_out, finish


# ══════════════════════════════════════════════════════════════
# WORKER — execute_scheduled_task
# ══════════════════════════════════════════════════════════════


async def execute_scheduled_task(ctx: dict[str, Any], task_id: str) -> dict[str, Any]:
    """Exécute une tâche : LLM → INSERT result → recompute next_run_at.

    Short-circuits : deleted, paused, already running, completed.

    Fail-safe :
    - Budget chat user épuisé → INSERT result `status='skipped'` +
      reprogramme la tâche sur `next_run_at` standard (pas de retry).
    - `ProviderUnavailableError` (réseau, 5xx) → retry_count++ et
      next_run_at = now + 5 min jusqu'à max_retries, ensuite status=failed.
    - `ProviderError` non-retryable → INSERT result failed + status=failed
      (pas de retry).
    """
    t0 = time.monotonic()
    task_uuid = UUID(task_id)
    log.info("planner.execute.start", task_id=task_id)

    # Variables capturées dans la session 1 et réutilisées hors session pour
    # l'appel LLM (anti-MissingGreenlet : on ne touche aucun attribut ORM
    # après la fermeture de la session). Pré-initialisées par sûreté ; toutes
    # les sorties de garde `return` avant qu'on n'atteigne le bloc LLM.
    task_prompt: str = ""
    task_title: str = ""
    task_expert_id: str = "general"
    task_output_kind: str = "generation"
    user_id_str: str = ""
    task_user_id: UUID | None = None
    config: ExpertConfig | None = None
    resolution_provider: ChatProvider | None = None
    resolution_model: str | None = None
    quality_blocks: tuple[str | None, str | None, str | None, str | None] = (
        None,
        None,
        None,
        None,
    )

    async with AsyncSessionLocal() as db:
        task = await db.get(ScheduledTask, task_uuid)
        if task is None:
            log.warning("planner.execute.task_missing", task_id=task_id)
            return {"skipped": True, "reason": "missing"}
        if task.deleted_at is not None:
            log.info("planner.execute.skip_deleted", task_id=task_id)
            return {"skipped": True, "reason": "deleted"}
        if task.paused:
            log.info("planner.execute.skip_paused", task_id=task_id)
            return {"skipped": True, "reason": "paused"}
        if task.status == "running":
            log.info("planner.execute.skip_already_running", task_id=task_id)
            return {"skipped": True, "reason": "already_running"}
        if task.status == "completed":
            log.info("planner.execute.skip_completed", task_id=task_id)
            return {"skipped": True, "reason": "completed"}

        # Charge l'user owner.
        user_result = await db.execute(select(User).where(User.id == task.user_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            log.warning(
                "planner.execute.user_missing",
                task_id=task_id,
                user_id=str(task.user_id),
            )
            return {"skipped": True, "reason": "user_missing"}

        # Capture les scalaires AVANT tout commit (anti-MissingGreenlet) — ces
        # str survivent à la fermeture de la session, contrairement aux
        # attributs ORM qui s'expirent au commit.
        task_prompt = task.prompt
        task_title = task.title
        task_expert_id = task.expert_id
        task_output_kind = extract_output_kind(task.metadata_json)
        task_user_id = task.user_id
        user_id_str = str(task.user_id)

        # Résolution expert : `config` (ExpertConfig frozen), `provider`
        # (singleton) et `model` (str) ne sont pas des objets ORM → réutilisables
        # hors session.
        resolution = get_ai_router().resolve(task_expert_id)
        config = resolution.config
        resolution_provider = resolution.provider
        resolution_model = resolution.model

        # Pile qualité NEXYA : preamble + temporal + mémoire + corpus. Construit
        # ICI car mémoire + corpus lisent la base (besoin de `db` + `user` non
        # expirés). Capturé en str réutilisables hors session.
        quality_blocks = await _build_quality_context(db, user, config, prompt=task_prompt)

        # Marque la tâche en cours.
        task.status = "running"
        task.updated_at = datetime.now(tz=UTC)
        await db.commit()

    # Exécution hors de la session initiale (on ne tient pas une
    # transaction pendant l'appel LLM qui peut prendre plusieurs secondes).
    result_status = "success"
    result_text: str | None = None
    error_text: str | None = None
    tokens_in = 0
    tokens_out = 0
    cost_usd = 0.0
    model = None
    provider_name = None
    should_retry_transient = False

    try:
        # Budget chat pré-flight — si épuisé → skipped (pas de retry).
        tracker = get_budget_tracker()
        try:
            await tracker.check_and_consume_chat(user_id_str, cost=1)
        except RateLimitExceededException:
            result_status = "skipped"
            error_text = "Quota chat journalier épuisé."
            log.info(
                "planner.execute.budget_exhausted",
                task_id=task_id,
                user_id=user_id_str,
            )
        else:
            # Appel LLM via la pile qualité NEXYA (recette `_run_link`),
            # dispatché sur `output_kind` (LOT B2).
            try:
                nexya_preamble, temporal_block, memory_context, corpus_context = quality_blocks

                if task_output_kind == OUTPUT_KIND_REMINDER:
                    # Rappel court et chaleureux : persona « nudge » (via
                    # system_override) + cap 256 tokens + thinking off (un
                    # rappel ne nécessite aucun raisonnement). Le préambule +
                    # contexte temporel + mémoire restent injectés → le nudge
                    # est NEXYA-voiced et personnalisé.
                    system_prompt_final = _assemble_system_prompt(
                        config,
                        nexya_preamble=nexya_preamble,
                        temporal_block=temporal_block,
                        memory_context=memory_context,
                        corpus_context=corpus_context,
                        system_override=REMINDER_SYSTEM_PROMPT,
                    )
                    completion_max_tokens: int | None = REMINDER_MAX_OUTPUT_TOKENS
                    completion_disable_thinking = True
                else:
                    # generation ET document : le LLM produit le Markdown riche
                    # avec la persona experte complète + cap par-expert + thinking
                    # config. Pour `document`, ce Markdown sera ensuite rendu en
                    # PDF/DOCX réel (bloc B3 plus bas) ; pour `generation`, il est
                    # renvoyé tel quel.
                    system_prompt_final = _assemble_system_prompt(
                        config,
                        nexya_preamble=nexya_preamble,
                        temporal_block=temporal_block,
                        memory_context=memory_context,
                        corpus_context=corpus_context,
                    )
                    completion_max_tokens = config.max_tokens
                    completion_disable_thinking = config.disable_thinking

                result_text, tokens_in, tokens_out, _finish = await _run_nexya_completion(
                    provider=resolution_provider,
                    model=resolution_model,
                    system_prompt=system_prompt_final,
                    prompt=task_prompt,
                    temperature=config.temperature,
                    max_tokens=completion_max_tokens,
                    disable_thinking=completion_disable_thinking,
                    user_id=user_id_str,
                    expert_id=config.expert_id,
                )
                model = resolution_model
                provider_name = resolution_provider.name
                log.info(
                    "planner.execute.llm_ok",
                    task_id=task_id,
                    provider=provider_name,
                    model=model,
                    output_kind=task_output_kind,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                )
            except ProviderUnavailableError as exc:
                result_status = "failed"
                error_text = f"Provider indisponible : {exc}"
                should_retry_transient = True
                provider_name = getattr(exc, "provider", None)
                log.warning(
                    "planner.execute.provider_unavailable",
                    task_id=task_id,
                    error=str(exc),
                )
            except ProviderError as exc:
                result_status = "failed"
                error_text = f"Erreur provider : {exc}"
                provider_name = getattr(exc, "provider", None)
                log.warning(
                    "planner.execute.provider_error",
                    task_id=task_id,
                    error=str(exc),
                )
            except Exception as exc:  # noqa: BLE001
                result_status = "failed"
                error_text = f"Erreur inattendue : {exc}"
                log.error(
                    "planner.execute.unexpected_error",
                    task_id=task_id,
                    error=str(exc),
                    error_type=type(exc).__name__,
                )
    except Exception as exc:  # noqa: BLE001
        # Dernier garde-fou (Budget tracker crash, etc.) — on ne plante pas
        # le worker, on remonte le flag et on reprogramme plus tard.
        result_status = "failed"
        error_text = f"Budget tracker ou setup KO : {exc}"
        should_retry_transient = True

    # ── LOT B3 : rendu document réel (output_kind="document") ──
    # Le LLM a produit le Markdown (`result_text`). Pour une tâche `document`,
    # on le rend en PDF réel stocké dans la Bibliothèque (branding C4.8 +
    # watermark C4.7d + C2PA AI Act via `generate_from_markdown`), dans une
    # session DB fraîche (mémoire/corpus ont fermé la leur ; le rendu lit
    # `users` + écrit `library_items`).
    #
    # Fail-safe absolu : un échec de rendu (storage down, quota Library, source
    # trop longue, WeasyPrint KO) ne dégrade PAS la tâche — le Markdown reste
    # exploitable et notifié. `document_block` reste None et la carte frontend
    # retombe sur le rendu Markdown générique. Le worker ne lève JAMAIS.
    document_block: dict[str, Any] | None = None
    if (
        task_output_kind == OUTPUT_KIND_DOCUMENT
        and result_status == "success"
        and result_text
        and result_text.strip()
        and task_user_id is not None
    ):
        try:
            # Import paresseux : `document_generator.service` tire WeasyPrint +
            # pikepdf + python-docx (lourds). On ne les charge que pour une
            # tâche `document`, pas à l'import du worker.
            from app.features.document_generator.service import (  # noqa: PLC0415
                DocumentGeneratorService,
            )

            async with AsyncSessionLocal() as doc_db:
                doc_user_result = await doc_db.execute(
                    select(User).where(User.id == task_user_id)
                )
                doc_user = doc_user_result.scalar_one_or_none()
                if doc_user is not None:
                    doc_response = await DocumentGeneratorService.generate_from_markdown(
                        doc_user,
                        doc_db,
                        markdown=result_text,
                        title=(task_title or None),
                        output_format="pdf",
                        template="minimal",
                    )
                    document_block = {
                        "library_id": str(doc_response.library_id),
                        "filename": doc_response.filename,
                        "format": "pdf",
                        "pages": doc_response.pages,
                        "size_bytes": doc_response.size_bytes,
                        "truncated": doc_response.truncated,
                    }
                    log.info(
                        "planner.execute.document_rendered",
                        task_id=task_id,
                        library_id=document_block["library_id"],
                        pages=doc_response.pages,
                        size_bytes=doc_response.size_bytes,
                        truncated=doc_response.truncated,
                    )
                else:
                    log.warning(
                        "planner.execute.document_user_missing",
                        task_id=task_id,
                        user_id=user_id_str,
                    )
        except Exception as exc:  # noqa: BLE001 — fail-safe absolu worker
            log.warning(
                "planner.execute.document_render_failed",
                task_id=task_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    duration_ms = int((time.monotonic() - t0) * 1000)

    # Recompute next_run_at + persistance dans une nouvelle session.
    async with AsyncSessionLocal() as db:
        task = await db.get(ScheduledTask, task_uuid)
        if task is None:
            log.warning(
                "planner.execute.task_vanished_post_exec",
                task_id=task_id,
            )
            return {"skipped": True, "reason": "vanished"}

        # INSERT result
        result_row = ScheduledTaskResult(
            task_id=task_uuid,
            user_id=task.user_id,
            duration_ms=duration_ms,
            status=result_status,
            result_text=result_text if result_status == "success" else None,
            error_text=error_text,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            cost_usd=cost_usd,
            model=model,
            provider=provider_name,
            # Le résultat porte son output_kind (B2 — le frontend rend la bonne
            # carte adaptative) + le bloc `document` (B3 — library_id, filename,
            # format, pages, size_bytes, truncated) si un PDF a été rendu. La
            # `download_url` n'est PAS persistée ici (presigned MinIO expirable) :
            # le sérialiseur la régénère FRAÎCHE au read.
            metadata_json=(
                {"output_kind": task_output_kind, "document": document_block}
                if document_block is not None
                else {"output_kind": task_output_kind}
            ),
        )
        db.add(result_row)

        # Recompute status + next_run_at selon le résultat.
        now_utc = datetime.now(tz=UTC)
        task.last_run_at = now_utc
        task.run_count = (task.run_count or 0) + 1

        if should_retry_transient and task.retry_count < task.max_retries:
            # Retry transient : re-scheduler dans 5 min.
            task.retry_count += 1
            task.next_run_at = now_utc + timedelta(minutes=RETRY_DEFER_MINUTES)
            task.status = "idle"
            log.info(
                "planner.execute.retry_scheduled",
                task_id=task_id,
                retry_count=task.retry_count,
                next_run=task.next_run_at.isoformat(),
            )
        elif result_status == "failed":
            # Non-retryable ou max_retries atteint.
            task.status = "failed"
            task.next_run_at = None
            log.info(
                "planner.execute.final_failure",
                task_id=task_id,
                retry_count=task.retry_count,
            )
        else:
            # Success ou skipped : reset retry + recompute next_run_at.
            task.retry_count = 0
            next_run = compute_next_run(task.schedule_type, task.schedule_config or {})
            if next_run is None:
                # Tâche 'once' terminée.
                task.status = "completed"
                task.next_run_at = None
                if task.auto_delete_after_run and result_status == "success":
                    task.deleted_at = now_utc
                    task.active = False
                    log.info(
                        "planner.execute.auto_deleted_after_run",
                        task_id=task_id,
                    )
            else:
                task.next_run_at = next_run
                task.status = "idle"

        task.updated_at = now_utc
        await db.commit()

    log.info(
        "planner.execute.completed",
        task_id=task_id,
        result_status=result_status,
        duration_ms=duration_ms,
    )

    # ── F3 : dispatcher dual-channel (push + email fallback) fail-safe ──
    # Le dispatcher lit les préférences user, tente push, bascule sur email
    # en fallback si push KO ou pas de device actif (selon settings), et
    # trace une row `notifications` pour la timeline in-app. Fail-safe strict :
    # ne raise JAMAIS au caller — le worker arq ne doit pas crasher sur une
    # panne FCM/Brevo/DB.
    try:
        await _dispatch_task_notification(
            task_uuid=task_uuid,
            result_status=result_status,
            result_text=result_text,
        )
    except Exception as exc:  # noqa: BLE001 — ceinture + bretelles
        log.warning(
            "planner.notification.unexpected_error",
            task_id=task_id,
            error=str(exc),
            error_type=type(exc).__name__,
        )

    return {
        "skipped": False,
        "status": result_status,
        "duration_ms": duration_ms,
    }


# ══════════════════════════════════════════════════════════════
# F3 — Hook NotificationDispatcher post-exécution
# ══════════════════════════════════════════════════════════════


def _build_task_notification_body(result_status: str, result_text: str | None) -> str:
    """Construit le body de notification (preview 140 chars max)."""
    preview_max = int(settings.fcm_body_preview_max_chars)
    if result_status == "success":
        body = (result_text or "").strip()
        if not body:
            return "Tâche exécutée."
        if len(body) > preview_max:
            body = body[: max(0, preview_max - 1)].rstrip() + "…"
        return body
    if result_status == "skipped":
        return "Tâche reportée (quota journalier atteint)."
    return "Échec — réessai automatique bientôt."


async def _dispatch_task_notification(
    *,
    task_uuid: UUID,
    result_status: str,
    result_text: str | None,
) -> None:
    """Délègue au `NotificationDispatcher` avec `category='tasks'`.

    Le dispatcher s'occupe de tout le reste : lookup préférences user,
    push FCM avec soft-delete UNREGISTERED auto, fallback email si
    préférence le permet, INSERT row `notifications` pour timeline
    in-app, log forensic complet.

    Ce helper reste minimal : charge task + user, construit le payload
    sémantique, délègue, c'est tout.
    """
    async with AsyncSessionLocal() as db:
        task = await db.get(ScheduledTask, task_uuid)
        if task is None or task.deleted_at is not None:
            log.info(
                "planner.notification.skipped_task_gone",
                task_id=str(task_uuid),
            )
            return
        user = await db.get(User, task.user_id)
        if user is None:
            log.warning(
                "planner.notification.user_missing",
                task_id=str(task_uuid),
                user_id=str(task.user_id),
            )
            return

        title = f"NEXYA — {task.title}"
        body = _build_task_notification_body(result_status, result_text)
        task_id_str = str(task_uuid)
        data_payload: dict[str, Any] = {
            "task_id": task_id_str,
            "status": result_status,
            "deep_link": f"nexya://task/{task_id_str}",
            "task_title": task.title,
            "notification_kind": "completed",
        }

        await NotificationDispatcher.dispatch(
            user=user,
            category="tasks",
            title=title,
            body=body,
            data=data_payload,
            source_task_id=task_uuid,
            source_kind="scheduled_task",
            db=db,
        )


# ══════════════════════════════════════════════════════════════
# CRON — cleanup_old_task_results
# ══════════════════════════════════════════════════════════════


async def cleanup_old_task_results(
    ctx: dict[str, Any],
) -> dict[str, Any]:
    """Purge les résultats > `tasks_results_retention_days`."""
    cutoff = datetime.now(tz=UTC) - timedelta(days=settings.tasks_results_retention_days)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            delete(ScheduledTaskResult)
            .where(ScheduledTaskResult.ran_at < cutoff)
            .returning(ScheduledTaskResult.id)
        )
        deleted_ids = list(result.scalars().all())
        await db.commit()

    log.info(
        "planner.cleanup.completed",
        deleted_count=len(deleted_ids),
        retention_days=settings.tasks_results_retention_days,
    )
    return {"deleted": len(deleted_ids)}
