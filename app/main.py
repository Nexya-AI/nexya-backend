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

import base64 as _base64
from datetime import UTC

import structlog
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.budget_tracker import get_budget_tracker
from app.ai.fcm import get_fcm_provider
from app.ai.moderation import close_moderation_service, get_moderation_service
from app.ai.providers import ImageGenerationRequest, ProviderError
from app.ai.runtime import get_ai_router, get_stream_handler
from app.ai.tools.planner_tools import register_planner_tools
from app.config import settings
from app.core.auth.guards import get_current_user
from app.core.database.postgres import check_db_connection, dispose_engine, engine, get_db
from app.core.database.redis import check_redis_connection, close_redis_pool
from app.core.errors.exceptions import (
    LlmUnavailableException,
    NexYaException,
    PlanRequiredException,
)
from app.core.errors.handlers import register_exception_handlers
from app.core.health import (
    ExtendedHealthService,
    detect_version,
    set_app_start_monotonic,
)
from app.core.observability import (
    TraceIdMiddleware,
    configure_logging,
    otel_is_initialized,
    prometheus_get_registry,
    prometheus_is_initialized,
    sentry_is_initialized,
    setup_otel,
    setup_prometheus,
    setup_sentry,
    shutdown_otel,
    shutdown_sentry,
    verify_scrape_token,
)
from app.core.observability.trace import get_trace_id
from app.core.openapi import customize_openapi
from app.core.security.headers import NexyaSecurityHeadersMiddleware
from app.features.ai_models.router import router as ai_models_router
from app.features.auth.models import User
from app.features.auth.router import router as auth_router
from app.features.chat.router import router as chat_router
from app.features.files.router import router as files_router
from app.features.helpdesk.router import router as helpdesk_router
from app.features.images.c2pa import (
    C2PASignRequest,
    get_manifest_provider,
)
from app.features.images.watermark import (
    WATERMARK_VERSION,
    apply_nexya_watermark,
)
from app.features.library.router import router as library_router
from app.features.library.service import LibraryService
from app.features.memory.router import router as memory_router
from app.features.notifications.router import router as notifications_router
from app.features.planner.router import router as planner_router
from app.features.projects.router import router as projects_router
from app.features.rag.router import router as rag_router
from app.features.rgpd.router import router as rgpd_router
from app.features.suggestions.router import router as suggestions_router
from app.features.vision.router import router as vision_router
from app.features.voice.router import router as voice_router
from app.shared.schemas import NexyaResponse

configure_logging()
log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# LIFESPAN — Démarrage et arrêt propres
# ══════════════════════════════════════════════════════════════


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

    # O1 — pose le timestamp monotonic de boot pour calculer uptime
    # dans `/ready` étendu. Doit être appelé avant tout autre setup.
    set_app_start_monotonic()

    # K1 — Observabilité prod, ordre d'init :
    # 1) Sentry FIRST → capture les erreurs d'init des services suivants.
    # 2) OTel ensuite → besoin de l'`app` FastAPI déjà créée pour
    #    `FastAPIInstrumentor` + de l'`engine` SQLAlchemy.
    # 3) Prometheus en dernier → init pure CPU, ne dépend de rien.
    # Chaque setup est fail-safe : une exception ne crashe PAS le service.
    setup_sentry(settings)
    setup_otel(settings, app=app, db_engine=engine)
    setup_prometheus(settings)

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
    # F2 — Initialisation du provider FCM (mock-first, warning si clé vide)
    # + enregistrement des tools Planner dans le registry singleton.
    get_fcm_provider()
    register_planner_tools()

    if db_ok and redis_ok:
        log.info("nexya.startup.ready", services="all")
    elif settings.is_production and (not db_ok or not redis_ok):
        log.critical("nexya.startup.failed", db=db_ok, redis=redis_ok)
        raise RuntimeError("Services critiques indisponibles en production")

    yield

    # Arrêt propre — flush observabilité avant les pools
    await shutdown_otel()
    await shutdown_sentry()
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

# ── Headers sécurité (O1 volet C) ──────────────────────────────
# Posé en dernier dans add_middleware = exécuté en premier (Starlette
# LIFO) pour que les headers s'appliquent à TOUTES les réponses, y
# compris celles générées par les error handlers globaux.
app.add_middleware(
    NexyaSecurityHeadersMiddleware,
    preset=settings.security_headers_preset,
)


