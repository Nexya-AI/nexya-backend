# syntax=docker/dockerfile:1.7
# ══════════════════════════════════════════════════════════════
# NEXYA Backend — Image production multi-stage
#
# Build :
#   docker build -f docker/Dockerfile -t nexya-backend:latest .
# Run (local) :
#   docker run --rm -p 8000:8000 --env-file .env nexya-backend:latest
# ══════════════════════════════════════════════════════════════

# ── Stage 1 ── builder ─────────────────────────────────────────
# On compile les dépendances ici, dans un venv qu'on copiera ensuite
# dans l'image finale. Le builder embarque les toolchains (gcc, etc.)
# qu'on ne veut PAS en prod.
FROM python:3.12-slim-bookworm AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0

# Dépendances système de build (psycopg binary n'a pas besoin de libpq-dev,
# mais cryptography peut avoir besoin de gcc sur certaines archis).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Installation de uv (pip install ultra rapide, lockfile-compatible).
COPY --from=ghcr.io/astral-sh/uv:0.5.14 /uv /usr/local/bin/uv

WORKDIR /build

# On copie d'abord pyproject.toml seul pour profiter du cache Docker
# tant que les dépendances ne changent pas.
COPY pyproject.toml ./

RUN uv venv /opt/venv --python 3.12 \
    && . /opt/venv/bin/activate \
    && uv pip install --no-cache -e .


# ── Stage 2 ── runtime ─────────────────────────────────────────
# Image finale — slim, sans toolchain, non-root.
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PATH="/opt/venv/bin:$PATH" \
    WEB_CONCURRENCY=4

# Runtime deps — libpq5 pour psycopg v3 + curl pour le HEALTHCHECK
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 \
        curl \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system --gid 1001 nexya \
    && useradd  --system --uid 1001 --gid nexya --home /app --shell /sbin/nologin nexya

WORKDIR /app

# Copie du venv compilé à l'étape builder — pas de recompilation.
COPY --from=builder /opt/venv /opt/venv

# Code applicatif — ownership = utilisateur non-root
COPY --chown=nexya:nexya app       ./app
COPY --chown=nexya:nexya migrations ./migrations
COPY --chown=nexya:nexya alembic.ini ./alembic.ini

USER nexya

EXPOSE 8000

# Healthcheck — liveness uniquement (pas /ready : on ne veut pas qu'un Redis
# qui hoquete marque l'image comme malade et déclenche un restart loop).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/healthz || exit 1

# uvicorn multi-workers — scale horizontal via WEB_CONCURRENCY (injecté par K8s).
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers ${WEB_CONCURRENCY} --proxy-headers --forwarded-allow-ips='*'"]
