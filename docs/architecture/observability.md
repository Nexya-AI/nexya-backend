# Observability — NEXYA Backend

> **Executive summary (EN).** Three-pillar stack delivered in K1+K2:
> OpenTelemetry (distributed traces, OTLP exporter), Sentry (errors +
> breadcrumbs, env-aware DSN), Prometheus (14 NEXYA custom metrics +
> token-protected `/metrics` endpoint). Grafana provisioned with 5
> dashboards (UIDs stables) + 6 calibrated alerts. structlog JSON
> logs correlated via `trace_id` + `span_id` injected by OTel
> processor. Health checks split: `/healthz` (liveness, no external
> deps), `/ready` (readiness extended O1 with version git +
> latency DB/Redis + arq queue depth + last_migration + uptime).

---

## Trois piliers

### 1. **Traces** — OpenTelemetry

`app/core/observability/otel.py::setup_otel(settings, app, db_engine)`
configure :

- `TracerProvider` avec `Resource(service.name="nexya-backend",
  service.version=app_version, deployment.environment=env)`
- `BatchSpanProcessor(OTLPSpanExporter)` vers `OTEL_EXPORTER_OTLP_
  ENDPOINT/v1/traces` (fail-open silent si endpoint inaccessible)
- Sampler `ParentBased(TraceIdRatioBased(0.1))` défaut — honore la
  décision du parent cross-service

**Auto-instrumentation** 5 couches isolées :
- `FastAPIInstrumentor` (`http.server` spans)
- `SQLAlchemyInstrumentor` (sur `engine.sync_engine` — limitation OTel
  1.27 sur AsyncEngine)
- `HttpxInstrumentor` (calls vers Brevo, Crisp, OpenAI, etc.)
- `RedisInstrumentor`
- `LoggingInstrumentor` (corrélation logs)

**Spans manuels critiques** :
- `ai.chat.stream` racine + attrs `provider/model/expert_id/outcome`
- `tools.run` parent + `tools.execute` enfant
- `notifications.dispatch` + attrs `notif.channel_used/fallback_triggered`
- `arq.{function}` via `_on_job_start`/`_on_job_end` hooks worker

**Kill-switch** : `OTEL_ENABLED=False` skip total init (zéro overhead).
**`OTEL_LOG_USER_IDS=False`** par défaut RGPD — `user_id` pas inclus
dans les attributs OTel sauf debug ponctuel.

### 2. **Erreurs** — Sentry

`app/core/observability/sentry.py::setup_sentry(settings)` :

- DSN vide → `sentry_sdk.init` PAS appelé du tout (zéro overhead)
- DSN rempli → init avec 5 integrations (FastApi, SQLAlchemy, Httpx,
  Redis, Asyncio, Logging level=INFO event_level=ERROR)
- `release=app_version` (CI/CD release pose `APP_VERSION`)
- `send_default_pii=False` (RGPD)
- `traces_sample_rate=0.05` (5% des transactions échantillonnées)
- `profiles_sample_rate=0.0` V1 (re-éval Phase 19)

**Scrubber A3** ponté par alias `scrub_secrets` depuis
`core/errors/handlers.py` — nettoie `event["request"]["data"]/
headers/cookies`, `event["extra"]`, `event["contexts"]`,
`event["breadcrumbs"]` AVANT envoi.