# ══════════════════════════════════════════════════════════════
# ROUTERS
# ══════════════════════════════════════════════════════════════

app.include_router(auth_router)
app.include_router(chat_router)
app.include_router(projects_router)
app.include_router(library_router)
app.include_router(files_router)
app.include_router(memory_router)
app.include_router(rag_router)
app.include_router(voice_router)
app.include_router(vision_router)
app.include_router(planner_router)
app.include_router(notifications_router)
app.include_router(rgpd_router)
app.include_router(ai_models_router)
app.include_router(suggestions_router)
app.include_router(helpdesk_router)


# ══════════════════════════════════════════════════════════════
# OPENAPI — schéma enrichi DD-ready (O1 volet A)
# ══════════════════════════════════════════════════════════════
# Hook posé APRÈS `include_router` pour que `customize_openapi` ait
# accès à toutes les routes. Le lambda permet de re-générer en dev
# (hot-reload) ; FastAPI cache via `app.openapi_schema` en prod.
app.openapi = lambda: customize_openapi(app)


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


@app.get("/healthz", response_model=NexyaResponse[dict], tags=["health"])
async def healthz() -> NexyaResponse[dict]:
    """Liveness probe — le process est vivant et répond.

    **Ne fait AUCUN check externe** : si DB/Redis tombent, on retourne
    quand même 200 pour que K8s ne kill pas le pod. K8s doit retirer
    le pod du load balancer via `/ready` (readiness), PAS le redémarrer.
    """
    return NexyaResponse(
        success=True,
        data={"status": "ok", "service": "NEXYA API", "env": settings.env},
    )


@app.get("/ready", tags=["health"])
async def ready() -> JSONResponse:
    """Readiness probe étendue (O1 volet B) — version + latence + queue arq.

    Retourne :
    - `status` : `ok` si DB+Redis up, `degraded` sinon (HTTP 503)
    - `version` : commit_sha[:8] + tag git + dirty flag + source détection
    - `db.latency_ms` + `db.last_migration` (Alembic version_num)
    - `redis.latency_ms`
    - `arq.queue_depth` (best-effort `ZCARD arq:queue`)
    - `uptime_seconds` (depuis lifespan startup)

    K8s lit le status_code (200/503) ; le body sert au debug ops.
    Backward-compat : `data.db` et `data.redis` toujours présents
    (avec un sub-champ `status` au lieu d'une string plate, mais le
    field racine `success` reste le signal binaire pour les anciens
    clients).
    """
    from app.core.database.redis import get_redis  # noqa: PLC0415

    redis_client = None
    try:
        redis_client = get_redis()
    except Exception:  # noqa: BLE001 — fail-safe absolu
        redis_client = None

    db_session = None
    try:
        # Ouvre une session éphémère pour le ping (n'utilise pas
        # `Depends(get_db)` pour ne pas bloquer le healthcheck si le
        # pool est saturé — on ouvre notre propre connexion).
        from app.core.database.postgres import AsyncSessionLocal  # noqa: PLC0415

        async with AsyncSessionLocal() as session:
            extended = await ExtendedHealthService.compute(
                db=session, redis=redis_client
            )
    except Exception:  # noqa: BLE001 — fail-safe absolu
        # DB indisponible — on retourne un payload dégradé
        extended = await ExtendedHealthService.compute(db=None, redis=redis_client)

    all_ok = extended.status == "ok"
    payload = NexyaResponse(
        success=all_ok,
        data=extended.model_dump(mode="json"),
    )
    return JSONResponse(
        status_code=200 if all_ok else 503,
        content=payload.model_dump(mode="json"),
    )


