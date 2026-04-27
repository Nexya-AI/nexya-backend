#!/usr/bin/env bash
# NEXYA Backend — Bootstrap stack tests de charge (Session N4 volet A)
#
# 1. Génère les clés JWT RS256 volatiles dans tests/load/tmp/.
# 2. Lance docker-compose -f docker-compose.load.yml up -d.
# 3. Attend que /healthz réponde (max 60s).
# 4. Lance les migrations Alembic + seed des users de test.
# 5. Crée le bucket MinIO s3 nexya-load.
#
# Idempotent : on peut le relancer sans casser l'état.
# Discipline strict bash (set -euo pipefail) + traps signaux.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOAD_DIR="$(cd "${SCRIPT_DIR}/.." && pwd)"
ROOT_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"

echo "▶ NEXYA load test bootstrap"
echo "  LOAD_DIR=${LOAD_DIR}"
echo "  ROOT_DIR=${ROOT_DIR}"

# ─── 1. Générer les clés JWT volatiles ────────────────────────
mkdir -p "${LOAD_DIR}/tmp"
if [ ! -f "${LOAD_DIR}/tmp/jwt_private.pem" ]; then
    echo "▶ Générer JWT keys RS256 volatiles..."
    openssl genrsa -out "${LOAD_DIR}/tmp/jwt_private.pem" 2048 2>/dev/null
    openssl rsa -in "${LOAD_DIR}/tmp/jwt_private.pem" \
        -pubout -out "${LOAD_DIR}/tmp/jwt_public.pem" 2>/dev/null
    echo "  ✓ Keys générées dans ${LOAD_DIR}/tmp/"
else
    echo "  ✓ Keys déjà présentes (réutilisation)"
fi

# ─── 2. docker-compose up -d ──────────────────────────────────
echo "▶ docker-compose up..."
cd "${SCRIPT_DIR}"
docker compose -f docker-compose.load.yml up -d

# ─── 3. Wait healthz (max 60s) ────────────────────────────────
echo "▶ Wait /healthz (max 60s)..."
for i in $(seq 1 60); do
    if curl -fsS http://localhost:8000/healthz > /dev/null 2>&1; then
        echo "  ✓ Backend healthy après ${i}s"
        break
    fi
    if [ "$i" -eq 60 ]; then
        echo "  ✗ Timeout — /healthz non répondu en 60s. Logs :"
        docker compose -f docker-compose.load.yml logs backend | tail -50
        exit 1
    fi
    sleep 1
done

# ─── 4. Migrations + seed ─────────────────────────────────────
echo "▶ Alembic upgrade head..."
docker compose -f docker-compose.load.yml exec -T backend \
    alembic upgrade head

echo "▶ Seed dev users (free@nexya.ai + pro@nexya.ai)..."
docker compose -f docker-compose.load.yml exec -T backend \
    python -m scripts.seed_dev || echo "  (seed déjà fait ou non critique)"

# ─── 5. MinIO bucket ──────────────────────────────────────────
echo "▶ Créer bucket MinIO nexya-load..."
docker compose -f docker-compose.load.yml exec -T minio \
    mc alias set local http://localhost:9000 nexya nexya_load_pwd 2>/dev/null \
    || true
docker compose -f docker-compose.load.yml exec -T minio \
    mc mb --ignore-existing local/nexya-load 2>/dev/null || true

echo ""
echo "✅ NEXYA load test stack ready"
echo "   Backend : http://localhost:8000"
echo "   MinIO   : http://localhost:9001 (nexya / nexya_load_pwd)"
echo ""
echo "▶ Lancer un scenario :"
echo "   k6 run tests/load/scenarios/auth_burst.js"
echo ""
echo "▶ Tear down :"
echo "   bash tests/load/docker/teardown.sh"
