#!/usr/bin/env bash
# NEXYA Backend — Backup quotidien Postgres → S3 (Session 2026-05-01)
#
# Pipeline :
#   1. Pré-checks : docker, aws CLI, optionnel gpg, flock
#   2. Acquisition d'un lock flock (anti double-run concurrent)
#   3. pg_dump format custom + compress=9 via docker exec
#   4. SHA-256 du dump pour vérification d'intégrité
#   5. Chiffrement GPG optionnel (env BACKUP_GPG_RECIPIENT)
#   6. Upload S3 SSE AES256 (région configurable)
#   7. Cleanup local des dumps > BACKUP_RETENTION_DAYS jours
#   8. Rapport final (taille, durée, statut S3)
#
# Usage :
#   bash scripts/backup_db.sh                        # exécution réelle
#   bash scripts/backup_db.sh --dry-run              # simulation sans side effects
#   bash scripts/backup_db.sh --help                 # affiche cette aide
#
# Env vars (défauts entre [...]) :
#   BACKUP_DIR              [/backups]               Dossier local des dumps
#   S3_BUCKET               [nexya-backups]          Bucket S3 destination
#   S3_REGION               [eu-central-1]           Région bucket S3
#   POSTGRES_CONTAINER      [nexya-postgres]         Nom du conteneur Docker
#   POSTGRES_DB             [nexya]                  Nom de la DB
#   POSTGRES_USER           [nexya]                  User Postgres
#   BACKUP_RETENTION_DAYS   [7]                      Cleanup local après N jours
#   BACKUP_GPG_RECIPIENT    [<vide>]                 Si défini, chiffre via gpg
#
# Mode dry-run :
#   - Préfixe chaque commande avec [DRY-RUN]
#   - Ne lance ni pg_dump, ni gpg, ni aws s3 cp, ni find -delete
#   - Vérifie quand même les pré-checks réels
#   - Exit 0 si pré-checks OK, sinon exit 3
#
# Cas test S3 absent (CI/dev) :
#   Si S3_BUCKET=test-skip, l'upload S3 est sauté avec un warning ;
#   le dump local + .sha256 sont quand même créés. Utile pour valider
#   localement sans credentials AWS.
#
# Exit codes :
#   0  succès complet (ou dry-run OK)
#   1  backup pg_dump échoué
#   2  args invalides
#   3  dépendances manquantes (docker, aws, etc.)
#   4  lock occupé (autre backup tourne déjà)
#   5  upload S3 échoué (mais dump local valide reste)
#
# Idempotence :
#   - Lock flock empêche 2 runs simultanés
#   - Si relancé après échec partiel, recrée un dump frais avec timestamp
#     UTC distinct (pas d'écrasement)

set -euo pipefail

# ═══════════════════════════════════════════════════════════════════
# Constantes & defaults
# ═══════════════════════════════════════════════════════════════════

readonly LOCK_FILE="/var/lock/nexya-backup.lock"
readonly BACKUP_DIR="${BACKUP_DIR:-/backups}"
readonly S3_BUCKET="${S3_BUCKET:-nexya-backups}"
readonly S3_REGION="${S3_REGION:-eu-central-1}"
readonly POSTGRES_CONTAINER="${POSTGRES_CONTAINER:-nexya-postgres}"
readonly POSTGRES_DB="${POSTGRES_DB:-nexya}"
readonly POSTGRES_USER="${POSTGRES_USER:-nexya}"
readonly BACKUP_RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
readonly BACKUP_GPG_RECIPIENT="${BACKUP_GPG_RECIPIENT:-}"

DRY_RUN="false"
TIMESTAMP="$(date -u +%Y%m%d_%H%M%S)"
DUMP_FILENAME="nexya-${TIMESTAMP}.dump"
DUMP_PATH="${BACKUP_DIR}/${DUMP_FILENAME}"
START_EPOCH="$(date +%s)"

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
Usage: $0 [--dry-run]

Backup quotidien Postgres NEXYA → S3 (cron typique 03:00 UTC).

Options :
  --dry-run    Simulation sans side effects (pré-checks réels conservés)
  -h, --help   Affiche cette aide

Env vars :
  BACKUP_DIR, S3_BUCKET, S3_REGION, POSTGRES_CONTAINER, POSTGRES_DB,
  POSTGRES_USER, BACKUP_RETENTION_DAYS, BACKUP_GPG_RECIPIENT.
  Voir le header du fichier pour les défauts.

