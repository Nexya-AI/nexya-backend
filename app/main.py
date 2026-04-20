"""
NEXYA Backend — Point d'entrée FastAPI.

Ce fichier crée l'app, branche l'infrastructure (DB, Redis, error handlers),
et enregistre les routers. Aucune logique métier ici.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from contextlib import asynccontextmanager

# Windows : forcer SelectorEventLoop (ProactorEventLoop buggé avec asyncpg sur Py 3.14+)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.ai.budget_tracker import get_budget_tracker
from app.ai.moderation import close_moderation_service, get_moderation_service
from app.ai.providers import ChatMessage as AiChatMessage
from app.ai.providers import ImageGenerationRequest, ProviderError
from app.ai.router import build_default_router
from app.ai.streaming import StreamContext, StreamHandler, mark_cancelled
from app.config import settings
from app.core.auth.guards import get_current_user
from app.core.database.postgres import check_db_connection, dispose_engine
from app.core.database.redis import check_redis_connection, close_redis_pool
from app.core.errors.exceptions import LlmUnavailableException, NexYaException
from app.core.errors.handlers import register_exception_handlers
from app.core.observability import TraceIdMiddleware, configure_logging
from app.core.observability.trace import get_trace_id
from app.features.auth.models import User
from app.features.auth.router import router as auth_router
from app.shared.schemas import NexyaResponse

configure_logging()
log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# LIFESPAN — Démarrage et arrêt propres
# ══════════════════════════════════════════════════════════════

_AI_ROUTER = None
_STREAM_HANDLER: StreamHandler | None = None


def get_stream_handler() -> StreamHandler:
    """Retourne le singleton StreamHandler (router + retry + breakers)."""
    global _AI_ROUTER, _STREAM_HANDLER
    if _STREAM_HANDLER is None:
        _AI_ROUTER = build_default_router()
        _STREAM_HANDLER = StreamHandler(router=_AI_ROUTER)
    return _STREAM_HANDLER


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Cycle de vie de l'application.

    Démarrage : vérifie les connexions DB + Redis, construit la Couche IA.
    Arrêt : ferme proprement les pools de connexions et le client HTTP
    du service de modération.

    En mode développement, l'API démarre même si DB/Redis sont
    indisponibles (dégradation gracieuse pour le prototype).
    """
    log.info("nexya.startup", env=settings.env, debug=settings.debug)

    # Vérification des services — non bloquant en dev
    db_ok = await check_db_connection()
    redis_ok = await check_redis_connection()

    if not db_ok:
        log.warning("nexya.startup.no_database", hint="L'API démarre sans base de données")
    if not redis_ok:
        log.warning("nexya.startup.no_redis", hint="L'API démarre sans Redis")

    # Construction éagère de la Couche IA (log l'état au démarrage)
    get_stream_handler()
    get_moderation_service()
    get_budget_tracker()

    if db_ok and redis_ok:
        log.info("nexya.startup.ready", services="all")
    elif settings.is_production and (not db_ok or not redis_ok):
        log.critical("nexya.startup.failed", db=db_ok, redis=redis_ok)
        raise RuntimeError("Services critiques indisponibles en production")

    yield

    # Arrêt propre
    await close_moderation_service()
    await dispose_engine()
    await close_redis_pool()
    log.info("nexya.shutdown.complete")


# ══════════════════════════════════════════════════════════════
# APP FACTORY
# ══════════════════════════════════════════════════════════════

app = FastAPI(
    title="NEXYA API",
    version="0.1.0",
    description="API REST + SSE pour l'assistant IA NEXYA",
    lifespan=lifespan,
    docs_url="/docs" if not settings.is_production else None,
    redoc_url="/redoc" if not settings.is_production else None,
)

# ── Exception handlers globaux ─────────────────────────────────
register_exception_handlers(app)

# ── CORS ───────────────────────────────────────────────────────
# Ordre d'exécution Starlette : le dernier middleware ajouté s'exécute en premier.
# On veut TraceId en premier (pour corréler tous les logs, y compris les rejets CORS),
# donc CORS est ajouté AVANT TraceId.
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Trace ID + access log ──────────────────────────────────────
app.add_middleware(TraceIdMiddleware)


# ══════════════════════════════════════════════════════════════
# ROUTERS
# ══════════════════════════════════════════════════════════════

app.include_router(auth_router)


# ══════════════════════════════════════════════════════════════
# HEALTH CHECKS — liveness / readiness distincts
# ══════════════════════════════════════════════════════════════
# /healthz  : liveness — le process répond. Pas de check externe : si la DB
#             tombe, Kubernetes ne doit PAS tuer le pod, il doit juste le
#             sortir du load balancer.
# /ready    : readiness — toutes les dépendances critiques sont dispo.
#             Retourne 503 si DB ou Redis sont KO pour que K8s retire le pod
#             du service jusqu'à rétablissement.
# ══════════════════════════════════════════════════════════════

@app.get("/healthz", response_model=NexyaResponse[dict])
async def healthz() -> NexyaResponse[dict]:
    """Liveness probe — le process est vivant et répond."""
    return NexyaResponse(
        success=True,
        data={"status": "ok", "service": "NEXYA API", "env": settings.env},
    )


@app.get("/ready")
async def ready() -> JSONResponse:
    """Readiness probe — l'API est prête à recevoir du trafic."""
    db_ok = await check_db_connection()
    redis_ok = await check_redis_connection()
    all_ok = db_ok and redis_ok
    payload = NexyaResponse(
        success=all_ok,
        data={
            "db": "ok" if db_ok else "unavailable",
            "redis": "ok" if redis_ok else "unavailable",
        },
    )
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content=payload.model_dump(mode="json"),
    )


