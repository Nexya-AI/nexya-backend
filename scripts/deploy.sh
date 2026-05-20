#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# NEXYA Backend — Déploiement production « 5 minutes »
# ══════════════════════════════════════════════════════════════════════════════
#
# Déploie une version de l'API NEXYA sur le VPS, de bout en bout :
#   1. git pull des fichiers d'infrastructure (compose, Caddyfile, scripts)
#   2. docker compose pull de l'image GHCR taggée
#   3. démarrage postgres + redis + minio, attente healthcheck
#   4. migrations Alembic (alembic upgrade head)
#   5. démarrage de la stack complète (api, worker, caddy)
#   6. attente du healthcheck de l'API
#   7. smoke test sur l'URL publique (retries pour le délai DNS/TLS)
#
# USAGE (depuis /opt/nexya sur le VPS) :
#   bash scripts/deploy.sh v1.0.0
#   bash scripts/deploy.sh --skip-pull v1.0.0    # ne pas git pull
#
# Variable d'environnement optionnelle :
#   NEXYA_PUBLIC_URL   URL publique pour le smoke test (défaut : api.nexyalabs.com)
#
# ┌─ ROLLBACK ──────────────────────────────────────────────────────────────────┐
# │ Si un déploiement casse quelque chose, redéploie la version précédente :     │
# │   bash scripts/deploy.sh v0.9.0                                              │
# │ (ou bash scripts/rollback.sh v0.9.0)                                         │
# └─────────────────────────────────────────────────────────────────────────────┘
#
# PRÉ-REQUIS : le VPS doit déjà être authentifié à GHCR (`docker login ghcr.io`,
# fait une seule fois en phase D7) — sinon le `docker compose pull` échoue.
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

# ── Chemins & constantes ──────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
cd "$REPO_ROOT"

readonly COMPOSE_FILE="docker/docker-compose.prod.yml"
readonly ENV_FILE=".env.production"
readonly TAG_REGEX='^v[0-9]+\.[0-9]+\.[0-9]+$'
readonly NEXYA_PUBLIC_URL="${NEXYA_PUBLIC_URL:-https://api.nexyalabs.com}"
readonly API_HEALTH_TIMEOUT=150     # secondes — attente nexya-api healthy
readonly SMOKE_MAX_ATTEMPTS=6       # 6 × 20 s = ~2 min pour absorber le délai ACME
readonly SMOKE_RETRY_DELAY=20

SKIP_PULL="false"
TAG=""

log()  { echo "[deploy] $*"; }
fail() { echo "[deploy] ❌ ERREUR : $*" >&2; exit 1; }

usage() {
  cat <<EOF
Usage: $0 [--skip-pull] <vX.Y.Z>

Déploie l'image NEXYA taggée vX.Y.Z sur le VPS.

Options :
  --skip-pull   Ne pas faire de git pull des fichiers d'infra
  -h, --help    Affiche cette aide
EOF
}

# ── Parsing des arguments ─────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --skip-pull) SKIP_PULL="true"; shift ;;
    -h|--help)   usage; exit 0 ;;
    v*)          TAG="$1"; shift ;;
    *)           usage; fail "Argument inconnu : $1" ;;
  esac
done

[[ -n "$TAG" ]] || { usage; fail "Tag de version manquant (ex: v1.0.0)."; }
[[ "$TAG" =~ $TAG_REGEX ]] || fail "Tag '$TAG' invalide — format attendu vX.Y.Z."

# ── Pré-checks ────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || fail "docker introuvable sur ce serveur."
docker compose version >/dev/null 2>&1 || fail "le plugin 'docker compose' est introuvable."
[[ -f "$ENV_FILE" ]] || fail "$ENV_FILE introuvable. Crée-le depuis .env.production.example (phase D7)."

export IMAGE_TAG="$TAG"
export GHCR_OWNER="${GHCR_OWNER:-nexya-ai}"

# Helper : `docker compose` préfixé du fichier prod + env-file.
dc() { docker compose -f "$COMPOSE_FILE" --env-file "$ENV_FILE" "$@"; }