**Filtres `before_send`** :
- `CancelledError` (déjà logué en warning, normal SSE cancel)
- `NexYaException` (erreurs métier, pas d'alerte oncall)
- `ResourceNotFoundException` (404 IDOR, pas un bug)

### 3. **Métriques** — Prometheus

`app/core/observability/prometheus.py` registry custom + 14 métriques
NEXYA prefixées `nexya_` :

| Métrique | Type | Cas d'usage |
|---|---|---|
| `nexya_ai_chat_calls_total{provider, model, outcome}` | Counter | Volume IA |
| `nexya_ai_chat_first_chunk_seconds` | Histogram | TTFT (time to first token) |
| `nexya_ai_chat_total_duration_seconds` | Histogram | Durée stream complet |
| `nexya_ai_tokens_consumed_total{kind}` | Counter | prompt+completion |
| `nexya_ai_cost_usd_total{provider, model}` | Counter | Cost tracking |
| `nexya_ai_provider_failures_total{provider, model, error_type}` | Counter | Detect fallback |
| `nexya_ai_circuit_breaker_state{provider, model}` | Gauge (0/1/2) | CLOSED/HALF_OPEN/OPEN |
| `nexya_tools_executed_total{tool_name, success}` | Counter | Function calling |
| `nexya_tools_execution_duration_seconds` | Histogram | Tool latency |
| `nexya_notifications_dispatched_total{category, channel_used}` | Counter | Push/email split |
| `nexya_notifications_fcm_failures_total{error_type}` | Counter | UNREGISTERED auto-cleanup |
| `nexya_arq_jobs_total{function, outcome}` | Counter | Worker volume |
| `nexya_arq_job_duration_seconds{function}` | Histogram | Worker latency |
| `nexya_cache_operations_total{op, result}` | Counter | Cache HIT/MISS/BYPASS |

**Buckets latence Africa-friendly** : 50ms → 60s (couvre 2G/3G + SSE
long-running).

**Endpoint `/metrics`** auth via header `X-Prometheus-Token` ou query
`?token=...`, comparé constant-time (`hmac.compare_digest`). En dev,
token vide = ouvert avec warning au boot. **En prod, token vide =
refus de démarrer** (production safety guard).

**Endpoint `/observability/status`** JSON synthèse 3 piliers (OTel/
Sentry/Prometheus) — debug rapide en prod sans avoir à scraper
`/metrics` ou consulter Grafana.

---

## Logs structurés (structlog)

### `TraceIdMiddleware`

Génère/propage `X-Request-ID` (réutilise si client en fournit un) +
bind dans `structlog.contextvars` :

```python
structlog.contextvars.bind_contextvars(
    trace_id=trace_id,
    method=request.method,
    path=request.url.path,
)
```

→ tous les logs émis pendant la requête sont corrélés automatiquement.

### Injection OTel

Processor `_inject_otel_context` ajoute `trace_id` (32 hex) +
`span_id` (16 hex) du span actif en cours. Format strict
Tempo/Jaeger/Datadog UI.

**Écrase le `trace_id` legacy** posé par `TraceIdMiddleware` quand
OTel actif (cohérence cross-service distribué). Désactivable via
`OBSERVABILITY_LOG_TRACE_INJECTION=False`.

### Format

JSON en prod (logfmt en dev humain). Exemple :

```json
{
  "timestamp": "2026-04-27T14:23:11.456Z",
  "level": "info",
  "event": "ai.chat.completed",
  "trace_id": "abc123def456...",
  "span_id": "789xyz...",
  "user_id": "uuid-...",
  "session_id": "sess-...",
  "provider": "gemini",
  "model": "gemini-2.5-flash",
  "expert_id": "computer",
  "outcome": "completed",
  "prompt_tokens": 230,
  "completion_tokens": 105,
  "cost_usd": 0.000123,
  "first_chunk_ms": 678,
  "total_duration_ms": 4521,
  "attempts": 1,
  "fallback_used": false
}
```

---

## Grafana K2

5 dashboards provisionnés UIDs stables `nexya-*` (`grafana/
provisioning/dashboards/`) :

| UID | Nom | Panels |
|---|---|---|
| `nexya-overview` | Vue globale | RPS chat, taux 5xx, p95 latence, coût IA 24h, top 5 experts, breaker states, cache hit rate |
| `nexya-ai` | IA détaillé | Variables `$provider` + `$model`, TTFB/durée p50/p95/p99, tokens consommés par kind, coût USD jour par provider, échecs par error_type, breaker state mappé |
| `nexya-tools-notifications` | Tools + Push | Variable `$tool_name`, tools executed par nom/succès, durée p95, taux succès global, notifications dispatchées par catégorie/canal, fallback push→email rate, FCM failures |
| `nexya-workers` | arq workers | Variable `$function`, jobs/min par fonction/outcome, durée p50/p95/p99, taux échec, total jobs 24h |
| `nexya-self` | Self-monitoring | `up{job=nexya-backend}`, scrape_duration, prometheus_tsdb_head_series, process_cpu, cache opérations |

**Provisioning automatique** uniquement (`allowUiUpdates: false`) —
single source of truth = Git. Toute édition UI rollbackée au prochain
scan.

**Datasource** : Prometheus default avec UID stable `nexya-prom`,
`url: http://prometheus:9090`, `editable: false`.

---

## Alertes Prometheus K2

`grafana/provisioning/alerting/rules.yml` 6 alertes group `nexya-
critical` interval 30s :

| Alerte | Expression | for | Severity |
|---|---|---|---|
| `Nexya5xxRateHigh` | `failures/calls > 0.01` | 5m | warning |
| `NexyaChatLatencyHigh` | `histogram_quantile(0.95, ...) > 5` | 10m | warning |
| `NexyaBreakerOpen` | `nexya_ai_circuit_breaker_state == 2` | 1m | critical |
| `NexyaFCMFailureRateHigh` | `> 0.05` | 10m | warning |
| `NexyaArqFailureRateHigh` | `failed/total > 0.10` | 15m | warning |
| `NexyaCostUSDDailyExceeded` | `increase(...) > 100` | 5m | critical |

Chaque alerte porte `summary` + `description` FR + label `team:
backend`. **AlertManager runtime déploiement reporté en L2 staging**
(Ivan choisit destination email/Slack/PagerDuty).

---

## Health checks (split liveness/readiness)

### `/healthz` (liveness)

```python
@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "NEXYA API", "env": env}
```

**Aucun check externe**. Si DB/Redis tombent, K8s ne doit PAS kill le
pod — il doit juste le sortir du load balancer via `/ready`. Confondre
liveness/readiness = pod redémarré inutilement quand juste la DB
tousse.

### `/ready` (readiness étendue O1)

`app/core/health/extended.py::ExtendedHealthService.compute(db, redis)`
agrège **fail-safe par champ** :

```json
{
  "success": true,
  "data": {
    "status": "ok",
    "version": {
      "version": "v0.4.2",
      "commit_sha": "875901f...",
      "tag": "v0.4.2",
      "dirty": false,
      "source": "git"
    },
    "db": {
      "status": "ok",
      "latency_ms": 1.23,
      "last_migration": "019_helpdesk"
    },
    "redis": {
      "status": "ok",
      "latency_ms": 0.45
    },
    "arq": {
      "queue_depth": 12
    },
    "uptime_seconds": 234567.123
  }
}
```

K8s lit le `status_code` (200 ok, 503 degraded). Le body sert au debug
ops.

### `/version` (public, no auth)

Endpoint léger < 5ms (pas de DB call) :

```json
{
  "success": true,
  "data": {
    "version": "v0.4.2",
    "commit_sha": "875901f...",
    "tag": "v0.4.2",
    "dirty": false,
    "env": "production",
    "source": "env"
  }
}
```

Le Flutter Settings affiche la version backend.
**Anti-fingerprinting validé par test** — pas de password/token/secret/
api_key dans la response.

---

## Évals IA + Load tests

### Évals (N3) — qualité IA

`tests/evals/` harness reproductible, 5 catégories × ~130 prompts,
juge Gemini 2.5 Pro structured output ou MockJudge SHA déterministe.
Workflow `evals.yml` 2 jobs :
- PR mock judge bloquant si pp_drop > 10pp
- Nightly real judge cron 3h UTC, ouvre issue auto si pp_drop > 5pp

Voir [`tests/evals/README.md`](../../tests/evals/README.md).

### Load tests (N4) — performance

`tests/load/` k6 6 scénarios + `thresholds.json` SLO codifiés +
`docker-compose.load.yml` stack éphémère mock-first. Workflow
`load.yml` `workflow_dispatch` + `cron weekly Sunday 4h UTC`,
fail+open issue auto si breach.

Voir [`tests/load/README.md`](../../tests/load/README.md).

---

## Roadmap observabilité

| Phase | Item |
|---|---|
| ✅ K1 | OTel + Sentry + Prometheus + 14 métriques + structlog injection |
| ✅ K2 | Grafana 5 dashboards + 6 alertes + docker-compose observability |
| ✅ N3 | Évals IA reproductibles |
| ✅ N4 | Load tests k6 |
| ✅ O1 | Health check étendu + endpoint /version |
| 🔧 L2 | Déploiement staging Hetzner + Loki/Tempo collector + AlertManager runtime (Slack/email destination) |
| 🔧 L3 | Loki log aggregation + Tempo distributed tracing UI + Grafana cloud |
| 🔧 V2 | Profiling Sentry profiles_sample_rate>0, ingestion JSON load → Grafana, dashboards K2 + DD admin metrics K2 helpdesk |
