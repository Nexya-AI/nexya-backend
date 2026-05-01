#!/usr/bin/env bash
# NEXYA Backend — Restauration Postgres depuis dump S3 (Session 2026-05-01)
#
# Pipeline (cf. db-restore.md cas 1) :
#   1. Pré-checks (docker, aws, sha256sum)
#   2. Download dump + .sha256 depuis S3
#   3. Vérification intégrité SHA-256
#   4. Backup pré-restore safety net (si DB courante existe)
#   5. DROP + CREATE DB cible (idempotent)
#   6. pg_restore vers DB cible
#   7. Vérifications intégrité post-restore (count users, last_migration, FK)
#   8. Si --swap : ALTER DATABASE nexya RENAME TO nexya_old + swap target → nexya
#                  (avec confirmation interactive sauf en --dry-run)
#
# Usage :
#   bash scripts/restore_db.sh <s3_path>                          # restore vers nexya_restore (no swap)
#   bash scripts/restore_db.sh <s3_path> --swap                   # restore + swap nexya_restore → nexya
#   bash scripts/restore_db.sh <s3_path> --target-db custom_name  # cible custom
#   bash scripts/restore_db.sh <s3_path> --dry-run                # simulation
#
# Ex :
#   bash scripts/restore_db.sh s3://nexya-backups/2026/04/nexya-20260427_030001.dump
#
# Env vars (défauts entre [...]) :
#   POSTGRES_CONTAINER  [nexya-postgres]   Nom du conteneur Docker
#   POSTGRES_USER       [nexya]            User Postgres
#   POSTGRES_DB         [nexya]            DB live (cible du --swap final)
#   S3_REGION           [eu-central-1]     Région bucket S3
#
# Exit codes :
#   0  succès
#   1  restore échoué (dump corrompu, pg_restore failed, vérifs intégrité KO)
#   2  args invalides
#   3  dépendances manquantes
#   4  swap annulé par l'user
#   5  download S3 échoué

set -euo pipefail

# ═══════════════════════════════════════════════════════════════════
# Constantes & defaults
# ═══════════════════════════════════════════════════════════════════

readonly POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-nexya-postgres}"
readonly POSTGRES_USER="${POSTGRES_USER:-nexya}"
readonly POSTGRES_DB="${POSTGRES_DB:-nexya}"
readonly S3_REGION="${S3_REGION:-eu-central-1}"

DUMP_S3_PATH=""
TARGET_DB="nexya_restore"
DRY_RUN="false"
DO_SWAP="false"
TMP_DIR=""

# ═══════════════════════════════════════════════════════════════════
# Logging
# ═══════════════════════════════════════════════════════════════════

log() { echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] $*" >&2; }
log_info() { log "INFO  $*"; }
log_error() { log "ERROR $*"; }

# ═══════════════════════════════════════════════════════════════════
# Argparse
# ═══════════════════════════════════════════════════════════════════

