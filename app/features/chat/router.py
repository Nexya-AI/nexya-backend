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
from dataclasses import dataclass, field

import structlog
from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.budget_tracker import get_budget_tracker
from app.ai.moderation import get_moderation_service
from app.ai.observability import StreamMetrics
from app.ai.providers import ChatMessage as AiChatMessage
from app.ai.runtime import get_stream_handler
from app.ai.streaming import StreamContext, mark_cancelled
from app.core.auth.guards import get_current_user
from app.core.database.postgres import AsyncSessionLocal, get_db
from app.core.observability.trace import get_trace_id
from app.core.security.rate_limiter import rate_limit_abuse_reports
from app.features.auth.models import User
from app.features.chat.models import Conversation
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
from app.shared.schemas import NexyaResponse
from workers.chat_tasks import enqueue_title_generation

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
    limit: int = Query(
        default=20, ge=1, le=50, description="Nombre d'items par page (1–50)."
    ),
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
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[ConversationsPage]:
    """Liste paginée des conversations — tri récence DESC, keyset-based.

    Le Flutter appelle sans `cursor` pour la première page, puis renvoie
    `next_cursor` tel quel à chaque défilement. `next_cursor=null` signale
    la fin de l'historique. Filtre `expert_id` réservé aux écrans
    « Discussions par expert » (un seul mode à la fois).
    """
    page = await ConversationService.list_for_user(
        current_user,
        db,
        cursor=cursor,
        limit=limit,
        is_archived=is_archived,
        is_favorite=is_favorite,
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
    "/conversations/trash",
    response_model=NexyaResponse[ConversationsPage],
)
async def list_trash_conversations(
    cursor: str | None = Query(
        default=None,
        max_length=256,
        description="Curseur opaque renvoyé par la page précédente.",
    ),
    limit: int = Query(
        default=20, ge=1, le=50, description="Nombre d'items par page (1–50)."
    ),
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
    conversation = await ConversationService.get_by_id(
        conversation_id, current_user, db
    )
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
    conversation = await ConversationService.update(
        conversation_id, body, current_user, db
    )
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
    conversation = await ConversationService.restore(
        conversation_id, current_user, db
    )
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
    limit: int = Query(
        default=20, ge=1, le=50, description="Nombre d'items par page (1–50)."
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[MessagesPage]:
    """Historique paginé d'une conversation — tri chronologique ASC.

    Owner check effectué dans le service : un `conversation_id` inexistant
    OU appartenant à un autre utilisateur retourne 404 (jamais 403, pas de
    distinction côté client).
    """
    page = await ConversationService.list_messages(
        conversation_id,
        current_user,
        db,
        cursor=cursor,
        limit=limit,
    )
    return NexyaResponse(
        success=True,
        data=MessagesPage(
            items=[MessageResponse.model_validate(m) for m in page.items],
            next_cursor=page.next_cursor,
        ),
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

# Mapping SSE `done.reason` → `Message.status` final (aligné sur le CHECK SQL).
_DONE_REASON_TO_STATUS: dict[str, str] = {
    "stop": "completed",
    "cancelled": "cancelled",
    "error": "failed",
}

# Seuil d'auto-titre : on enqueue dès que la conversation a au moins 4
# messages `completed` (≈ 2 tours user/assistant). Le « >= » plutôt que
# « == » est volontaire — si l'enqueue d'un tour précédent a échoué (Redis
# flap, bug arq), un tour ultérieur déclenche une nouvelle tentative ; la
# sentinelle `title_generated_at` en DB protège du doublon de titre.
_TITLE_AUTOGENERATE_THRESHOLD = 4


@dataclass(slots=True)
class _StreamOutcome:
    """Accumulateur mutable partagé entre le scan SSE et la finalisation.

    - `done_reason` : dernière raison vue dans un événement `done`. Si on n'en
      voit jamais (cas d'un disconnect avant la fin), on retombe sur le défaut
      `'error'` → status `failed`.
    - `error_code` : code d'erreur vu dans le dernier événement `error` (pris
      comme `error_code` final si on termine en échec ou annulation).
    """

    done_reason: str = "error"
    error_code: str | None = None
    content_parts: list[str] = field(default_factory=list)


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

    # ── 2. Modération du prompt utilisateur (fail-open si clé absente) ─
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

    # ── 3. Décision de mode : persisté vs legacy stateless ───────────
    is_legacy_stateless = body.conversation_id is None and bool(body.history)
    response_headers: dict[str, str] = {
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
        "Connection": "keep-alive",
        "X-Session-Id": session_id,
    }

    if is_legacy_stateless:
        # Chemin legacy : pas de persistance. Le message n'entre jamais en DB.
        # Gardé pour la compat du Flutter actuel qui envoie toujours `history`.
        ai_messages = _build_ai_messages_from_history(body)
        ctx = StreamContext(
            expert_id=body.expert_id,
            user_messages=ai_messages,
            user_id=user_id_str,
            trace_id=trace_id,
            session_id=session_id,
        )
        handler = get_stream_handler()
        return StreamingResponse(
            handler.stream(request, ctx),
            media_type="text/event-stream",
            headers=response_headers,
        )

    # ── 4. Résolution de la conversation cible (persistance active) ──
    conversation = await ConversationService.ensure_conversation_for_stream(
        body.conversation_id,
        current_user,
        db,
        expert_id_hint=body.expert_id,
    )

    # Historique : on rejoue toujours la version en base — `body.history` est
    # ignoré dans les chemins persistés (la vérité est en DB).
    context_messages = await ConversationService.load_context_messages(
        conversation, db
    )

    # Insertion atomique user + placeholder assistant, compteur +2.
    _user_msg, placeholder = await ConversationService.start_stream_turn(
        conversation, body.message, db
    )

    ai_messages = list(context_messages)
    ai_messages.append(AiChatMessage(role="user", content=body.message))

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
    )

    response_headers["X-Conversation-Id"] = str(conversation.id)

    handler = get_stream_handler()
    return StreamingResponse(
        _persisted_stream(
            handler=handler,
            request=request,
            ctx=ctx,
            metrics=metrics,
            assistant_message_id=placeholder.id,
            conversation_id=conversation.id,
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
    return NexyaResponse(
        success=True, data={"session_id": body.session_id, "cancelled": True}
    )


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
    outcome = _StreamOutcome()
    try:
        async for event in handler.stream(request, ctx):
            yield event
            _observe_sse_event(event, outcome)
    finally:
        await asyncio.shield(
            _finalize_in_fresh_session(
                assistant_message_id=assistant_message_id,
                conversation_id=conversation_id,
                outcome=outcome,
                metrics=metrics,
            )
        )


def _observe_sse_event(event: str, outcome: _StreamOutcome) -> None:
    """Parse un événement SSE pour extraire `delta`, `done.reason`, `error.code`.

    Format attendu (cf. streaming._sse) :
        event: <type>\n
        data: <json>\n
        \n

    Les commentaires (`: keepalive`) sont ignorés silencieusement. Un
    événement malformé est loggé en warning et ignoré — on préfère perdre
    un fragment de trace plutôt que faire crasher la finalisation.
    """
    if event.startswith(":"):
        return
    event_type: str | None = None
    data_str: str | None = None
    for line in event.split("\n"):
        if line.startswith("event: "):
            event_type = line[len("event: "):].strip()
        elif line.startswith("data: "):
            data_str = line[len("data: "):]
    if event_type is None or data_str is None:
        return
    try:
        payload = json.loads(data_str)
    except (ValueError, TypeError):
        log.warning("chat.stream.sse_parse_failed", raw=event[:120])
        return

    if event_type == "chunk":
        delta = payload.get("delta")
        if isinstance(delta, str):
            outcome.content_parts.append(delta)
    elif event_type == "done":
        reason = payload.get("reason")
        if isinstance(reason, str):
            outcome.done_reason = reason
    elif event_type == "error":
        code = payload.get("code")
        if isinstance(code, str):
            outcome.error_code = code


async def _finalize_in_fresh_session(
    *,
    assistant_message_id: uuid.UUID,
    conversation_id: uuid.UUID,
    outcome: _StreamOutcome,
    metrics: StreamMetrics,
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
    status_final = _DONE_REASON_TO_STATUS.get(outcome.done_reason, "failed")
    content = "".join(outcome.content_parts)
    usage = metrics.usage

    should_enqueue_title = False
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
            )
            # Décide d'enqueuer le titre auto une fois la finalisation
            # commitée — on ne déclenche que sur une fin propre, et tant
            # que la sentinelle DB n'a pas été posée par un précédent run.
            if status_final == "completed":
                conv = await db.get(Conversation, conversation_id)
                should_enqueue_title = bool(
                    conv
                    and conv.title_generated_at is None
                    and conv.title is None
                    and conv.message_count >= _TITLE_AUTOGENERATE_THRESHOLD
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
