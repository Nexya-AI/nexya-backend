"""
Router Chat — endpoints CRUD + SSE streaming persisté.

Ce module regroupe sous un seul `APIRouter(prefix="/chat")` :

- `POST /chat/conversations`            — création
- `GET  /chat/conversations`            — liste paginée (cursor-based)
- `GET  /chat/conversations/trash`      — corbeille paginée (F2.0)
- `GET  /chat/conversations/{id}`       — détail
- `PATCH /chat/conversations/{id}`      — update partiel
- `DELETE /chat/conversations/{id}`     — soft-delete (204)
- `POST /chat/conversations/{id}/restore`   — sortir de la corbeille (F2.0)
- `DELETE /chat/conversations/{id}/permanent` — purge définitive (F2.0, 204)
- `GET  /chat/conversations/{id}/messages` — historique paginé
- `POST /chat/stream`                   — SSE streaming persisté (Lot 4)
- `POST /chat/stop`                     — annulation via clé Redis (Lot 4)

Discipline NEXYA (cf. CLAUDE.md § 8) :
- Aucune logique métier ici — chaque endpoint délègue à `ConversationService`,
  `BudgetTracker`, `ModerationService` ou `StreamHandler`.
- Toutes les réponses (sauf 204 DELETE et SSE) encapsulées dans `NexyaResponse[T]`.
- Guards `get_current_user` sur toutes les routes (auth obligatoire).
- Conversions ORM → Pydantic ici, pas dans le service.

Points critiques du stream persisté :
- **Trois modes** selon les champs présents dans `ChatStreamRequest` :
  1. `conversation_id=None, history=[]` → création implicite + persistance.
  2. `conversation_id=<UUID>`          → ajout à une conv existante (IDOR-safe).
  3. `conversation_id=None, history=[...]` → legacy stateless, PAS de persistance
     (pour compat Flutter actuel — retiré quand tous les clients migrent).
- **Atomicité** : le placeholder `Message(status='streaming')` est inséré AVANT
  que la chaîne provider soit contactée. Toute issue (success, failure, cancel,
  disconnect) est matérialisée par un UPDATE final dans une session DB fraîche,
  shieldée contre la cancellation pour être robuste à un `client_disconnect` en
  plein stream (sinon le placeholder resterait en `streaming` ad vitam).
- **Pas de leak DB** : la session injectée par `Depends(get_db)` est utilisée
  pour le pré-stream (ensure + start_turn). La finalisation ouvre une
  `AsyncSessionLocal` indépendante car le lifecycle de la requête HTTP peut
  être clos pendant qu'on écrit en DB depuis le `finally`.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator

import structlog
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.budget_tracker import get_budget_tracker
from app.ai.cache import CachedResponse, get_prompt_cache
from app.ai.engine import (
    QueryEngine,
    StreamOutcome,
    observe_sse_event,
)
from app.ai.experts import resolve_model_for_pill
from app.ai.moderation import get_moderation_service
from app.ai.moderation_rules import check_business_rules
from app.ai.observability import StreamMetrics
from app.ai.providers import ChatMessage as AiChatMessage
from app.ai.providers.base import ChatUsage
from app.ai.runtime import get_ai_router, get_stream_handler
from app.ai.streaming import StreamContext, mark_cancelled
from app.ai.token_estimator import estimate as estimate_tokens
from app.ai.tools import get_tool_registry
from app.config import settings
from app.core.auth.guards import get_current_user
from app.core.database.postgres import AsyncSessionLocal, get_db
from app.core.errors.exceptions import LlmQuotaExceededException
from app.core.observability.trace import get_trace_id
from app.core.security.rate_limiter import rate_limit_abuse_reports
from app.features.auth.models import User
from app.features.chat.models import Conversation, Message
from app.features.chat.schemas import (
    AbuseReportCreate,
    AbuseReportResponse,
    ChatStopRequest,
    ChatStreamRequest,
    ConversationCreate,
    ConversationListItem,
    ConversationResponse,
    ConversationsPage,
    ConversationUpdate,
    MessageResponse,
    MessagesPage,
)
from app.features.chat.service import ConversationService, ReportService
from app.features.experts.context_builder import build_expert_corpus_context
from app.features.memory.context_builder import build_memory_context
from app.features.planner.models import ScheduledTask
from app.features.rich_content import detect_rich_content
from app.shared.schemas import NexyaResponse
from workers.chat_tasks import enqueue_title_generation
from workers.memory_tasks import (
    EXTRACTION_MIN_MESSAGES,
    enqueue_memory_extraction,
)

log = structlog.get_logger()

router = APIRouter(prefix="/chat", tags=["chat"])


# ══════════════════════════════════════════════════════════════
# CONVERSATIONS — CRUD
# ══════════════════════════════════════════════════════════════


@router.post(
    "/conversations",
    response_model=NexyaResponse[ConversationResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_conversation(
    body: ConversationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ConversationResponse]:
    """Crée une nouvelle conversation pour l'utilisateur courant.

    Retourne 201 avec la conversation complète. Le titre et `expert_id` sont
    optionnels — un expert absent prend la valeur `'general'` côté service.
    Le `conversation_id` retourné sera utilisé par le Flutter pour le
    prochain appel `POST /chat/stream`.
    """
    conversation = await ConversationService.create(body, current_user, db)
    return NexyaResponse(
        success=True,
        data=ConversationResponse.model_validate(conversation),
    )


@router.get(
    "/conversations",
    response_model=NexyaResponse[ConversationsPage],
)
async def list_conversations(
    cursor: str | None = Query(
        default=None,
        max_length=256,
        description="Curseur opaque renvoyé par la page précédente.",
    ),
    limit: int = Query(default=20, ge=1, le=50, description="Nombre d'items par page (1–50)."),
    is_archived: bool = Query(
        default=False,
        description="`false` = onglet principal, `true` = onglet Archivées.",
    ),
    is_favorite: bool | None = Query(
        default=None,
        description="`true` favoris seuls, `false` non-favoris seuls, absent = pas de filtre.",
    ),
    expert_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=32,
        description="Filtre par mode expert (ex. `computer`, `cooking`). Absent = tous experts.",
    ),
    q: str | None = Query(
        default=None,
        min_length=1,
        max_length=200,
        description=(
            "Recherche plein texte. Matche sur le titre (trigram fuzzy) "
            "ou sur le contenu d'au moins un message (tsvector français). "
            "Absent ou vide = pas de filtre."
        ),
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ConversationsPage]:
    """Liste paginée des conversations — tri récence DESC, keyset-based.

    Le Flutter appelle sans `cursor` pour la première page, puis renvoie
    `next_cursor` tel quel à chaque défilement. `next_cursor=null` signale
    la fin de l'historique. Filtre `expert_id` réservé aux écrans
    « Discussions par expert » (un seul mode à la fois). Le paramètre `q`
    active la recherche plein texte (titre trigram + FTS français sur les
    messages) sans changer la clé de tri (le curseur reste compatible).
    """
    page = await ConversationService.list_for_user(
        current_user,
        db,
        cursor=cursor,
        limit=limit,
        is_archived=is_archived,
        is_favorite=is_favorite,
        expert_id=expert_id,
        q=q,
    )
    return NexyaResponse(
        success=True,
        data=ConversationsPage(
            items=[ConversationListItem.model_validate(c) for c in page.items],
            next_cursor=page.next_cursor,
        ),
    )


@router.get(
    "/conversations/trash",
    response_model=NexyaResponse[ConversationsPage],
)
async def list_trash_conversations(
    cursor: str | None = Query(
        default=None,
        max_length=256,
        description="Curseur opaque renvoyé par la page précédente.",
    ),
    limit: int = Query(default=20, ge=1, le=50, description="Nombre d'items par page (1–50)."),
    expert_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=32,
        description="Filtre par mode expert. Absent = tous experts.",
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ConversationsPage]:
    """Liste des conversations dans la corbeille — tri `deleted_at DESC`.

    Endpoint dédié (plutôt qu'un flag `?include_deleted=true`) : la corbeille
    a une sémantique et un tri propres (récence de suppression, pas d'activité),
    et l'écran Flutter `trash_screen.dart` attend une collection isolée.

    **Ordre de déclaration critique** : cette route est **avant**
    `/conversations/{conversation_id}` pour éviter que FastAPI parse `"trash"`
    comme un UUID (422). Un déplacement vers le bas casserait l'endpoint.

    `deleted_at` est peuplé dans chaque item (contrairement à la liste active
    où il est toujours `null`).
    """
    page = await ConversationService.list_trash_for_user(
        current_user,
        db,
        cursor=cursor,
        limit=limit,
        expert_id=expert_id,
    )
    return NexyaResponse(
        success=True,
        data=ConversationsPage(
            items=[ConversationListItem.model_validate(c) for c in page.items],
            next_cursor=page.next_cursor,
        ),
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=NexyaResponse[ConversationResponse],
)
async def get_conversation(
    conversation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ConversationResponse]:
    """Détail d'une conversation — 404 si elle n'existe pas OU n'appartient
    pas à l'utilisateur courant (protection IDOR sans fuite d'information)."""
    conversation = await ConversationService.get_by_id(conversation_id, current_user, db)
    return NexyaResponse(
        success=True,
        data=ConversationResponse.model_validate(conversation),
    )


@router.patch(
    "/conversations/{conversation_id}",
    response_model=NexyaResponse[ConversationResponse],
)
async def update_conversation(
    conversation_id: uuid.UUID,
    body: ConversationUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ConversationResponse]:
    """Mise à jour partielle — seuls les champs envoyés sont modifiés.

    Sémantique PATCH stricte : un champ absent n'est pas touché, un champ
    présent est écrit (même `null` n'est pas interprété comme un effacement
    ici, puisque les champs modifiables sont soit `title` optionnel, soit
    des booléens). Payload vide = no-op (pas d'incrément d'`updated_at`).
    """
    conversation = await ConversationService.update(conversation_id, body, current_user, db)
    return NexyaResponse(
        success=True,
        data=ConversationResponse.model_validate(conversation),
    )


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Soft-delete — la conversation disparaît des listings mais reste en DB.

    Retourne 204 (No Content) — convention REST pour les suppressions idempotentes
    réussies. La suppression physique est couplée à `DELETE /user/account` (RGPD),
    qui déclenche le `ON DELETE CASCADE` au niveau FK.
    """
    await ConversationService.soft_delete(conversation_id, current_user, db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post(
    "/conversations/{conversation_id}/restore",
    response_model=NexyaResponse[ConversationResponse],
)
async def restore_conversation(
    conversation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ConversationResponse]:
    """Restaure une conversation depuis la corbeille — 200 avec la conv complète.

    Verbe REST : `POST /resource/{id}/action` pour une action non-CRUD.
    Un `PATCH /conversations/{id}` avec `deleted_at=null` aurait été ambigu
    (effacement vs restauration), et le owner check doit se faire **dans le
    monde de la corbeille** — une conv active ne doit pas pouvoir être
    « restaurée ». Le service exige donc `deleted_at IS NOT NULL`.

    404 IDOR-safe si la conversation n'existe pas, n'appartient pas à
    l'utilisateur, ou n'est pas dans la corbeille.
    """
    conversation = await ConversationService.restore(conversation_id, current_user, db)
    return NexyaResponse(
        success=True,
        data=ConversationResponse.model_validate(conversation),
    )


@router.delete(
    "/conversations/{conversation_id}/permanent",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def permanent_delete_conversation(
    conversation_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Purge définitive depuis la corbeille — SQL DELETE physique, 204.

    Two-step obligatoire : seule une conv **déjà soft-deleted** peut être
    purgée (invariant enforced côté service). Refuser une purge directe
    d'une conv active est une garantie UX (pas de « delete + purge » en
    un clic qui perdrait les messages sans filet).

    Le DELETE SQL cascade sur les messages et les abuse_reports via
    `ON DELETE CASCADE` — atomique, pas de boucle applicative.

    404 IDOR-safe si la conversation n'est pas dans la corbeille de
    l'utilisateur courant.
    """
    await ConversationService.permanent_delete(conversation_id, current_user, db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ══════════════════════════════════════════════════════════════
# MESSAGES — lecture paginée (l'écriture passe par /chat/stream)
# ══════════════════════════════════════════════════════════════


# Lifecycle fields synthétiques pour les tâches purgées / soft-deleted.
# Le frontend (TaskPreviewCard) mappe `status='deleted'` vers une carte
# « Tâche supprimée » discrète (icône + couleur neutres) — l'user a fait
# l'action volontairement OU l'admin a purgé RGPD, on ne crie pas dessus.
_DELETED_TASK_STATUS: str = "deleted"


async def _build_messages_with_live_task_status(
    messages: list[Message],
    user: User,
    db: AsyncSession,
) -> list[MessageResponse]:
    """Construit les `MessageResponse` Pydantic + enrichit `metadata_json.tool_calls`
    avec le statut **live** des `scheduled_tasks` référencées.

    Pourquoi un enrichissement à la lecture plutôt qu'une dénormalisation
    à l'écriture ? — Le `metadata_json` est figé au moment où le tool a
    été exécuté (LOT B). Le lifecycle de la tâche (idle → pending →
    running → completed/failed/paused) évolue ensuite côté worker arq
    sans notification au message d'origine. Une dénormalisation
    impliquerait un UPDATE cross-table à chaque tick du worker — coûteux
    + fragile (race conditions, vues désynchronisées sur reload). Le
    pattern « enrichir au GET messages » garantit que la carte chat
    affiche le statut RÉEL au moment de la lecture, sans pollution
    de la table `messages` ni polling tight-loop côté client.

    Pipeline :
    1. **Collecte** les `task_id` depuis `metadata_json.tool_calls[*]
       .data.task.id` sur tous les messages porteurs (parsing
       défensif strict : tout type inattendu → skip silencieux).
    2. **Court-circuit** : si aucun `task_id` → retour direct sans
       SQL supplémentaire. Cas nominal du chat texte sans tool.
    3. **UN seul SELECT** `scheduled_tasks WHERE id IN (...) AND
       user_id = user.id` — filtre IDOR-safe (un user qui forgerait un
       UUID d'une tâche d'un autre user ne verrait jamais son statut,
       elle apparaîtrait comme « supprimée »).
    4. **Patch in-place** sur une deep-copy du `metadata_json` : les 5
       champs lifecycle (`status, paused, next_run_at, last_run_at,
       run_count`). Une tâche absente du SELECT (purge RGPD physique)
       OU `deleted_at != None` (soft-delete) → `status='deleted'`
       synthétique. Le payload original côté DB reste intact (lecture
       seule, on ne mute pas `m.metadata_json`).
    5. **`MessageResponse.model_copy(update={metadata_json: enriched})`**
       préserve les autres champs validés via `from_attributes` puis
       remplace uniquement le `metadata_json` patché.

    Coût SQL : 1 requête supplémentaire avec un `IN`, optimisée par l'index
    PK sur `scheduled_tasks.id`. Pour une page de 50 messages × max
    5 tool_calls chacun ≈ 250 IDs au pire — négligeable.
    """
    # Étape 1 — collecte des task_id (parsing défensif).
    task_ids: set[uuid.UUID] = set()
    for m in messages:
        meta = m.metadata_json
        if not isinstance(meta, dict):
            continue
        tool_calls = meta.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tc in tool_calls:
            if not isinstance(tc, dict):
                continue
            data = tc.get("data")
            if not isinstance(data, dict):
                continue
            task = data.get("task")
            if not isinstance(task, dict):
                continue
            tid_raw = task.get("id")
            if not isinstance(tid_raw, str):
                continue
            try:
                task_ids.add(uuid.UUID(tid_raw))
            except (ValueError, TypeError):
                continue

    # Étape 2 — court-circuit : pas de tool_calls = pas de SQL en plus.
    if not task_ids:
        return [MessageResponse.model_validate(m) for m in messages]

    # Étape 3 — UN seul SELECT, filtre IDOR-safe (user_id = user.id).
    stmt = select(ScheduledTask).where(
        ScheduledTask.id.in_(task_ids),
        ScheduledTask.user_id == user.id,
    )
    result = await db.execute(stmt)
    live_tasks = result.scalars().all()
    task_by_id: dict[str, ScheduledTask] = {str(t.id): t for t in live_tasks}

    # Étape 4 + 5 — patch + MessageResponse.model_copy.
    enriched_items: list[MessageResponse] = []
    for m in messages:
        meta = m.metadata_json
        if not isinstance(meta, dict) or not isinstance(meta.get("tool_calls"), list):
            enriched_items.append(MessageResponse.model_validate(m))
            continue

        # Deep-copy via JSON — le metadata_json est JSONB côté DB, donc
        # 100 % JSON-serializable. Garantit qu'on ne mute pas l'ORM.
        new_meta = json.loads(json.dumps(meta))
        for tc in new_meta["tool_calls"]:
            if not isinstance(tc, dict):
                continue
            data = tc.get("data")
            if not isinstance(data, dict):
                continue
            task = data.get("task")
            if not isinstance(task, dict):
                continue
            tid = task.get("id")
            if not isinstance(tid, str):
                continue

            live_task = task_by_id.get(tid)
            if live_task is None or live_task.deleted_at is not None:
                # Purgée RGPD OU soft-deleted → statut synthétique.
                task["status"] = _DELETED_TASK_STATUS
                continue

            # Patch les 5 champs lifecycle. Le `next_run_at`/`last_run_at`
            # peuvent être None (tâche `once` exécutée ou `failed`
            # définitif → next_run_at=null).
            task["status"] = live_task.status
            task["paused"] = live_task.paused
            task["next_run_at"] = (
                live_task.next_run_at.isoformat() if live_task.next_run_at is not None else None
            )
            task["last_run_at"] = (
                live_task.last_run_at.isoformat() if live_task.last_run_at is not None else None
            )
            task["run_count"] = live_task.run_count

        enriched_items.append(
            MessageResponse.model_validate(m).model_copy(update={"metadata_json": new_meta})
        )

    return enriched_items


@router.get(
    "/conversations/{conversation_id}/messages",
    response_model=NexyaResponse[MessagesPage],
)
async def list_conversation_messages(
    conversation_id: uuid.UUID,
    cursor: str | None = Query(
        default=None,
        max_length=256,
        description="Curseur opaque renvoyé par la page précédente.",
    ),
    limit: int = Query(default=20, ge=1, le=50, description="Nombre d'items par page (1–50)."),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[MessagesPage]:
    """Historique paginé d'une conversation — tri chronologique ASC.

    Owner check effectué dans le service : un `conversation_id` inexistant
    OU appartenant à un autre utilisateur retourne 404 (jamais 403, pas de
    distinction côté client).

    **LOT C (2026-05-23)** — enrichissement live du `metadata_json.tool_calls`.
    Pour chaque message assistant porteur d'un `create_task`/`update_task`/
    `pause_task`, on patche `data.task.{status,paused,next_run_at,
    last_run_at,run_count}` avec les valeurs ACTUELLES de la table
    `scheduled_tasks` (UN seul SELECT `WHERE id IN (...)`, pas de N+1). La
    carte tâche côté Flutter affiche ainsi le statut réel à chaque
    réouverture de la conv (« Programmée » → « Terminée » après exécution,
    « Supprimée » si purgée), sans polling côté client.
    """
    page = await ConversationService.list_messages(
        conversation_id,
        current_user,
        db,
        cursor=cursor,
        limit=limit,
    )

    # Enrichissement LOT C — patch metadata_json.tool_calls avec le statut
    # live des tâches. Le helper retourne les `MessageResponse` déjà
    # construits + enrichis, dans le même ordre que `page.items`.
    items = await _build_messages_with_live_task_status(page.items, current_user, db)

    return NexyaResponse(
        success=True,
        data=MessagesPage(items=items, next_cursor=page.next_cursor),
    )


# ══════════════════════════════════════════════════════════════
# CHAT STREAM — SSE persisté (Lot 4)
# ══════════════════════════════════════════════════════════════
# Flux :
#   1. Parse body → choisir le mode (nouveau persisté / existant / legacy)
#   2. Budget + modération (pré-stream — erreurs via JSON, pas en SSE)
#   3. Pour les modes persistés :
#      - ensure_conversation_for_stream (IDOR-safe)
#      - load_context_messages (si conversation_id fourni)
#      - start_stream_turn : insère user + placeholder assistant (streaming)
#   4. StreamingResponse avec wrapper générateur :
#      - yield les événements SSE du StreamHandler au client
#      - accumule les deltas pour `Message.content` final
#      - finalize_assistant_stream dans le `finally`, session fraîche, shieldée
# ══════════════════════════════════════════════════════════════

# Seuil d'auto-titre : on enqueue dès que la conversation a au moins
# 2 messages `completed` (= 1 paire user/assistant terminée). Le « >= »
# plutôt que « == » est volontaire — si l'enqueue d'un tour précédent
# a échoué (Redis flap, bug arq), un tour ultérieur déclenche une nouvelle
# tentative ; la sentinelle `title_generated_at` en DB protège du doublon.
#
# **2026-05-15 fix** — abaissé de 4 à 2 après retour terrain Ivan : les
# users voyaient le placeholder « Nouvelle discussion » trop longtemps
# (jusqu'à 2 tours complets avant que le titre auto soit déclenché).
# Avec un seuil à 2 (1 paire user/assistant en `status=completed`), le
# titre apparaît dès la première vraie réponse IA. `generate_conversation_title`
# worker exige aussi `len(rows) >= 2` (cf. workers/chat_tasks.py:193),
# cohérence end-to-end respectée. Coût supplémentaire estimé : ~3× l'actuel
# (~$475/mois → ~$1500/mois worst-case 950k users × 1 conv/jour), acceptable
# car on génère uniquement sur conv finalisées (pas cancelled/failed).
_TITLE_AUTOGENERATE_THRESHOLD = 2


@router.post("/stream")
async def chat_stream(
    body: ChatStreamRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """SSE streaming — orchestre budget → modération → persistance → stream IA.

    Contrat SSE en sortie :
    - `event: chunk`     `{delta, finish_reason?, usage?}`
    - `event: keepalive` (commentaire `:` toutes les 15 s — anti-coupure 2G/3G)
    - `event: error`     `{code, message}` — codes NEXYA (LLM_UNAVAILABLE, STREAM_CANCELLED, CONTENT_FILTERED…)
    - `event: done`      `{reason, duration_ms}` — toujours émis en dernier

    Headers de réponse :
    - `X-Session-Id`       : ID utilisable par le client pour appeler `/chat/stop`.
    - `X-Conversation-Id`  : UUID de la conversation touchée (modes persistés
                             uniquement). Permet au Flutter de naviguer vers
                             l'historique sans rappel supplémentaire.
    """
    user_id_str = str(current_user.id)
    trace_id = get_trace_id() or uuid.uuid4().hex
    session_id = body.session_id or uuid.uuid4().hex

    # ── 1. Budget : cap absolu user/jour (pré-consommation) ──────────
    await get_budget_tracker().check_and_consume_chat(user_id_str)

    # ── 2. Modération OpenAI du prompt (fail-open si clé absente) ────
    decision = await get_moderation_service().check(
        body.message, kind="input", user_id=user_id_str, trace_id=trace_id
    )
    if not decision.allowed:
        return JSONResponse(
            status_code=400,
            content=NexyaResponse(
                success=False,
                error="Ta requête a été bloquée par le filtre de sécurité.",
                code="CONTENT_FILTERED",
            ).model_dump(mode="json"),
        )

    # ── 3. Règles de modération métier B2 (prescription, acte juridique) ─
    # Appliqué APRÈS la modération générique pour laisser OpenAI catch le
    # contenu toxique, puis appliquer nos règles spécifiques NEXYA.
    rules_decision = check_business_rules(
        text=body.message,
        expert_id=body.expert_id or "general",
        kind="input",
    )
    if not rules_decision.allowed:
        return JSONResponse(
            status_code=400,
            content=NexyaResponse(
                success=False,
                error=rules_decision.message or "Cette requête sort du cadre autorisé pour NEXYA.",
                code="CONTENT_FILTERED",
                data={"rule": rules_decision.reason},
            ).model_dump(mode="json"),
        )

    # ── 4. Résolution de la chaîne IA pour expert_id → provider/model ─
    # Nécessaire pour estimer les tokens et construire la clé de cache.
    resolution = get_ai_router().resolve(body.expert_id)
    config = resolution.config

    # ── 4.5. F2.5 — Tools LLM (function calling). Injection des 4 tools
    # Planner si (a) le kill-switch global est ON et (b) l'expert courant
    # autorise les tools (`tools_allowed=True`, False par défaut sur
    # `medicine` et `legal`). `build_openai_tools()` produit le format
    # natif OpenAI `[{type:function, function:{name,description,parameters}}]`,
    # consommé tel quel par le provider OpenAI/Qwen, ré-écrit en
    # `input_schema` côté Anthropic et `function_declarations` côté
    # Gemini par les helpers privés des providers. `tools_for_request`
    # peut être None ou liste vide → le `StreamContext.tools` reste None
    # et les providers se comportent comme F2 (sans tools).
    tools_for_request: list[dict] | None = None
    if settings.tools_enabled_in_chat and config.tools_allowed:
        registry_tools = get_tool_registry().build_openai_tools()
        if registry_tools:
            tools_for_request = registry_tools

    # ── 4.6. Model pills (2026-05-23) — résolution UI → backend.
    # Si l'utilisateur a sélectionné une pill (GEEK/LOTH/JUSTO) avant
    # d'envoyer son message, on résout vers `(model_name, disable_thinking)`
    # selon `ExpertConfig.model_pill_mapping` (matrice 11 experts × 3 pills).
    # Pill absente / inconnue / studio → fail-safe `(None, None)` → le
    # `StreamContext` garde ses override à None et `_run_link` utilise la
    # config legacy A1+A2 (config.primary_model + config.disable_thinking).
    # Voir [experts.py::resolve_model_for_pill] pour la matrice complète.
    pill_model_override, pill_thinking_override = resolve_model_for_pill(
        body.expert_id, body.model_pill
    )
    if body.model_pill and pill_model_override is None:
        # Pill envoyée mais non résolue (studio image-only, ou pill inconnue
        # passée par un client buggé). Log debug uniquement — pas d'erreur
        # user-visible, on retombe sur le comportement legacy.
        log.debug(
            "ai.chat.pill_unresolved",
            user_id=user_id_str,
            expert_id=body.expert_id,
            pill=body.model_pill,
        )

    # ── 5. Préparation des messages IA selon le mode ────────────────
    is_legacy_stateless = body.conversation_id is None and bool(body.history)
    response_headers: dict[str, str] = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
        "X-Session-Id": session_id,
    }

    # Compose la liste finale des `ChatMessage` qui servira à l'estimation
    # tokens + clé de cache. Le mode persisté ajoute l'historique DB, le mode
    # legacy utilise `body.history`.
    if is_legacy_stateless:
        ai_messages = _build_ai_messages_from_history(body)
        conversation = None
        placeholder = None
    else:
        conversation = await ConversationService.ensure_conversation_for_stream(
            body.conversation_id,
            current_user,
            db,
            expert_id_hint=body.expert_id,
            project_id=body.project_id,
            # [Bug-040 stable fix 2026-05-15] Pose un titre déterministe au
            # INSERT de la nouvelle conv (uniquement mode new, conversation_id
            # None). Évite définitivement le placeholder « Nouvelle discussion »
            # + les titres LLM dégénérés. Voir docstring helper.
            first_message=body.message,
        )
        context_messages = await ConversationService.load_context_messages(conversation, db)
        ai_messages = list(context_messages)
        ai_messages.append(AiChatMessage(role="user", content=body.message))

    # ── 5.5. D3 — Récupération des memories pertinentes ──────────────
    # L'injection mémoire IA se fait AVANT le token estimator pour que
    # le cap 30 000 tokens (B2) prenne en compte le bloc mémoire injecté.
    # Fail-safe absolue : `build_memory_context` catche toute exception
    # en interne et retourne `None` si la recherche échoue (pgvector
    # lent, embeddings API down, budget embeddings dépassé). Le chat
    # ne doit JAMAIS être bloqué par un dysfonctionnement mémoire.
    memory_context = await build_memory_context(current_user, db, query=body.message)

    # ── 5.6. G1 — Récupération des chunks corpus expert pertinents ──
    # Même discipline que D3 : fail-safe absolue, shortcut si
    # `config.corpus_enabled=False` OU si le kill-switch global
    # `settings.expert_corpus_enabled=False`. Consomme 1 embed query
    # Gemini (task_type=RETRIEVAL_QUERY, ~$0 dans le quota gratuit).
    expert_corpus_context: str | None = None
    if config.corpus_enabled:
        expert_corpus_context = await build_expert_corpus_context(
            expert_slug=config.expert_id,
            query=body.message,
            db=db,
        )

    # ── 5.7. I1 (2026-05-05) — Bloc RAG documents user pré-calculé front
    # Le frontend appelle `POST /rag/query` D5 AVANT `/chat/stream` quand
    # `projectId != null` et qu'au moins 1 fichier RAG-eligible (PDF/DOCX/
    # TXT/MD avec `chunks_indexed_at != None`) est rattaché. Le résultat
    # est transmis dans le body `body.rag_context = {framed_context, instruction}`.
    # `None` = pas d'injection RAG (mode legacy strictement préservé,
    # rétrocompat A1+B1+B2+B3+B4+G1).
    rag_context_tuple: tuple[str, str] | None = None
    rag_block_for_check: str | None = None
    if body.rag_context is not None:
        rag_context_tuple = (body.rag_context.framed_context, body.rag_context.instruction)
        rag_block_for_check = f"{body.rag_context.framed_context}\n\n{body.rag_context.instruction}"

    # Pour le token estimator + cache key, on compose localement le
    # system_prompt final dans le même ordre que `_stream_link` :
    # memory → corpus → rag → expert. La concat définitive est refaite
    # dans `_stream_link` à partir des champs `ctx.memory_context` +
    # `ctx.expert_corpus_context` + `ctx.rag_context` — Single Source of Truth.
    _prompt_parts = [
        memory_context,
        expert_corpus_context,
        rag_block_for_check,
        config.system_prompt or None,
    ]
    system_prompt_for_check = "\n\n".join(p for p in _prompt_parts if p)

    # ── 6. Estimation tokens pré-appel + cap anti-abus (402) ────────
    # On estime AVANT toute écriture DB pour qu'un user qui dépasse le cap
    # soit refusé sans que son message user soit persisté (expérience plus
    # propre : pas de conversation orpheline en DB avec un placeholder failed).
    estimate = estimate_tokens(
        provider=resolution.provider.name,
        model=resolution.model,
        messages=ai_messages,
        system_prompt=system_prompt_for_check,
        max_tokens=config.max_tokens,
    )
    if estimate.prompt_tokens > settings.chat_prompt_tokens_per_request_max:
        log.warning(
            "ai.chat.prompt_tokens_over_cap",
            user_id=user_id_str,
            trace_id=trace_id,
            expert_id=body.expert_id,
            provider=resolution.provider.name,
            model=resolution.model,
            prompt_tokens=estimate.prompt_tokens,
            cap=settings.chat_prompt_tokens_per_request_max,
        )
        raise LlmQuotaExceededException()

    # ── 7. Cache prompt : lookup avant stream (brique B2) ────────────
    # Seul le mode legacy stateless est cachable pour B2 — le mode persisté
    # demanderait d'insérer le message user + placeholder et de simuler
    # un stream après hit, complexe à valider en un lot. La prochaine
    # itération étendra le cache au mode persisté une fois le legacy
    # retiré (contrat Flutter figé sur l'historique côté serveur).
    cache = get_prompt_cache()
    cache_key: str | None = None
    if is_legacy_stateless and cache.is_cacheable(config, ai_messages):
        cache_key = cache.build_key(
            model=resolution.model,
            messages=ai_messages,
            system_prompt=system_prompt_for_check,
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            expert_id=config.expert_id,
        )
        hit = await cache.get(cache_key)
        if hit is not None:
            log.info(
                "ai.cache.hit",
                user_id=user_id_str,
                trace_id=trace_id,
                expert_id=config.expert_id,
                provider=hit.provider,
                model=hit.model,
                cache_key=cache_key,
            )
            return StreamingResponse(
                _replay_cached_stream(hit, trace_id=trace_id),
                media_type="text/event-stream",
                headers={**response_headers, "X-Cache": "HIT"},
            )

    # ── 8a. Stream legacy stateless (pas de persistance DB) ──────────
    if is_legacy_stateless:
        ctx = StreamContext(
            expert_id=body.expert_id,
            user_messages=ai_messages,
            user_id=user_id_str,
            trace_id=trace_id,
            session_id=session_id,
            memory_context=memory_context,
            expert_corpus_context=expert_corpus_context,
            rag_context=rag_context_tuple,
            tools=tools_for_request,
            # [planner-from-chat LOT 1] — exécution serveur des tools.
            user=current_user,
            db_session_factory=AsyncSessionLocal,
            # planner-from-chat tz-fix (2026-05-23) — offset ISO du
            # client (Flutter `DateTime.now().timeZoneOffset`). Permet
            # au LLM d'interpréter « 20h » comme heure LOCALE.
            client_timezone=body.client_timezone,
            # Model pills (2026-05-23) — overrides résolus depuis
            # `body.model_pill` (None si pas de pill ou pill non-résolue).
            pill_model_override=pill_model_override,
            pill_disable_thinking_override=pill_thinking_override,
        )
        handler = get_stream_handler()
        return StreamingResponse(
            _legacy_stream_with_cache_put(
                handler=handler,
                request=request,
                ctx=ctx,
                cache_key=cache_key,
                provider_name=resolution.provider.name,
                model=resolution.model,
            ),
            media_type="text/event-stream",
            headers={**response_headers, "X-Cache": "MISS" if cache_key else "BYPASS"},
        )

    # ── 8b. Stream persisté : user + placeholder déjà insérés ───────
    assert conversation is not None  # garanti par le branchement ci-dessus
    _user_msg, placeholder = await ConversationService.start_stream_turn(
        conversation, body.message, db
    )

    metrics = StreamMetrics(
        user_id=user_id_str,
        trace_id=trace_id,
        expert_id=conversation.expert_id,
        session_id=session_id,
    )
    ctx = StreamContext(
        expert_id=conversation.expert_id,
        user_messages=ai_messages,
        user_id=user_id_str,
        trace_id=trace_id,
        session_id=session_id,
        metrics=metrics,
        memory_context=memory_context,
        expert_corpus_context=expert_corpus_context,
        rag_context=rag_context_tuple,
        tools=tools_for_request,
        # [planner-from-chat LOT 1] — exécution serveur des tools.
        user=current_user,
        db_session_factory=AsyncSessionLocal,
        # planner-from-chat tz-fix (2026-05-23) — offset ISO du client.
        client_timezone=body.client_timezone,
        # Model pills (2026-05-23) — overrides résolus depuis
        # `body.model_pill` (None si pas de pill ou pill non-résolue).
        pill_model_override=pill_model_override,
        pill_disable_thinking_override=pill_thinking_override,
    )

    response_headers["X-Conversation-Id"] = str(conversation.id)
    # C2-fix (2026-05-02) — UUID backend du message assistant fraîchement
    # persisté en DB par `start_stream_turn` ci-dessus. Permet au client
    # Flutter de cibler ce message pour `POST /chat/messages/{id}/feedback`
    # (C2) et `POST /chat/reports` (C1) sans devoir GET la liste des
    # messages post-stream. Aligné pattern `X-Session-Id`/`X-Conversation-Id`.
    # Émis UNIQUEMENT en mode persisté (mode legacy stateless n'INSERT pas
    # de row, mode cache HIT court-circuite avant `start_stream_turn`).
    response_headers["X-Assistant-Message-Id"] = str(placeholder.id)

    handler = get_stream_handler()
    return StreamingResponse(
        _persisted_stream(
            handler=handler,
            request=request,
            ctx=ctx,
            metrics=metrics,
            assistant_message_id=placeholder.id,
            conversation_id=conversation.id,
            user_message=body.message,
        ),
        media_type="text/event-stream",
        headers=response_headers,
    )


@router.post("/stop", response_model=NexyaResponse[dict])
async def chat_stop(
    body: ChatStopRequest,
    current_user: User = Depends(get_current_user),
) -> NexyaResponse[dict]:
    """Pose la clé d'annulation Redis. Le stream actif côté serveur la lit
    dans la seconde et coupe le flux proprement (SSE `error STREAM_CANCELLED`
    + `done reason=cancelled`), ce qui déclenche la finalisation du message
    avec `status='cancelled'`."""
    await mark_cancelled(body.session_id)
    return NexyaResponse(success=True, data={"session_id": body.session_id, "cancelled": True})


# ══════════════════════════════════════════════════════════════
# ABUSE REPORTS — POST /chat/reports
# ══════════════════════════════════════════════════════════════


@router.post(
    "/reports",
    response_model=NexyaResponse[AbuseReportResponse],
    status_code=status.HTTP_201_CREATED,
)
async def create_abuse_report(
    body: AbuseReportCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[AbuseReportResponse]:
    """Signalement d'un message — exigence App Store §1.2 / Play §I.

    Pipeline défensif :

    1. **Rate limit user-scoped** (`RATE_LIMIT_ABUSE`, 10/heure/utilisateur)
       — distinct du rate limit IP : un user qui spamme le bouton
       « Signaler » n'est pas la même chose qu'un brute-force d'IP.
    2. **Owner check** sur le message cible via JOIN
       (Message.id + Conversation.user_id), 404 IDOR-safe en cas de
       mismatch — pas de 403 (anti-énumération d'UUID valides).
    3. **Insertion + détection de doublon** : la contrainte UNIQUE
       `(user_id, message_id)` garantit l'idempotence côté DB. Un
       second tap retourne 409 (`DUPLICATE_REPORT`) avec un message
       neutre — le Flutter affiche un toast « déjà signalé » sans
       erreur rouge.
    """
    await rate_limit_abuse_reports(current_user.id)
    report = await ReportService.create_report(current_user, body, db)
    return NexyaResponse(
        success=True,
        data=AbuseReportResponse.model_validate(report),
    )


# ══════════════════════════════════════════════════════════════
# HELPERS — stream persisté
# ══════════════════════════════════════════════════════════════


def _build_ai_messages_from_history(body: ChatStreamRequest) -> list[AiChatMessage]:
    """Convertit `body.history + body.message` au format IA (chemin legacy)."""
    messages: list[AiChatMessage] = []
    for h in body.history:
        role = _coerce_role(h.role)
        messages.append(AiChatMessage(role=role, content=h.content))  # type: ignore[arg-type]
    messages.append(AiChatMessage(role="user", content=body.message))
    return messages


def _coerce_role(role: str) -> str:
    """Normalise le rôle envoyé par le Flutter : `'ai' | 'bot' | 'model'`
    → `'assistant'`. Tout rôle inconnu retombe sur `'user'` pour éviter
    d'injecter un `role=system` non voulu depuis un client mal formé."""
    if role in ("user", "system", "assistant"):
        return role
    if role in ("ai", "bot", "model"):
        return "assistant"
    return "user"


async def _persisted_stream(
    *,
    handler,
    request: Request,
    ctx: StreamContext,
    metrics: StreamMetrics,
    assistant_message_id: uuid.UUID,
    conversation_id: uuid.UUID,
    user_message: str,
) -> AsyncIterator[str]:
    """Enveloppe persistance autour du générateur SSE du StreamHandler.

    Flux :
    - Relaye les événements SSE au client tel quel (pas de réécriture).
    - En parallèle : accumule `delta` des événements `chunk`, capture la
      raison finale dans `done`, mémorise le code d'erreur dans `error`.
    - Dans le `finally` : ouvre une session DB FRAÎCHE
      (`AsyncSessionLocal`) — la session de la requête est peut-être déjà
      en rollback ou fermée si le client s'est déconnecté — et appelle
      `finalize_assistant_stream`.
    - `asyncio.shield` protège la finalisation de la cancellation : si le
      client coupe sa connexion pendant l'écriture DB, la tâche continue
      jusqu'à son terme (on paie au pire un `CancelledError` au return
      du générateur, jamais un placeholder `streaming` orphelin en DB).

    `content` enregistré = concaténation de tous les `delta` reçus. Un
    stream avorté (failed / cancelled) conserve donc la partie déjà
    générée par l'IA, visible dans l'historique comme preuve de ce qui
    a été rendu à l'utilisateur. Alignement RGPD : le contenu affiché
    côté client est le contenu stocké.
    """
    outcome = StreamOutcome()
    engine = QueryEngine(handler=handler)
    try:
        async for event in engine.run(request, ctx, outcome=outcome):
            yield event
    finally:
        await asyncio.shield(
            _finalize_in_fresh_session(
                assistant_message_id=assistant_message_id,
                conversation_id=conversation_id,
                outcome=outcome,
                metrics=metrics,
                user_message=user_message,
            )
        )


async def _finalize_in_fresh_session(
    *,
    assistant_message_id: uuid.UUID,
    conversation_id: uuid.UUID,
    outcome: StreamOutcome,
    metrics: StreamMetrics,
    user_message: str,
) -> None:
    """Ouvre une session DB indépendante et finalise le placeholder.

    Session FRAÎCHE (pas celle du `Depends(get_db)` de la requête) pour être
    robuste à un disconnect client pendant le stream : la session HTTP est
    alors en train d'être roll-backée par le middleware, on ne peut plus
    écrire dessus. Une `AsyncSessionLocal` neuve nous donne une transaction
    propre qui ne dépend pas du lifecycle de la requête.

    Aucune exception ne remonte : un échec de finalisation DB est loggé
    mais ne re-raise pas — on est déjà dans un `finally` shieldé, propager
    une erreur ici masquerait un éventuel `CancelledError` légitime du
    caller.
    """
    status_final = outcome.final_status()
    content = outcome.final_content()
    usage = metrics.usage

    # planner-from-chat — instantané des tool calls exécutés pendant le
    # stream, persisté dans `messages.metadata_json` pour que la carte de
    # tâche du chat survive à la réouverture de la conversation. `None`
    # quand aucun tool n'a tourné (cas nominal du chat texte).
    metadata_json: dict | None = (
        {"tool_calls": outcome.tool_results} if outcome.tool_results else None
    )

    # C4.4 — détection automatique d'un brouillon email/WhatsApp dans la
    # réponse assistante finale. Fail-safe absolu : exception du détecteur
    # → log warning + skip, la finalisation chat continue sans
    # `rich_content`. Pas de carte vs faux positif → trade-off conservateur.
    if status_final == "completed" and content:
        try:
            rich = detect_rich_content(user_message, content)
        except Exception as exc:  # noqa: BLE001
            rich = None
            log.warning(
                "chat.stream.rich_content_detection_failed",
                assistant_message_id=str(assistant_message_id),
                error=str(exc),
                error_type=type(exc).__name__,
            )
        if rich is not None:
            if metadata_json is None:
                metadata_json = {}
            metadata_json["rich_content"] = rich

    should_enqueue_title = False
    should_enqueue_memory_extraction = False
    try:
        async with AsyncSessionLocal() as db:
            await ConversationService.finalize_assistant_stream(
                assistant_message_id,
                conversation_id,
                db,
                content=content,
                status=status_final,
                provider=metrics.provider or None,
                model=metrics.model or None,
                prompt_tokens=usage.prompt_tokens if usage else None,
                completion_tokens=usage.completion_tokens if usage else None,
                total_tokens=usage.total_tokens if usage else None,
                cost_usd=metrics.cost_usd if usage else None,
                error_code=outcome.error_code,
                metadata_json=metadata_json,
            )
            # Décide d'enqueuer le titre auto une fois la finalisation
            # commitée — on ne déclenche que sur une fin propre, et tant
            # que la sentinelle DB n'a pas été posée par un précédent run.
            #
            # **2026-05-15 — Bug-040 stable fix** : depuis l'introduction
            # du titre déterministe au INSERT (cf.
            # `ConversationService.ensure_conversation_for_stream`), les
            # 2 conditions `title_generated_at IS NULL AND title IS NULL`
            # sont **toujours false** sur les conv créées via /chat/stream
            # → l'enqueue est naturellement no-op (gratuit, pas de Redis
            # call gaspillé). Le code reste pour V2 raffinement LLM
            # conditionnel (ex: conv >= 50 messages, l'IA peut générer
            # un meilleur titre que le déterministe).
            if status_final == "completed":
                conv = await db.get(Conversation, conversation_id)
                should_enqueue_title = bool(
                    conv
                    and conv.title_generated_at is None
                    and conv.title is None
                    and conv.message_count >= _TITLE_AUTOGENERATE_THRESHOLD
                )
                # Décide d'enqueuer l'extraction de faits durables (D2)
                # — mêmes principes que le titre auto :
                # - stream finalisé proprement uniquement,
                # - sentinelle `memory_extracted_at` pas encore posée,
                # - seuil `>= EXTRACTION_MIN_MESSAGES` (6 messages =
                #   3 tours user/assistant minimum pour avoir du signal
                #   exploitable par le LLM extractif).
                # Seuil `>=` (pas `==`) : si l'enqueue du précédent run
                # a raté, un tour ultérieur déclenche un nouveau essai,
                # la sentinelle protège du double travail.
                should_enqueue_memory_extraction = bool(
                    conv
                    and conv.memory_extracted_at is None
                    and conv.message_count >= EXTRACTION_MIN_MESSAGES
                )
    except Exception as exc:  # noqa: BLE001
        log.error(
            "chat.stream.finalize_failed",
            assistant_message_id=str(assistant_message_id),
            conversation_id=str(conversation_id),
            status=status_final,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return

    if should_enqueue_title:
        # Hors du `with` pour ne pas tenir la transaction ouverte le temps
        # de l'I/O Redis. L'enqueue fail-safe : `enqueue_title_generation`
        # log et avale l'erreur si Redis est down.
        await enqueue_title_generation(conversation_id)

    if should_enqueue_memory_extraction:
        # Même discipline fail-safe : `enqueue_memory_extraction` log et
        # avale l'erreur si Redis est down (l'extraction est cosmétique,
        # jamais bloquante pour l'utilisateur).
        await enqueue_memory_extraction(conversation_id)


# ══════════════════════════════════════════════════════════════
# HELPERS — cache prompt (brique B2)
# ══════════════════════════════════════════════════════════════


def _sse_chunk(delta: str) -> str:
    """Format SSE `event: chunk` avec un delta texte — aligné sur `_sse` de `streaming.py`."""
    payload = json.dumps({"delta": delta}, ensure_ascii=False, separators=(",", ":"))
    return f"event: chunk\ndata: {payload}\n\n"


def _sse_done(reason: str, *, usage: dict | None = None) -> str:
    """Format SSE `event: done` — `reason` toujours présent, `usage` si connu."""
    data: dict[str, object] = {"reason": reason}
    if usage is not None:
        data["usage"] = usage
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: done\ndata: {payload}\n\n"


async def _replay_cached_stream(
    hit: CachedResponse,
    *,
    trace_id: str,
) -> AsyncIterator[str]:
    """Rejoue une réponse cachée sous forme de SSE — single chunk + done.

    On ne reconstitue pas la séquence originale de chunks (impossible :
    seule la réponse finale est stockée). Le Flutter reçoit donc le texte
    entier dans un unique événement `chunk`, puis un `done reason=stop`.
    Cohérent avec le contrat SSE actuel — le client n'assume aucune
    granularité particulière sur les deltas.
    """
    usage_payload = {
        "prompt_tokens": hit.prompt_tokens,
        "completion_tokens": hit.completion_tokens,
        "total_tokens": hit.total_tokens,
    }
    log.info(
        "ai.cache.replayed",
        trace_id=trace_id,
        provider=hit.provider,
        model=hit.model,
        total_tokens=hit.total_tokens,
    )
    yield _sse_chunk(hit.text)
    yield _sse_done("stop", usage=usage_payload)


async def _legacy_stream_with_cache_put(
    *,
    handler,
    request: Request,
    ctx: StreamContext,
    cache_key: str | None,
    provider_name: str,
    model: str,
) -> AsyncIterator[str]:
    """Enveloppe le stream legacy pour capturer le texte final et le mettre
    en cache à la fin — si et seulement si la génération s'est terminée
    proprement (done reason = stop, pas d'erreur).

    Contrairement au mode persisté, on ne finalise rien en DB ici — on
    accumule juste les deltas localement et on appelle `cache.put()` à
    la fin. `cache.put()` est fail-open (Redis down → log + no-op).
    """
    outcome = StreamOutcome()
    finish_reason: str | None = None
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    # Pas de try/finally ici : si le stream est interrompu par une exception
    # ou un disconnect client, la boucle s'arrête et on ne cache rien — c'est
    # exactement ce qu'on veut (on ne cache que les streams cleanly terminés).
    async for event in handler.stream(request, ctx):
        yield event
        observe_sse_event(event, outcome)
        # Tente d'extraire `finish_reason` + `usage` du dernier chunk.
        if event.startswith("event: chunk"):
            _, _, data_line = event.partition("data: ")
            data_str = data_line.split("\n", 1)[0] if data_line else ""
            if data_str:
                try:
                    payload = json.loads(data_str)
                except (ValueError, TypeError):
                    payload = None
                if isinstance(payload, dict):
                    fr = payload.get("finish_reason")
                    if isinstance(fr, str):
                        finish_reason = fr
                    usage = payload.get("usage")
                    if isinstance(usage, dict):
                        prompt_tokens = int(usage.get("prompt_tokens", 0) or 0)
                        completion_tokens = int(usage.get("completion_tokens", 0) or 0)
                        total_tokens = int(
                            usage.get("total_tokens", prompt_tokens + completion_tokens) or 0
                        )

    # Stream terminé naturellement. On décide du cache-put ci-dessous.
    if cache_key is None:
        return
    # Ne cache QUE les streams cleanly terminés (done reason=stop,
    # pas d'erreur, pas de troncature). `cache.put` re-vérifie
    # aussi ces invariants — ceinture + bretelles.
    if outcome.done_reason != "stop" or outcome.error_code:
        return
    content = "".join(outcome.content_parts)
    if not content.strip():
        return
    usage_obj = ChatUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=total_tokens or (prompt_tokens + completion_tokens),
    )
    cache = get_prompt_cache()
    try:
        await cache.put(
            cache_key,
            text=content,
            provider=provider_name,
            model=model,
            usage=usage_obj,
            status="completed",
            error_code=None,
            finish_reason=finish_reason,
        )
        log.info(
            "ai.cache.stored",
            trace_id=ctx.trace_id,
            provider=provider_name,
            model=model,
            cache_key=cache_key,
            total_tokens=usage_obj.total_tokens,
        )
    except Exception as exc:  # noqa: BLE001 — fail-open
        log.warning(
            "ai.cache.store_failed",
            trace_id=ctx.trace_id,
            cache_key=cache_key,
            error=str(exc),
        )


# ══════════════════════════════════════════════════════════════════
# Session N1 — Feedback chat (thumbs up/down)
# ══════════════════════════════════════════════════════════════════

from app.features.feedback.schemas import (  # noqa: E402
    FeedbackCreate,
    FeedbackResponse,
)
from app.features.feedback.service import FeedbackService  # noqa: E402


@router.post(
    "/messages/{message_id}/feedback",
    response_model=NexyaResponse[FeedbackResponse],
    status_code=status.HTTP_201_CREATED,
)
async def post_feedback(
    message_id: uuid.UUID,
    body: FeedbackCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[FeedbackResponse]:
    """UPSERT atomique du feedback (thumbs up/down) sur un message.

    Idempotent : re-poste même rating = no-op DB-level. Change rating =
    update via `on_conflict_do_update`.
    """
    row = await FeedbackService.record_feedback(current_user, message_id, body, db)
    return NexyaResponse(success=True, data=FeedbackResponse.model_validate(row))


@router.delete(
    "/messages/{message_id}/feedback",
    status_code=status.HTTP_204_NO_CONTENT,
)
async def delete_feedback(
    message_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Annule le feedback (idempotent — 204 même si pas de row).

    Anti-énumération : ne distingue pas « j'avais un feedback » vs
    « j'en avais pas » pour un message d'un autre user.
    """
    await FeedbackService.delete_feedback(current_user, message_id, db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