# Alias pour compat — legacy /health pointait vers un check dégradé.
# On le redirige sur /ready (comportement le plus attendu d'un /health).
@app.get("/health", include_in_schema=False)
async def health_alias() -> JSONResponse:
    return await ready()


# ══════════════════════════════════════════════════════════════
# ENDPOINTS IA — chat streaming, stop, génération d'images
# Ces endpoints passent par la Couche IA complète :
#   moderation → budget → router → retry/breakers → streaming SSE.
# Migreront vers features/chat/ et features/vision/ dans une PR dédiée
# (extraction sans changement de comportement).
# ══════════════════════════════════════════════════════════════

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []
    expert_id: str | None = None
    session_id: str | None = None


class ChatStopRequest(BaseModel):
    session_id: str


class ImageRequest(BaseModel):
    prompt: str
    count: int = Field(default=1, ge=1, le=4)
    expert_id: str | None = "studio"


@app.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    request: Request,
    current_user: User = Depends(get_current_user),
):
    """Chat SSE streaming via la Couche IA (router + retry + fallback).

    Contrat SSE :
    - `event: chunk`     `{delta, finish_reason?, usage?}`
    - `event: keepalive` (commentaire `:` toutes les 15s)
    - `event: error`     `{code, message}` — codes NEXYA (LLM_UNAVAILABLE, STREAM_CANCELLED…)
    - `event: done`      `{reason, duration_ms}` — toujours émis en dernier
    """
    user_id = str(current_user.id)
    trace_id = get_trace_id() or uuid.uuid4().hex
    session_id = body.session_id or uuid.uuid4().hex

    # 1. Budget : cap absolu user/jour (pré-consommation du chat)
    await get_budget_tracker().check_and_consume_chat(user_id)

    # 2. Modération du prompt utilisateur (fail-open si clé absente)
    decision = await get_moderation_service().check(
        body.message, kind="input", user_id=user_id, trace_id=trace_id
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

    # 3. Construction du contexte pour le StreamHandler
    ai_messages: list[AiChatMessage] = [
        AiChatMessage(role=_coerce_role(h.role), content=h.content)
        for h in body.history
    ]
    ai_messages.append(AiChatMessage(role="user", content=body.message))

    ctx = StreamContext(
        expert_id=body.expert_id,
        user_messages=ai_messages,
        user_id=user_id,
        trace_id=trace_id,
        session_id=session_id,
    )

    handler = get_stream_handler()

    return StreamingResponse(
        handler.stream(request, ctx),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
            "X-Session-Id": session_id,  # permet au client de rappeler /chat/stop
        },
    )


@app.post("/chat/stop", response_model=NexyaResponse[dict])
async def chat_stop(
    body: ChatStopRequest,
    current_user: User = Depends(get_current_user),
):
    """Pose la clé d'annulation Redis. Le stream actif côté serveur la lit
    dans la seconde et coupe le flux proprement (SSE `error STREAM_CANCELLED` + `done`)."""
    await mark_cancelled(body.session_id)
    return NexyaResponse(success=True, data={"session_id": body.session_id, "cancelled": True})


@app.post("/image/generate", response_model=NexyaResponse[dict])
async def image_generate(
    body: ImageRequest,
    current_user: User = Depends(get_current_user),
):
    """Génération d'images via la Couche IA (moderation + budget + router → Imagen)."""
    user_id = str(current_user.id)
    trace_id = get_trace_id() or uuid.uuid4().hex

    # 1. Budget image/jour (cost = nombre d'images demandées)
    await get_budget_tracker().check_and_consume_image(user_id, cost=body.count)

    # 2. Modération du prompt
    decision = await get_moderation_service().check(
        body.prompt, kind="input", user_id=user_id, trace_id=trace_id
    )
    if not decision.allowed:
        raise NexYaException(
            code="CONTENT_FILTERED",
            message="Ta requête a été bloquée par le filtre de sécurité.",
            status_code=400,
        )

    # 3. Résolution via LlmRouter (defaults: studio → gemini-imagen)
    handler = get_stream_handler()
    assert _AI_ROUTER is not None
    resolution = _AI_ROUTER.resolve_image(body.expert_id)

    request = ImageGenerationRequest(
        prompt=body.prompt,
        count=body.count,
        user_id=user_id,
        trace_id=trace_id,
        expert_id=resolution.config.expert_id,
    )

    log.info(
        "image.generate.start",
        user_id=user_id,
        trace_id=trace_id,
        provider=resolution.provider.name,
        model=resolution.model,
        count=body.count,
    )

    try:
        images = await resolution.provider.generate_images(request)
    except ProviderError as exc:
        log.error(
            "image.generate.provider_error",
            user_id=user_id,
            provider=resolution.provider.name,
            model=resolution.model,
            error_type=type(exc).__name__,
            retryable=exc.retryable,
        )
        raise LlmUnavailableException()

    log.info("image.generate.done", user_id=user_id, count=len(images))
    return NexyaResponse(
        success=True,
        data={
            "images": [
                {"base64": img.base64_data, "mime_type": img.mime_type}
                for img in images
            ],
            "provider": resolution.provider.name,
            "model": resolution.model,
        },
    )


# ─── helpers locaux ──────────────────────────────────────────────

def _coerce_role(role: str) -> str:
    """Le frontend envoie parfois 'ai' pour assistant — on normalise."""
    if role in ("user", "system", "assistant"):
        return role
    if role in ("ai", "bot", "model"):
        return "assistant"
    return "user"