Exit codes : 0=ok, 1=dump fail, 2=args fail, 3=deps fail,
             4=lock occupé, 5=S3 fail.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --dry-run)
      DRY_RUN="true"
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      log_error "Argument inconnu : $1"
      usage
      exit 2
      ;;
  esac
done

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

# ═══════════════════════════════════════════════════════════════════
# Pré-checks dépendances
# ═══════════════════════════════════════════════════════════════════

check_dependencies() {
  local missing=()
  for cmd in docker aws sha256sum find; do
    if ! command -v "$cmd" >/dev/null 2>&1; then
      missing+=("$cmd")
    fi
  done

  if [[ -n "$BACKUP_GPG_RECIPIENT" ]] && ! command -v gpg >/dev/null 2>&1; then
    missing+=("gpg (requis car BACKUP_GPG_RECIPIENT défini)")
  fi

  # `flock` est sur Linux uniquement (absent macOS BSD-style et Windows).
  # On le rend optionnel — si absent, le lock est sauté avec warning.
  if ! command -v flock >/dev/null 2>&1; then
    log_info "WARN  flock absent — lock anti-concurrent désactivé"
  fi

  if [[ ${#missing[@]} -gt 0 ]]; then
    log_error "Dépendances manquantes : ${missing[*]}"
    return 3
  fi
  return 0
}

# ═══════════════════════════════════════════════════════════════════
# Lock flock (anti double-run)
# ═══════════════════════════════════════════════════════════════════

acquire_lock_or_exit() {
  if ! command -v flock >/dev/null 2>&1; then
    log_info "lock skipped (flock indisponible)"
    return 0
  fi

  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] flock acquire on $LOCK_FILE"
    return 0
  fi

  # Crée le fichier de lock si absent
  mkdir -p "$(dirname "$LOCK_FILE")" 2>/dev/null || true
  exec 200>"$LOCK_FILE" || {
    log_error "Impossible d'ouvrir $LOCK_FILE (permissions ?)"
    exit 4
  }

  if ! flock -n 200; then
    log_error "Lock $LOCK_FILE déjà occupé — un autre backup tourne"
    exit 4
  fi

  log_info "Lock $LOCK_FILE acquis"
}

# ═══════════════════════════════════════════════════════════════════
# Pipeline backup
# ═══════════════════════════════════════════════════════════════════

ensure_backup_dir() {
  run mkdir -p "$BACKUP_DIR"
}

do_pg_dump() {
  log_info "pg_dump → $DUMP_PATH"
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] docker exec $POSTGRES_CONTAINER pg_dump --format=custom --compress=9 -U $POSTGRES_USER -d $POSTGRES_DB"
    return 0
  fi

  if ! docker exec "$POSTGRES_CONTAINER" pg_dump \
        --format=custom --compress=9 \
        -U "$POSTGRES_USER" -d "$POSTGRES_DB" \
        > "$DUMP_PATH"; then
    log_error "pg_dump a échoué"
    return 1
  fi

  local size_bytes
  size_bytes=$(stat -c%s "$DUMP_PATH" 2>/dev/null || stat -f%z "$DUMP_PATH" 2>/dev/null || echo 0)
  log_info "pg_dump OK — taille=${size_bytes} bytes"
}

do_sha256() {
  log_info "SHA-256 du dump"
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] sha256sum $DUMP_PATH > ${DUMP_PATH}.sha256"
    return 0
  fi
  ( cd "$BACKUP_DIR" && sha256sum "$DUMP_FILENAME" > "${DUMP_FILENAME}.sha256" )
  log_info "SHA-256 OK"
}

do_gpg_encrypt() {
  if [[ -z "$BACKUP_GPG_RECIPIENT" ]]; then
    log_info "GPG skip (BACKUP_GPG_RECIPIENT non défini)"
    return 0
  fi

  log_info "Chiffrement GPG (recipient=$BACKUP_GPG_RECIPIENT)"
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] gpg --encrypt --recipient $BACKUP_GPG_RECIPIENT --output ${DUMP_PATH}.gpg $DUMP_PATH"
    return 0
  fi

  gpg --batch --yes --encrypt \
      --recipient "$BACKUP_GPG_RECIPIENT" \
      --output "${DUMP_PATH}.gpg" \
      "$DUMP_PATH"

  # Remplace l'original par la version chiffrée pour l'upload
  rm -f "$DUMP_PATH"
  mv "${DUMP_PATH}.gpg" "$DUMP_PATH"
  log_info "GPG OK"
}

