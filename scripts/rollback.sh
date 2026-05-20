#!/usr/bin/env bash
# NEXYA Backend — Rollback prod (Session L1)
#
# Usage :
#   bash scripts/rollback.sh v1.2.2          # rollback réel
#   bash scripts/rollback.sh --dry-run v1.2.2  # affiche commandes sans exécuter
#
# Pipeline :
#   1. Valide le tag (regex semver `vX.Y.Z`)
#   2. Pull image GHCR
#   3. Stop conteneur courant (gracefully, 30s timeout)
#   4. Update docker-compose.prod.yml pour pointer sur le nouveau tag
#   5. Up le conteneur cible
#   6. Smoke test post-restart (healthz + ready)
#   7. Si KO → restore .bak + relance ancienne image + log incident
#   8. Cleanup .bak après 5 min
#
# strict bash mode : -e (exit on error), -u (unset vars), -o pipefail
# (échec dans un pipe casse). Crucial pour un script de rollback :
# une commande qui plante au milieu DOIT arrêter le script (pas
# continuer en silence avec la prod cassée).

set -euo pipefail

# ─────────────────────────────────────────────────────────────
# Constantes + parsing args
# ─────────────────────────────────────────────────────────────

readonly IMAGE_BASE="ghcr.io/nexya-ai/nexya-backend"
readonly COMPOSE_FILE="docker/docker-compose.prod.yml"
readonly SMOKE_TIMEOUT_SECONDS=20
readonly BAK_CLEANUP_DELAY_SECONDS=300
readonly TAG_REGEX='^v[0-9]+\.[0-9]+\.[0-9]+$'

DRY_RUN=false

usage() {
  cat <<EOF
Usage: $0 [--dry-run] <tag>

Rollback prod NEXYA vers une version donnée.

Arguments :
  <tag>       Version cible au format semver vX.Y.Z (ex: v1.2.3)

Options :
  --dry-run   Affiche les commandes sans les exécuter
  -h, --help  Affiche cette aide
EOF
}

# Parse les args (--dry-run optionnel + tag obligatoire)
if [[ "${1:-}" == "-h" || "${1:-}" == "--help" ]]; then
  usage
  exit 0
fi

if [[ "${1:-}" == "--dry-run" ]]; then
  DRY_RUN=true
  shift
fi

if [[ $# -ne 1 ]]; then
  echo "ERROR: tag manquant" >&2
  usage
  exit 1
fi

readonly TARGET_TAG="$1"

# Validation tag semver
if [[ ! "$TARGET_TAG" =~ $TAG_REGEX ]]; then
  echo "ERROR: tag '$TARGET_TAG' invalide — format attendu vX.Y.Z" >&2
  exit 1
fi

# ─────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────

run() {
  # Exécute la commande sauf en --dry-run.
  if [[ "$DRY_RUN" == true ]]; then
    echo "[DRY-RUN] $*"
  else
    echo "[EXEC] $*"
    "$@"
  fi
}

log() {
  echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] [rollback] $*"
}

# ─────────────────────────────────────────────────────────────
# Pipeline rollback
# ─────────────────────────────────────────────────────────────

main() {
  log "Début rollback vers $TARGET_TAG (dry_run=$DRY_RUN)"

  # 1. Pull image GHCR
  log "Pull image $IMAGE_BASE:$TARGET_TAG"
  run docker pull "$IMAGE_BASE:$TARGET_TAG"

  # 2. Vérifie que l'image existe localement après pull
  if [[ "$DRY_RUN" == false ]]; then
    if ! docker inspect "$IMAGE_BASE:$TARGET_TAG" > /dev/null 2>&1; then
      log "ERROR: image $IMAGE_BASE:$TARGET_TAG introuvable après pull"
      exit 1
    fi
  fi

  # 3. Backup compose courant
  log "Backup $COMPOSE_FILE → $COMPOSE_FILE.bak"
  run cp "$COMPOSE_FILE" "$COMPOSE_FILE.bak"

  # 4. Update tag dans le compose
  log "Update tag dans $COMPOSE_FILE"
  run sed -i.tmp "s|$IMAGE_BASE:.*|$IMAGE_BASE:$TARGET_TAG|g" "$COMPOSE_FILE"
  run rm -f "$COMPOSE_FILE.tmp"

  # 5. Stop puis up le conteneur (graceful 30s)
  log "Stop conteneur courant (timeout 30s)"
  run docker compose -f "$COMPOSE_FILE" down --timeout 30

  log "Up conteneur cible $TARGET_TAG"
  run docker compose -f "$COMPOSE_FILE" up -d

  # 6. Wait puis smoke test
  log "Wait $SMOKE_TIMEOUT_SECONDS s avant smoke test"
  run sleep "$SMOKE_TIMEOUT_SECONDS"

  log "Smoke test healthz + ready"
  if [[ "$DRY_RUN" == false ]]; then
    if ! bash scripts/smoke_test.sh "http://localhost:8000"; then
      log "ERROR: smoke test FAILED — restoring backup"
      restore_and_exit 1
    fi
  else
    echo "[DRY-RUN] bash scripts/smoke_test.sh http://localhost:8000"
  fi

  # 7. OK
  log "rollback.success tag=$TARGET_TAG"
  schedule_bak_cleanup
}

restore_and_exit() {
  local exit_code="$1"
  log "Restore $COMPOSE_FILE.bak"
  run mv "$COMPOSE_FILE.bak" "$COMPOSE_FILE"
  log "Up ancienne image (relance après échec)"
  run docker compose -f "$COMPOSE_FILE" up -d
  log "rollback.failed exit_code=$exit_code"
  exit "$exit_code"
}

schedule_bak_cleanup() {
  # Cleanup .bak après 5 min via subshell détaché. Si le rollback est
  # confirmé OK, le backup ne sert plus à rien et trompe les ops si
  # laissé en place trop longtemps.
  if [[ "$DRY_RUN" == true ]]; then
    echo "[DRY-RUN] sleep $BAK_CLEANUP_DELAY_SECONDS && rm -f $COMPOSE_FILE.bak &"
  else
    (
      sleep "$BAK_CLEANUP_DELAY_SECONDS"
      rm -f "$COMPOSE_FILE.bak"
      log "cleanup.bak done"
    ) &
  fi
}

# Trap pour ne pas laisser de .tmp ou .bak orphelins en cas d'interrupt
trap 'log "rollback interrompu — vérifier état manuellement"; exit 130' INT TERM

main
