"""
NEXYA Backend — Point d'entrée FastAPI.

Ce fichier crée l'app, branche l'infrastructure (DB, Redis, error handlers),
et enregistre les routers. Aucune logique métier ici.
"""

from __future__ import annotations

import asyncio
import json
import sys
from contextlib import asynccontextmanager

# Windows : forcer SelectorEventLoop (ProactorEventLoop buggé avec asyncpg sur Py 3.14+)
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import structlog
from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from app.config import settings
from app.core.auth.guards import get_current_user
from app.core.database.postgres import check_db_connection, dispose_engine
from app.core.database.redis import check_redis_connection, close_redis_pool
from app.core.errors.handlers import register_exception_handlers
from app.core.observability import TraceIdMiddleware, configure_logging
from app.features.auth.models import User
from app.features.auth.router import router as auth_router
from app.shared.schemas import NexyaResponse

# ── Clients IA externes (déménagés en `app/integrations/` PR 2) ─
# Ces endpoints prototypes seront migrés vers `features/chat/` et
# `features/vision/` quand ces modules seront implémentés.
from .integrations.gemini_client import stream_gemini
from .integrations.imagen_client import generate_images

configure_logging()
log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# LIFESPAN — Démarrage et arrêt propres
# ══════════════════════════════════════════════════════════════

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Cycle de vie de l'application.

    Démarrage : vérifie les connexions DB + Redis.
    Arrêt : ferme proprement les pools de connexions.

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

    if db_ok and redis_ok:
        log.info("nexya.startup.ready", services="all")
    elif settings.is_production and (not db_ok or not redis_ok):
        log.critical("nexya.startup.failed", db=db_ok, redis=redis_ok)
        raise RuntimeError("Services critiques indisponibles en production")

    yield

    # Arrêt propre
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
# ENDPOINTS EXISTANTS (Gemini + Imagen)
# Ces endpoints seront migrés vers features/chat/ et
# features/vision/ quand ces modules seront implémentés.
# ══════════════════════════════════════════════════════════════

class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    message: str
    history: list[ChatMessage] = []


class ImageRequest(BaseModel):
    prompt: str
    count: int = Field(default=1, ge=1, le=4)


@app.post("/chat/stream")
async def chat_stream(
    body: ChatRequest,
    current_user: User = Depends(get_current_user),
):
    """Chat SSE streaming via Gemini (prototype — sera migré vers features/chat/)."""
    log.info(
        "chat.stream.start",
        user_id=str(current_user.id),
        message_preview=body.message[:50],
        history_len=len(body.history),
    )

    async def generate():
        async for chunk in stream_gemini(body.message, body.history):
            for i in range(0, len(chunk), 5):
                sub = chunk[i : i + 5]
                yield f"data: {json.dumps(sub, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0)
        yield "data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.post("/image/generate", response_model=NexyaResponse[dict])
async def image_generate(
    body: ImageRequest,
    current_user: User = Depends(get_current_user),
):
    """Génération d'images via Imagen 3 (prototype — sera migré vers features/vision/)."""
    log.info(
        "image.generate.start",
        user_id=str(current_user.id),
        prompt_preview=body.prompt[:50],
        count=body.count,
    )
    images = await generate_images(body.prompt, body.count)
    log.info("image.generate.done", user_id=str(current_user.id), count=len(images))
    return NexyaResponse(success=True, data={"images": images})