@app.get("/version", tags=["health"])
async def version() -> NexyaResponse[dict]:
    """Version publique de l'API (sans secret).

    Endpoint léger (< 5 ms, pas de DB) que le Flutter peut afficher
    dans Settings (« version backend 0.4.2 »). Aucun token requis.

    Retourne :
    - `version` : tag git ou commit_sha[:8] (`unknown` en fallback)
    - `commit_sha` : SHA git complet 40 chars
    - `tag` : tag git si exact match HEAD, sinon null
    - `dirty` : booléen — l'arbre courant a-t-il des modifs non committées
    - `env` : `development` / `staging` / `production`
    - `source` : d'où vient la version (`git` / `git_head_file` / `env` / `unknown`)
    """
    info = detect_version()
    return NexyaResponse(
        success=True,
        data={
            "version": info.version,
            "commit_sha": info.commit_sha,
            "tag": info.tag,
            "dirty": info.dirty,
            "env": settings.env,
            "source": info.source,
        },
    )


# Alias pour compat — legacy /health pointait vers un check dégradé.
# On le redirige sur /ready (comportement le plus attendu d'un /health).
@app.get("/health", include_in_schema=False)
async def health_alias() -> JSONResponse:
    return await ready()


# ══════════════════════════════════════════════════════════════
# OBSERVABILITÉ K1 — /metrics (Prometheus) + /observability/status
# ══════════════════════════════════════════════════════════════
# /metrics : exposition Prometheus standard, format text/plain v0.0.4.
#   Auth via header `X-Prometheus-Token` ou query `?token=...` comparé
#   constant-time à `PROMETHEUS_SCRAPE_TOKEN`. En dev, token vide =
#   ouvert avec warning au boot. En prod, token vide = refus de
#   démarrer (model_validator de Settings).
# /observability/status : JSON synthèse 3 piliers, auth identique.
# ══════════════════════════════════════════════════════════════


def _extract_scrape_token(request: Request) -> str | None:
    """Récupère le token depuis le header `X-Prometheus-Token` OU
    le query param `?token=...`. Header prioritaire."""
    token = request.headers.get("X-Prometheus-Token")
    if token:
        return token
    return request.query_params.get("token")


@app.get(settings.prometheus_metrics_path, include_in_schema=False)
async def metrics_endpoint(request: Request) -> Response:
    """Expose les métriques Prometheus au format text/plain.

    Le scraper externe (Prometheus standalone, Grafana Agent, etc.)
    appelle cet endpoint en GET avec son token. La response est
    lue ligne par ligne par Prometheus.
    """
    if not prometheus_is_initialized():
        return Response(
            content="# Prometheus disabled\n",
            media_type="text/plain; charset=utf-8",
            status_code=503,
        )

    provided = _extract_scrape_token(request)
    if not verify_scrape_token(provided, settings.prometheus_scrape_token):
        return Response(
            content="Unauthorized\n",
            media_type="text/plain; charset=utf-8",
            status_code=401,
        )

    try:
        from prometheus_client import generate_latest

        registry = prometheus_get_registry()
        payload = generate_latest(registry)
        return Response(
            content=payload,
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )
    except Exception as exc:  # noqa: BLE001
        log.warning("prometheus.export_failed", error=str(exc))
        return Response(
            content="# export error\n",
            media_type="text/plain; charset=utf-8",
            status_code=500,
        )


@app.get("/observability/status", include_in_schema=False)
async def observability_status(request: Request) -> JSONResponse:
    """Synthèse JSON des 3 piliers — utile debug rapide en prod sans
    avoir à scraper /metrics ou consulter les UIs externes."""
    provided = _extract_scrape_token(request)
    if not verify_scrape_token(provided, settings.prometheus_scrape_token):
        return JSONResponse(
            status_code=401,
            content={"success": False, "error": "Unauthorized"},
        )

    return JSONResponse(
        content={
            "success": True,
            "data": {
                "otel": {
                    "enabled": settings.otel_enabled,
                    "initialized": otel_is_initialized(),
                    "endpoint": settings.otel_exporter_otlp_endpoint,
                    "service_name": settings.otel_service_name,
                    "sampler_ratio": settings.otel_traces_sampler_ratio,
                },
                "sentry": {
                    "enabled": bool(settings.sentry_dsn),
                    "initialized": sentry_is_initialized(),
                    "environment": settings.sentry_environment,
                    "release": settings.app_version,
                },
                "prometheus": {
                    "enabled": settings.prometheus_enabled,
                    "initialized": prometheus_is_initialized(),
                    "metrics_path": settings.prometheus_metrics_path,
                    "token_protected": bool(settings.prometheus_scrape_token),
                    "metrics_count": 14,
                },
            },
        },
    )


