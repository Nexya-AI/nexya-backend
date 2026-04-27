# ADR 0001 — FastAPI vs Django REST Framework

## Status

Accepted (2026-04-04)

## Context

NEXYA backend doit être :
- **Async-first** pour SSE streaming chat IA + 950k users cibles
- **Python** pour profiter de l'écosystème IA (`openai`, `anthropic`,
  `google-genai`, `pgvector`, etc.)
- **Compatible Africa-first** : low-latency, 2G/3G heartbeat
- **Type-safe** : Pydantic v2 pour validation/sérialisation

Choix entre :
1. FastAPI (Starlette + Pydantic v2)
2. Django + Django REST Framework (DRF)
3. Aiohttp + custom validation

## Decision

**FastAPI**.

## Consequences

### Positives

- **Async natif** — `async def` partout, parfait pour SSE long-running
- **Pydantic v2** intégré — validation/serialization gratuite
- **Auto-generation OpenAPI 3.1** depuis le code (cf. O1 customizer)
- **Ecosystem moderne** : Uvicorn + Starlette middleware ASGI
- **Performance** : ~3-5× plus rapide que Django sync sur SSE
- **Type hints** strict — erreurs de typage attrapées par mypy/ruff
- **Communauté IA Python** : la plupart des tuto/SDK utilisent FastAPI

### Négatives

- **Pas de admin gratuit** comme Django Admin (V2 si besoin —
  Streamlit ou Retool)
- **Pas de migrations natives** — on ajoute Alembic (cf.
  [ADR 0002](0002-sqlalchemy-async.md))
- **Écosystème plus jeune** que Django — quelques librairies tierces
  manquent ou sont récentes
- **Async DB** = piège ProactorEventLoop sur Windows + asyncpg vs
  psycopg v3 (documenté `app/core/database/postgres.py`)

### Mitigations

- Admin endpoints custom dans `/admin/*` avec `require_admin` ACL
  email-list (J1)
- Migrations Alembic 19 livrées (cf.
  [`docs/architecture/data-model.md`](../architecture/data-model.md))
- Windows event loop policy forcée via `asyncio.set_event_loop_policy(
  WindowsSelectorEventLoopPolicy())` dans `main.py`
- Driver `psycopg[binary]` v3 au lieu d'asyncpg (bug Py 3.14 Windows)

## Alternatives considérées

### Django + DRF

**Pour** : admin gratuit, écosystème mature, migrations natives,
auth mature.

**Contre** :
- Sync par défaut (async support encore expérimental 2024)
- Moins performant sur SSE
- ORM Django plus rigide que SQLAlchemy
- Auth Django pas adapté pour mobile API JWT (plutôt session-based)

### Aiohttp + custom validation

**Pour** : ultra-léger, full-async.

**Contre** :
- Pas de validation automatique (faut écrire tout à la main)
- Pas d'auto-generation OpenAPI
- Communauté plus petite
- Plus de code custom = plus de bugs

## Notes

Décision révisable Phase 19 si NEXYA atteint des contraintes que
FastAPI ne tient pas (ex: > 50k req/s nécessitant Rust/Go). À ce
moment-là, migration partielle endpoint-by-endpoint vers Axum (Rust)
ou Fiber (Go) reste possible — l'API REST + SSE est portable.
