#!/usr/bin/env bash
# NEXYA Backend — Export schema SQL (Session O2).
#
# Génère `docs/architecture/schema.sql` via `pg_dump --schema-only` pour
# audit DBA externe sans avoir à cloner le repo Python complet.
#
# Usage :
#   bash scripts/export_schema.sh
#   DATABASE_URL=postgresql://... bash scripts/export_schema.sh
#
# Pré-requis : `pg_dump` installé localement + DB accessible avec
# toutes les migrations appliquées (`alembic upgrade head`).
#
# Anti-pattern évité : pas de `--data-only` ni d'export complet — on
# veut juste le schéma (DDL) pour audit, pas les données dev/test/prod.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
OUT_FILE="${ROOT_DIR}/docs/architecture/schema.sql"

# Pré-vérifs
if ! command -v pg_dump >/dev/null 2>&1; then
    echo "✗ pg_dump absent. Installer postgresql-client." >&2
    exit 2
fi

# Lecture DATABASE_URL : argument explicite > env var > .env
DB_URL="${DATABASE_URL:-}"
if [ -z "${DB_URL}" ] && [ -f "${ROOT_DIR}/.env" ]; then
    DB_URL=$(grep -E '^DATABASE_URL=' "${ROOT_DIR}/.env" | head -1 | cut -d= -f2- || true)
    DB_URL="${DB_URL//\"/}"
fi
if [ -z "${DB_URL}" ]; then
    echo "✗ DATABASE_URL non défini (env var ou .env)." >&2
    exit 2
fi

# pg_dump n'accepte pas le préfixe SQLAlchemy `postgresql+psycopg://` —
# on le strip pour obtenir une URL libpq compatible.
PG_URL="${DB_URL/postgresql+psycopg:\/\//postgresql:\/\/}"

mkdir -p "$(dirname "${OUT_FILE}")"

# Header avec date + commit pour traçabilité (ne pas exposer le user
# git en cas de fuite — strict info technique).
COMMIT_SHA="$(git -C "${ROOT_DIR}" rev-parse --short HEAD 2>/dev/null || echo unknown)"
ISO_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)"

{
    echo "-- ═══════════════════════════════════════════════════════════════════"
    echo "-- NEXYA Backend — schéma SQL exporté"
    echo "-- ═══════════════════════════════════════════════════════════════════"
    echo "-- Date export : ${ISO_DATE}"
    echo "-- Commit SHA  : ${COMMIT_SHA}"
    echo "-- Source      : pg_dump --schema-only --no-owner --no-acl"
    echo "-- Re-générer  : bash scripts/export_schema.sh"
    echo "-- ═══════════════════════════════════════════════════════════════════"
    echo ""
    pg_dump \
        --schema-only \
        --no-owner \
        --no-acl \
        --no-comments \
        "${PG_URL}"
} > "${OUT_FILE}"

LINE_COUNT=$(wc -l < "${OUT_FILE}")
TABLE_COUNT=$(grep -c -E '^CREATE TABLE' "${OUT_FILE}" || echo 0)

echo "✅ Schéma SQL exporté → ${OUT_FILE}"
echo "   ${LINE_COUNT} lignes, ${TABLE_COUNT} tables"
