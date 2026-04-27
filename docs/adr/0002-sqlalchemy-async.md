# ADR 0002 — SQLAlchemy 2.0 async + Alembic

## Status

Accepted (2026-04-04)

## Context

Choix de l'ORM async pour PostgreSQL 16 + pgvector. Critères :
- **Async natif** (FastAPI compat — cf. [ADR 0001](0001-fastapi-vs-django.md))
- **Type-safe** Mapped[...] Pydantic-like
- **pgvector support** pour mémoire IA + RAG
- **Migrations** versionnées et reversibles
- **Mature** + écosystème Python

Choix entre :
1. SQLAlchemy 2.0 + Alembic
2. Tortoise ORM
3. SQLModel (FastAPI maker — basé sur SQLAlchemy + Pydantic)
4. Encode/databases (raw SQL async)

## Decision

**SQLAlchemy 2.0 async + Alembic**.

## Consequences

### Positives

- **Mature** : 16+ ans d'historique, écosystème ENORME
- **Type-safe** depuis SQLAlchemy 2.0 avec `Mapped[Type]`
  ```python
  class User(Base, UUIDMixin):
      email: Mapped[str] = mapped_column(unique=True)
  ```
- **pgvector intégré** via `pgvector.sqlalchemy.Vector(1536)`
- **Alembic** = outil de migrations le plus stable Python
- **Async** + sync API duale (utile pour scripts batch, seed)
- **Query builder** flexible (`select`, `update`, `delete`, `func`,
  `tuple_()`, `text()` pour SQL raw quand besoin)
- **Connection pool** auto avec `pool_pre_ping`, `pool_recycle`
- **Hooks ORM** : événements before_insert, etc. (utiles pour audit,
  V2)

### Négatives

- **Verbosité** vs Tortoise / SQLModel
- **Courbe d'apprentissage** : 2.0 syntax très différente de 1.4
  (peu de tuto à jour)
- **Async limited** : `SQLAlchemyInstrumentor` OTel ne supporte pas
  encore `AsyncEngine` (workaround : on instrumente `engine.sync_engine`)
- **psycopg v3** > asyncpg : sur Py 3.14 Windows, asyncpg crash
  `MissingGreenlet` post-rollback (psycopg v3 = stable)

### Mitigations

- **Typing strict** mypy + Mapped[...] partout
- **Alembic env.py** custom pour async (`run_async` + asyncio policy
  Windows)
- **OTel sync_engine workaround** documenté
  `app/core/observability/otel.py`
- **Driver psycopg v3** : `postgresql+psycopg://` au lieu de
  `postgresql+asyncpg://` (cf. mémoire `project_nexya_dev_setup.md`)

## Alternatives considérées

### Tortoise ORM

**Pour** : syntaxe Django-like, async natif, simple.

**Contre** :
- Pas de support pgvector natif (faut hack)
- Migrations Aerich moins mature qu'Alembic
- Communauté plus petite
- Moins flexible pour SQL raw avancé

### SQLModel (Tiangolo)

**Pour** : Pydantic v2 + SQLAlchemy combinés, syntaxe légère.

**Contre** :
- Layer abstraction supplémentaire (SQLModel sur SQLAlchemy)
- Moins flexible pour les cas complexes (custom type, JOIN avancés)
- Communauté plus petite
- Type-safety Mapped[...] de SQLAlchemy 2.0 natif déjà excellent

### Encode/databases (raw SQL async)

**Pour** : ultra-léger, contrôle total.

**Contre** :
- Pas d'ORM = boilerplate énorme pour 19 tables
- Pas de migrations natives
- Risque SQL injection si f-string oublié
- Pas de hooks événementiels

## Volume actuel (post-O2)

- 19 migrations Alembic livrées et testées (réversibilité partielle
  via `alembic downgrade -1` en CI)
- 22 tables ORM principales
- Tests pytest 1700+ verts utilisant le pattern `_FakeAsyncSession`
  + monkeypatch pour tests unitaires sans Postgres réel
- Production : `pool_size=20` + `max_overflow=10` + `pool_recycle=3600`

## Notes

Migration vers Tortoise/SQLModel = effort énorme + risque régression.
Décision verrouillée jusqu'à preuve contraire (ex: bottleneck perf
mesurable post 100k users).
