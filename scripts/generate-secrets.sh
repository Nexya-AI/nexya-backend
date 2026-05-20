#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════════
# NEXYA Backend — Génération des secrets de production
# ══════════════════════════════════════════════════════════════════════════════
#
# Génère :
#   - APP_SECRET                (openssl rand -hex 32)
#   - POSTGRES_PASSWORD         (openssl rand -hex 24)
#   - REDIS_PASSWORD            (openssl rand -hex 24)
#   - S3_ACCESS_KEY / S3_SECRET_KEY  (creds MinIO)
#   - PROMETHEUS_SCRAPE_TOKEN   (openssl rand -hex 32)
#   - GRAFANA_ADMIN_PASSWORD    (openssl rand -hex 16)
#   - la paire de clés JWT RS256 → secrets/jwt_private.pem + jwt_public.pem
#
# Tous les secrets aléatoires sont en HEXADÉCIMAL → URL-safe (aucun caractère
# à échapper dans DATABASE_URL / REDIS_URL).
#
# Le script N'ÉCRIT RIEN dans git. Il :
#   - écrit les .pem dans secrets/ (dossier gitignored) ;
#   - mémorise les valeurs dans secrets/secrets-generated.env (gitignored) ;
#   - affiche sur STDOUT un bloc prêt à coller dans .env.production.
#
# IDEMPOTENT : relancer le script réaffiche les MÊMES valeurs (lues depuis
# secrets/secrets-generated.env). Les clés JWT existantes ne sont jamais
# écrasées. Pour TOUT régénérer (⚠️ invalide les tokens + secrets existants) :
#   bash scripts/generate-secrets.sh --force
#
# USAGE (depuis /opt/nexya sur le VPS) :
#   bash scripts/generate-secrets.sh
# ══════════════════════════════════════════════════════════════════════════════

set -euo pipefail

log() { echo "[generate-secrets] $*" >&2; }

# ── Chemins (résolus depuis l'emplacement du script — robuste au CWD) ─────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(dirname "$SCRIPT_DIR")"
SECRETS_DIR="$REPO_ROOT/secrets"
SECRETS_RECORD="$SECRETS_DIR/secrets-generated.env"
JWT_PRIVATE="$SECRETS_DIR/jwt_private.pem"
JWT_PUBLIC="$SECRETS_DIR/jwt_public.pem"

FORCE="false"
if [[ "${1:-}" == "--force" ]]; then
  FORCE="true"
fi

# ── Pré-checks ────────────────────────────────────────────────────────────────
if ! command -v openssl >/dev/null 2>&1; then
  echo "ERROR : openssl est requis mais introuvable." >&2
  exit 1
fi

mkdir -p "$SECRETS_DIR"
chmod 700 "$SECRETS_DIR"

# ── Clés JWT RS256 ────────────────────────────────────────────────────────────
if [[ -f "$JWT_PRIVATE" && -f "$JWT_PUBLIC" && "$FORCE" != "true" ]]; then
  log "Clés JWT déjà présentes — conservées (--force pour régénérer)."
else
  log "Génération de la paire de clés JWT RS256 (2048 bits)…"
  openssl genrsa -out "$JWT_PRIVATE" 2048 2>/dev/null
  openssl rsa -in "$JWT_PRIVATE" -pubout -out "$JWT_PUBLIC" 2>/dev/null
  chmod 600 "$JWT_PRIVATE"
  chmod 644 "$JWT_PUBLIC"
  log "Clés JWT écrites dans $SECRETS_DIR/"
fi

# ── Secrets aléatoires ────────────────────────────────────────────────────────
if [[ -f "$SECRETS_RECORD" && "$FORCE" != "true" ]]; then
  log "Secrets déjà générés — réaffichage des valeurs existantes."
else
  log "Génération des secrets aléatoires…"
  {
    echo "APP_SECRET=$(openssl rand -hex 32)"
    echo "POSTGRES_PASSWORD=$(openssl rand -hex 24)"
    echo "REDIS_PASSWORD=$(openssl rand -hex 24)"
    echo "S3_ACCESS_KEY=$(openssl rand -hex 12)"
    echo "S3_SECRET_KEY=$(openssl rand -hex 24)"
    echo "PROMETHEUS_SCRAPE_TOKEN=$(openssl rand -hex 32)"
    echo "GRAFANA_ADMIN_PASSWORD=$(openssl rand -hex 16)"
  } > "$SECRETS_RECORD"
  chmod 600 "$SECRETS_RECORD"
  log "Secrets mémorisés dans $SECRETS_RECORD"
fi

# Charge les valeurs générées dans l'environnement du script.
# shellcheck disable=SC1090
source "$SECRETS_RECORD"

# ── Bloc à coller dans .env.production ────────────────────────────────────────
# Affiché sur STDOUT (les logs vont sur STDERR) → copie/pipe propre.
cat <<EOF

# ══════════════════════════════════════════════════════════════════════════
# >>> COLLE CE BLOC DANS .env.production (remplace les __A_GENERER__) <<<
# ══════════════════════════════════════════════════════════════════════════
APP_SECRET=${APP_SECRET}

DATABASE_URL=postgresql+psycopg://nexya:${POSTGRES_PASSWORD}@postgres:5432/nexya
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}

REDIS_URL=redis://:${REDIS_PASSWORD}@redis:6379/0
REDIS_PASSWORD=${REDIS_PASSWORD}

S3_ACCESS_KEY=${S3_ACCESS_KEY}
S3_SECRET_KEY=${S3_SECRET_KEY}

PROMETHEUS_SCRAPE_TOKEN=${PROMETHEUS_SCRAPE_TOKEN}
GRAFANA_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD}
# ══════════════════════════════════════════════════════════════════════════
# Clés JWT : déjà écrites dans secrets/jwt_private.pem + jwt_public.pem.
# .env.production pointe dessus via JWT_PRIVATE_KEY=/app/secrets/jwt_private.pem
# (ne rien copier de plus pour le JWT).
# ══════════════════════════════════════════════════════════════════════════
EOF

log "Terminé. ⚠️  Ne commite JAMAIS secrets/ ni .env.production."