usage() {
  cat <<EOF
Usage: $0 <s3_dump_path> [--target-db NAME] [--swap] [--dry-run]

Restauration Postgres depuis un dump S3.

Args :
  <s3_dump_path>      Chemin S3 du dump (ex: s3://nexya-backups/2026/04/nexya-XXX.dump)

Options :
  --target-db NAME    DB cible (défaut: nexya_restore)
  --swap              Après restore + vérifs OK, swap target → nexya
                      (avec confirmation interactive)
  --dry-run           Simulation (vérifie pré-checks + download path)
  -h, --help          Affiche cette aide

Vérifications post-restore intégrité :
  - count(users) > 0
  - SELECT version_num FROM alembic_version
  - count messages WHERE conversation_id NOT IN (SELECT id FROM conversations) = 0

Exit codes : 0=ok, 1=restore fail, 2=args fail, 3=deps fail,
             4=swap annulé, 5=download fail.
EOF
}

if [[ $# -eq 0 ]]; then
  log_error "Argument <s3_dump_path> manquant"
  usage
  exit 2
fi

while [[ $# -gt 0 ]]; do
  case "$1" in
    --target-db)
      shift
      TARGET_DB="${1:-}"
      if [[ -z "$TARGET_DB" ]]; then
        log_error "--target-db exige une valeur"
        exit 2
      fi
      shift
      ;;
    --swap)
      DO_SWAP="true"
      shift
      ;;
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    s3://*)
      if [[ -n "$DUMP_S3_PATH" ]]; then
        log_error "Plus d'un argument s3:// fourni"
        exit 2
      fi
      DUMP_S3_PATH="$1"
      shift
      ;;
    *)
      log_error "Argument inconnu : $1"
      usage
      exit 2
      ;;
  esac
done

if [[ -z "$DUMP_S3_PATH" ]]; then
  log_error "<s3_dump_path> obligatoire"
  exit 2
fi

# Validation regex semver-like sur le path (anti typo)
if [[ ! "$DUMP_S3_PATH" =~ ^s3://[a-zA-Z0-9._-]+/.+\.dump$ ]]; then
  log_error "Format <s3_dump_path> invalide : attendu s3://bucket/path/file.dump"
  exit 2
fi

readonly DUMP_FILENAME="$(basename "$DUMP_S3_PATH")"
readonly TARGET_DB
readonly DRY_RUN
readonly DO_SWAP

# ═══════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════

run() {
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] $*"
  else
    log_info "[EXEC] $*"
    "$@"
  fi
}

psql_in_container() {
  # Exécute du SQL via psql dans le conteneur — connecté à la DB postgres
  # par défaut (pour pouvoir DROP/CREATE des DBs).
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] docker exec $POSTGRES_CONTAINER psql -U $POSTGRES_USER -d postgres -c \"$*\""
    return 0
  fi
  docker exec -i "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d postgres -c "$*"
}

psql_in_db() {
  local db="$1"
  shift
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] docker exec $POSTGRES_CONTAINER psql -U $POSTGRES_USER -d $db -c \"$*\""
    return 0
  fi
  docker exec -i "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$db" -c "$*"
}

# ═══════════════════════════════════════════════════════════════════
# Pré-checks
# ═══════════════════════════════════════════════════════════════════

check_dependencies() {
  local missing=()
  for cmd in docker aws sha256sum; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      missing+=("$cmd")
    fi
  done
  if [[ ${#missing[@]} -gt 0 ]]; then
    log_error "Dépendances manquantes : ${missing[*]}"
    return 3
  fi
  return 0
}

# ═══════════════════════════════════════════════════════════════════
# Pipeline
# ═══════════════════════════════════════════════════════════════════

prepare_tmp_dir() {
  TMP_DIR="$(mktemp -d -t nexya-restore.XXXXXX)"
  log_info "TMP_DIR=$TMP_DIR"
}

download_dump_from_s3() {
  log_info "Download $DUMP_S3_PATH"
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] aws s3 cp $DUMP_S3_PATH $TMP_DIR/"
    log_info "[DRY-RUN] aws s3 cp ${DUMP_S3_PATH}.sha256 $TMP_DIR/"
    return 0
  fi
  if ! aws s3 cp "$DUMP_S3_PATH" "$TMP_DIR/" --region "$S3_REGION"; then
    log_error "Download S3 du dump a échoué"
    return 5
  fi
  if ! aws s3 cp "${DUMP_S3_PATH}.sha256" "$TMP_DIR/" --region "$S3_REGION"; then
    log_error "Download S3 du .sha256 a échoué"
    return 5
  fi
  log_info "Download OK"
}

verify_sha256() {
  log_info "Vérification SHA-256"
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] sha256sum -c ${DUMP_FILENAME}.sha256"
    return 0
  fi
  ( cd "$TMP_DIR" && sha256sum -c "${DUMP_FILENAME}.sha256" ) || {
    log_error "SHA-256 mismatch — dump corrompu"
    return 1
  }
  log_info "SHA-256 OK"
}

backup_current_db_safety_net() {
  # Avant un restore destructif, on dump la DB courante au cas où.
  log_info "Backup pré-restore (safety net) de $POSTGRES_DB"
  local safety_path="$TMP_DIR/before-restore-$(date -u +%Y%m%d_%H%M%S).dump"
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] docker exec $POSTGRES_CONTAINER pg_dump --format=custom -U $POSTGRES_USER $POSTGRES_DB > $safety_path"
    return 0
  fi
  if docker exec "$POSTGRES_CONTAINER" pg_dump --format=custom \
        -U "$POSTGRES_USER" "$POSTGRES_DB" > "$safety_path" 2>/dev/null; then
    log_info "Safety net dump → $safety_path"
  else
    log_info "WARN  Safety net dump impossible (DB courante absente ?) — on continue"
  fi
}

create_target_db() {
  log_info "DROP + CREATE DB cible '$TARGET_DB' (idempotent)"
  psql_in_container "DROP DATABASE IF EXISTS $TARGET_DB;"
  psql_in_container "CREATE DATABASE $TARGET_DB;"
}

restore_dump() {
  log_info "pg_restore → $TARGET_DB"
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] docker exec -i $POSTGRES_CONTAINER pg_restore -U $POSTGRES_USER -d $TARGET_DB < $TMP_DIR/$DUMP_FILENAME"
    return 0
  fi
  docker exec -i "$POSTGRES_CONTAINER" pg_restore \
        -U "$POSTGRES_USER" -d "$TARGET_DB" \
        < "$TMP_DIR/$DUMP_FILENAME" || {
    log_error "pg_restore a échoué"
    return 1
  }
  log_info "pg_restore OK"
}