# Helper : attend qu'un conteneur passe `healthy` (poll docker inspect).
wait_healthy() {
  local container="$1" timeout="$2" elapsed=0 status
  log "Attente du healthcheck : $container (timeout ${timeout}s)…"
  while [[ $elapsed -lt $timeout ]]; do
    status="$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container" 2>/dev/null || echo "absent")"
    if [[ "$status" == "healthy" ]]; then
      log "$container : healthy ✓"
      return 0
    fi
    sleep 5
    elapsed=$((elapsed + 5))
  done
  return 1
}

log "═══════════════════════════════════════════════"
log "Déploiement NEXYA — image ghcr.io/$GHCR_OWNER/nexya-backend:$IMAGE_TAG"
log "═══════════════════════════════════════════════"

# ── 1. git pull des fichiers d'infrastructure ─────────────────────────────────
if [[ "$SKIP_PULL" == "true" ]]; then
  log "1/7 — git pull sauté (--skip-pull)"
else
  log "1/7 — git pull --ff-only (fichiers d'infra à jour)"
  git pull --ff-only \
    || log "WARN  git pull a échoué (working tree modifié ?) — on continue avec les fichiers actuels."
fi

# ── 2. Pull de l'image GHCR ───────────────────────────────────────────────────
log "2/7 — Pull de l'image $IMAGE_TAG depuis GHCR"
dc pull \
  || fail "docker compose pull a échoué. Le VPS est-il authentifié à GHCR ? → docker login ghcr.io"

# ── 3. Services de données + attente healthcheck ──────────────────────────────
log "3/7 — Démarrage postgres + redis + minio"
dc up -d postgres redis minio
wait_healthy nexya-postgres 90 || fail "postgres n'est pas devenu healthy."
wait_healthy nexya-redis 60   || fail "redis n'est pas devenu healthy."
wait_healthy nexya-minio 60   || fail "minio n'est pas devenu healthy."

# ── 4. Migrations Alembic ─────────────────────────────────────────────────────
log "4/7 — Migrations Alembic (alembic upgrade head)"
dc run --rm nexya-api alembic upgrade head \
  || fail "Les migrations Alembic ont échoué. La stack n'est PAS démarrée — corrige puis relance."

# ── 5. Démarrage de la stack complète ─────────────────────────────────────────
log "5/7 — Démarrage de la stack complète (api, worker, caddy)"
dc up -d

# ── 6. Attente du healthcheck de l'API ────────────────────────────────────────
log "6/7 — Vérification du healthcheck de l'API"
if ! wait_healthy nexya-api "$API_HEALTH_TIMEOUT"; then
  log "Logs nexya-api (100 dernières lignes) :"
  dc logs --tail=100 nexya-api >&2 || true
  fail "nexya-api n'est pas devenu healthy. Rollback : bash scripts/deploy.sh <tag-précédent>"
fi

# ── 7. Smoke test public (avec retries pour le délai DNS/TLS) ─────────────────
log "7/7 — Smoke test public : $NEXYA_PUBLIC_URL"
smoke_ok="false"
attempt=1
while [[ $attempt -le $SMOKE_MAX_ATTEMPTS ]]; do
  if bash scripts/smoke_test.sh "$NEXYA_PUBLIC_URL"; then
    smoke_ok="true"
    break
  fi
  log "Smoke échoué (tentative $attempt/$SMOKE_MAX_ATTEMPTS) — DNS/certificat TLS pas encore prêt ? retry dans ${SMOKE_RETRY_DELAY}s…"
  attempt=$((attempt + 1))
  sleep "$SMOKE_RETRY_DELAY"
done
[[ "$smoke_ok" == "true" ]] \
  || fail "Smoke test public KO après $SMOKE_MAX_ATTEMPTS tentatives. Rollback : bash scripts/deploy.sh <tag-précédent>"

# ── Récapitulatif ─────────────────────────────────────────────────────────────
log "═══════════════════════════════════════════════"
log "✅ Déploiement $IMAGE_TAG réussi."
log "   API   : $NEXYA_PUBLIC_URL"
log "   Image : ghcr.io/$GHCR_OWNER/nexya-backend:$IMAGE_TAG"
log "═══════════════════════════════════════════════"