# ══════════════════════════════════════════════════════════════
# ENDPOINT IA — génération d'images
# Migrera vers `features/vision/` dans une PR dédiée (extraction sans
# changement de comportement). Les endpoints de chat (`/chat/stream`,
# `/chat/stop`) ont été extraits dans `features/chat/router.py` au Lot 4.
# ══════════════════════════════════════════════════════════════


class ImageRequest(BaseModel):
    prompt: str
    count: int = Field(default=1, ge=1, le=4)
    expert_id: str | None = "studio"
    # E4 — watermark visuel NEXYA par défaut (retirable Pro+surcoût).
    remove_watermark: bool = False


def _build_auto_library_title(prompt: str, idx: int, total: int) -> str:
    """Compose un titre par défaut pour une image sauvée automatiquement.

    - Tronque le prompt à 60 caractères (coupe sur un espace si possible).
    - Ajoute `(N)` si `total > 1` pour distinguer les images d'une même
      génération multiple (N de 1 à total).
    - Fallback `Image générée YYYY-MM-DD HH:MM (N)` si prompt vide ou
      uniquement whitespace.
    """
    base = (prompt or "").strip()
    if base:
        # Tronque proprement sur un espace pour ne pas couper un mot.
        if len(base) > 60:
            truncated = base[:60].rsplit(" ", 1)[0]
            base = truncated if len(truncated) >= 20 else base[:60]
    else:
        from datetime import datetime as _dt

        base = "Image générée " + _dt.utcnow().strftime("%Y-%m-%d %H:%M")
    if total > 1:
        return f"{base} ({idx + 1})"
    return base