verify_integrity() {
  log_info "Vérifications intégrité post-restore sur $TARGET_DB"
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] count(users) > 0 + alembic_version + FK orphelines = 0"
    return 0
  fi

  # 1. count(users) > 0
  local users_count
  users_count=$(docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$TARGET_DB" \
        -tAc "SELECT count(*) FROM users;" 2>/dev/null || echo "ERROR")
  log_info "users count = $users_count"
  if [[ "$users_count" == "ERROR" ]] || [[ "$users_count" -lt 0 ]]; then
    log_error "Vérif count(users) échouée"
    return 1
  fi

  # 2. last_migration alembic
  local migration
  migration=$(docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$TARGET_DB" \
        -tAc "SELECT version_num FROM alembic_version;" 2>/dev/null || echo "ERROR")
  log_info "alembic_version = $migration"
  if [[ "$migration" == "ERROR" ]] || [[ -z "$migration" ]]; then
    log_error "alembic_version absent — restore incomplet"
    return 1
  fi

  # 3. FK orphelines = 0 sur messages → conversations
  local orphans
  orphans=$(docker exec "$POSTGRES_CONTAINER" psql -U "$POSTGRES_USER" -d "$TARGET_DB" \
        -tAc "SELECT count(*) FROM messages WHERE conversation_id NOT IN (SELECT id FROM conversations);" 2>/dev/null || echo "0")
  log_info "messages orphelines = $orphans"
  if [[ "$orphans" -gt 0 ]]; then
    log_error "$orphans messages orphelines détectées — FK cassées"
    return 1
  fi

  log_info "Vérifs intégrité OK"
}

confirm_swap_or_abort() {
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] swap auto-confirmé"
    return 0
  fi

  echo "" >&2
  echo "═══════════════════════════════════════════════════════════════" >&2
  echo "  ATTENTION : Swap imminent" >&2
  echo "  La DB '$POSTGRES_DB' va être renommée en '${POSTGRES_DB}_old'" >&2
  echo "  La DB '$TARGET_DB' va être renommée en '$POSTGRES_DB'" >&2
  echo "  Les connexions actives sur '$POSTGRES_DB' vont être interrompues." >&2
  echo "═══════════════════════════════════════════════════════════════" >&2
  read -r -p "Tape 'swap' pour confirmer (autre = annuler) : " confirmation
  if [[ "$confirmation" != "swap" ]]; then
    log_info "Swap annulé par l'utilisateur"
    return 4
  fi
  return 0
}

swap_databases() {
  log_info "Swap : $POSTGRES_DB → ${POSTGRES_DB}_old, $TARGET_DB → $POSTGRES_DB"
  # Important : il faut interrompre les connexions actives à $POSTGRES_DB
  # avant le ALTER DATABASE RENAME (sinon "database is being accessed").
  psql_in_container "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$POSTGRES_DB' AND pid <> pg_backend_pid();"
  psql_in_container "ALTER DATABASE $POSTGRES_DB RENAME TO ${POSTGRES_DB}_old;"
  psql_in_container "ALTER DATABASE $TARGET_DB RENAME TO $POSTGRES_DB;"
  log_info "Swap OK — ${POSTGRES_DB}_old conservée pour rollback (drop manuel après 24h)"
}

# ═══════════════════════════════════════════════════════════════════
# Trap nettoyage
# ═══════════════════════════════════════════════════════════════════

cleanup_on_exit() {
  local exit_code=$?
  if [[ -n "${TMP_DIR:-}" && -d "$TMP_DIR" ]]; then
    if [[ "$DRY_RUN" == "true" ]]; then
      log_info "[DRY-RUN] cleanup TMP_DIR (skipped)"
    else
      rm -rf "$TMP_DIR"
    fi
  fi
  if [[ $exit_code -ne 0 ]]; then
    log_error "Restore échoué (exit=$exit_code)"
  fi
}
trap cleanup_on_exit EXIT
trap 'log_error "Interrompu (SIGINT/SIGTERM)"; exit 130' INT TERM

# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

main() {
  log_info "═══ NEXYA restore_db.sh — start ═══"
  log_info "Source S3 : $DUMP_S3_PATH"
  log_info "Target DB : $TARGET_DB"
  log_info "Swap      : $DO_SWAP"
  log_info "Dry-run   : $DRY_RUN"

  check_dependencies || exit 3
  prepare_tmp_dir
  download_dump_from_s3 || exit 5
  verify_sha256 || exit 1
  backup_current_db_safety_net
  create_target_db
  restore_dump || exit 1
  verify_integrity || exit 1

  if [[ "$DO_SWAP" == "true" ]]; then
    confirm_swap_or_abort || exit 4
    swap_databases
  else
    log_info "Restore terminé sans swap. DB cible : $TARGET_DB"
    log_info "Pour swap manuel ultérieur : --swap"
  fi

  log_info "═══ Restore OK ═══"
}

main