do_s3_upload() {
  if [[ "$S3_BUCKET" == "test-skip" ]]; then
    log_info "S3 upload skip (S3_BUCKET=test-skip — mode CI/dev local)"
    return 0
  fi

  local s3_prefix
  s3_prefix="$(date -u +%Y/%m)"
  local s3_dest="s3://${S3_BUCKET}/${s3_prefix}/"

  log_info "Upload S3 → $s3_dest (region=$S3_REGION, --sse AES256)"
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] aws s3 cp $DUMP_PATH $s3_dest --sse AES256 --region $S3_REGION"
    log_info "[DRY-RUN] aws s3 cp ${DUMP_PATH}.sha256 $s3_dest --sse AES256 --region $S3_REGION"
    return 0
  fi

  if ! aws s3 cp "$DUMP_PATH" "$s3_dest" \
      --sse AES256 --region "$S3_REGION"; then
    log_error "Upload S3 du dump a échoué — le dump local est conservé"
    return 5
  fi
  if ! aws s3 cp "${DUMP_PATH}.sha256" "$s3_dest" \
      --sse AES256 --region "$S3_REGION"; then
    log_error "Upload S3 du sha256 a échoué"
    return 5
  fi
  log_info "S3 upload OK"
}

do_cleanup_local() {
  log_info "Cleanup local — dumps > $BACKUP_RETENTION_DAYS jours"
  if [[ "$DRY_RUN" == "true" ]]; then
    log_info "[DRY-RUN] find $BACKUP_DIR -name 'nexya-*.dump*' -mtime +$BACKUP_RETENTION_DAYS -delete"
    return 0
  fi
  find "$BACKUP_DIR" -name 'nexya-*.dump*' -mtime "+$BACKUP_RETENTION_DAYS" -delete || {
    log_info "WARN  cleanup find a renvoyé un code non-zéro (peut être normal si rien à supprimer)"
  }
}

print_summary() {
  local end_epoch duration size
  end_epoch="$(date +%s)"
  duration=$(( end_epoch - START_EPOCH ))
  if [[ -f "$DUMP_PATH" ]]; then
    size=$(stat -c%s "$DUMP_PATH" 2>/dev/null || stat -f%z "$DUMP_PATH" 2>/dev/null || echo 0)
  else
    size="(absent)"
  fi
  log_info "═══ Récap backup ═══"
  log_info "Timestamp     : $TIMESTAMP"
  log_info "Dump path     : $DUMP_PATH"
  log_info "Taille bytes  : $size"
  log_info "Durée totale  : ${duration}s"
  log_info "S3 bucket     : $S3_BUCKET (region=$S3_REGION)"
  log_info "GPG enabled   : $([[ -n "$BACKUP_GPG_RECIPIENT" ]] && echo "yes ($BACKUP_GPG_RECIPIENT)" || echo "no")"
  log_info "Dry-run       : $DRY_RUN"
}

# ═══════════════════════════════════════════════════════════════════
# Trap nettoyage
# ═══════════════════════════════════════════════════════════════════

cleanup_on_exit() {
  local exit_code=$?
  if [[ $exit_code -ne 0 ]]; then
    log_error "Backup échoué (exit=$exit_code)"
  fi
  # Le file descriptor 200 est fermé automatiquement à la sortie shell,
  # donc le flock est libéré sans action explicite.
}
trap cleanup_on_exit EXIT
trap 'log_error "Interrompu (SIGINT/SIGTERM)"; exit 130' INT TERM

# ═══════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════

main() {
  log_info "═══ NEXYA backup_db.sh — start (dry_run=$DRY_RUN) ═══"

  check_dependencies || exit 3
  acquire_lock_or_exit

  ensure_backup_dir

  if ! do_pg_dump; then
    exit 1
  fi
  do_sha256
  do_gpg_encrypt
  do_s3_upload || {
    # L'upload S3 a échoué mais le dump local est conservé pour retry manuel.
    print_summary
    exit 5
  }
  do_cleanup_local
  print_summary

  log_info "═══ Backup OK ═══"
}

main