@app.post("/image/generate", response_model=NexyaResponse[dict])
async def image_generate(
    body: ImageRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Génération d'images via la Couche IA (moderation + budget + router → Imagen).

    Session C3 — auto-save des images générées dans la Library :
    chaque image retournée est automatiquement persistée via
    `LibraryService.create_from_bytes(source='generated', ...)`. En cas
    d'échec d'upload/INSERT, on log un warning mais on renvoie toujours
    la réponse 200 au client — l'IA a déjà été payée, on ne pénalise
    pas l'user. La réponse est enrichie avec `library_ids: list[str]`
    pour que le Flutter puisse pointer directement dans la biblio.
    """
    user_id = str(current_user.id)
    trace_id = get_trace_id() or uuid.uuid4().hex

    # E4 — gate Pro pour retirer le watermark. Un Free qui demande
    # `remove_watermark=True` reçoit 403 PLAN_REQUIRED avant tout appel
    # LLM (économie facture). Le Pro passe, facture future différentielle
    # préparée via metadata (voir wallet v2 dans mémoire).
    if body.remove_watermark and not current_user.is_pro:
        raise PlanRequiredException(feature="Image sans watermark")

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
    resolution = get_ai_router().resolve_image(body.expert_id)

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

    # 4. E4 — Watermark + Auto-save Library (fail-safe jamais bloquant).
    # Si `remove_watermark=False` (défaut), chaque image reçoit le
    # watermark NEXYA avant retour au client ET persistance. Si
    # `remove_watermark=True` (Pro seulement, gated plus haut), les
    # images sortent sans watermark et le metadata Library trace le
    # choix pour la future facturation différentielle (wallet v2).
    # NOTE : `GeneratedImage` est frozen, on stocke les base64 finaux
    # dans une liste parallèle au lieu de muter l'objet.
    library_ids: list[str] = []
    total = len(images)
    apply_watermark = not body.remove_watermark
    # Liste parallèle : base64 final (watermarké ou pas) par image.
    final_b64_per_image: list[str] = [img.base64_data for img in images]
    # E4.5 — accumule les résultats C2PA par image pour l'agrégat response.
    c2pa_manifest_ids: list[str | None] = []
    c2pa_applied_flags: list[bool] = []

    for idx, img in enumerate(images):
        try:
            data = _base64.b64decode(img.base64_data, validate=False)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "image.generate.library_decode_failed",
                user_id=user_id,
                trace_id=trace_id,
                error=str(exc),
            )
            continue

        has_watermark = False
        if apply_watermark:
            # Application watermark CPU-side Pillow. Fail-safe absolu :
            # si ça crash, retour bytes originaux + `has_watermark=False`.
            data, has_watermark = apply_nexya_watermark(data, img.mime_type)
            if has_watermark:
                # Re-encode base64 pour le client — l'image rendue au
                # client contient le watermark incrusté.
                final_b64_per_image[idx] = _base64.b64encode(data).decode()

        # E4.5 — Signature C2PA APRÈS watermark (la signature couvre
        # l'image telle qu'elle sortira au client + en Library), AVANT
        # l'INSERT Library (les bytes persistés et le manifest_id sont
        # cohérents). Fail-safe absolu : exception → image inchangée +
        # `has_c2pa=False` + `c2pa_skip_reason` tracé en metadata.
        # Mock-first par défaut : sans clés X.509 fournies, le mock
        # ne touche pas les bytes mais trace `has_c2pa=True` (flow
        # bout-en-bout testable). Vraie signature dès qu'Ivan fournit
        # les clés dans `.env` + `pip install c2pa-python`.
        from datetime import datetime as _dt2

        c2pa_sign_request = C2PASignRequest(
            prompt=body.prompt,
            provider=resolution.provider.name,
            model=resolution.model,
            generation_timestamp=_dt2.now(UTC),
            watermark_applied=has_watermark,
            watermark_version=WATERMARK_VERSION if has_watermark else None,
        )
        c2pa_result = await get_manifest_provider().sign_image(
            data, img.mime_type, c2pa_sign_request
        )
        c2pa_manifest_ids.append(c2pa_result.manifest_id)
        c2pa_applied_flags.append(c2pa_result.applied)
        if c2pa_result.applied and c2pa_result.image_bytes is not data:
            # Bytes modifiés par la signature réelle — re-encode base64
            # pour le client. Mock retourne les mêmes bytes (identité
            # `is`) donc cette branche est skip en mode mock.
            data = c2pa_result.image_bytes
            final_b64_per_image[idx] = _base64.b64encode(data).decode()

        try:
            item = await LibraryService.create_from_bytes(
                current_user,
                db,
                type_="image",
                title=_build_auto_library_title(body.prompt, idx, total),
                data=data,
                mime_type=img.mime_type,
                source="generated",
                provider=resolution.provider.name,
                model=resolution.model,
                prompt=body.prompt,
                metadata_json={
                    "has_watermark": has_watermark,
                    "watermark_version": (WATERMARK_VERSION if has_watermark else None),
                    "no_watermark_was_requested": body.remove_watermark,
                    "has_c2pa": c2pa_result.applied,
                    "c2pa_manifest_id": c2pa_result.manifest_id,
                    "c2pa_signed_at": (
                        c2pa_result.signed_at.isoformat()
                        if c2pa_result.signed_at
                        else None
                    ),
                    "c2pa_skip_reason": c2pa_result.skip_reason,
                },
            )
            library_ids.append(str(item.id))
        except Exception as exc:  # noqa: BLE001
            # Fail-safe : log + on ne fait pas remonter l'erreur.
            log.warning(
                "image.generate.library_save_failed",
                user_id=user_id,
                trace_id=trace_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )

    return NexyaResponse(
        success=True,
        data={
            "images": [
                {
                    "base64": final_b64_per_image[idx],
                    "mime_type": img.mime_type,
                }
                for idx, img in enumerate(images)
            ],
            "provider": resolution.provider.name,
            "model": resolution.model,
            "library_ids": library_ids,
            "watermark_applied": apply_watermark,
            "watermark_version": (WATERMARK_VERSION if apply_watermark else None),
            # E4.5 — Conformité AI Act UE 2026 : signature C2PA par image.
            # `c2pa_applied` = True ssi TOUTES les images ont été signées
            # (anti-régression : un seul échec coupe le flag, le client
            # peut afficher un badge ⚠️). `c2pa_manifest_ids` indexé par
            # image (None si skip pour cette image).
            "c2pa_applied": (
                bool(c2pa_applied_flags) and all(c2pa_applied_flags)
            ),
            "c2pa_manifest_ids": c2pa_manifest_ids,
        },
    )
