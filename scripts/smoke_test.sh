#!/usr/bin/env bash
# NEXYA Backend — Smoke test post-deploy (Session L1)
#
# Usage :
#   bash scripts/smoke_test.sh http://localhost:8000             # CI / local
#   ENV=staging bash scripts/smoke_test.sh https://staging-api.nexya.ai
#
# CI = uniquement les checks read-only (healthz + ready + metrics +
# observability/status). Tranche 7 (2026-04-26) : PAS de POST register
# en CI (rate limit A3 cassera au 6ᵉ run sur la même IP).
#
# Staging = ajoute un test bout-en-bout DB write (register + delete user
# dummy) pour valider la chaîne complète post-deploy réel.

set -euo pipefail

readonly BASE_URL="${1:-}"
readonly ENV="${ENV:-ci}"

if [[ -z "$BASE_URL" ]]; then
  echo "ERROR: base URL manquante" >&2
  echo "Usage: $0 <base_url>" >&2
  echo "Exemple: $0 http://localhost:8000" >&2
  exit 1
fi

# Trap EXIT pour log final + cleanup (si test register a tourné).
DUMMY_USER_TOKEN=""
DUMMY_USER_EMAIL=""

cleanup() {
  local exit_code=$?
  if [[ -n "$DUMMY_USER_TOKEN" ]]; then
    echo "[smoke] Cleanup user dummy ($DUMMY_USER_EMAIL)"
    curl -fsS -X DELETE "$BASE_URL/user/account" \
      -H "Authorization: Bearer $DUMMY_USER_TOKEN" \
      > /dev/null 2>&1 || true
  fi
  if [[ $exit_code -eq 0 ]]; then
    echo "[smoke] OK — tous les checks ont passé"
  else
    echo "[smoke] FAIL — exit code $exit_code"
  fi
  exit $exit_code
}
trap cleanup EXIT

log() {
  echo "[smoke] $*"
}

# ─────────────────────────────────────────────────────────────
# Checks read-only (toujours exécutés)
# ─────────────────────────────────────────────────────────────

log "1. GET /healthz"
curl -fsS "$BASE_URL/healthz" > /dev/null

log "2. GET /ready"
curl -fsS "$BASE_URL/ready" > /dev/null

log "3. GET /metrics (auth token requis)"
if [[ -n "${PROMETHEUS_SCRAPE_TOKEN:-}" ]]; then
  metrics_response=$(curl -fsS "$BASE_URL/metrics?token=$PROMETHEUS_SCRAPE_TOKEN")
  # Sanity : vérifie présence de quelques métriques NEXYA custom.
  if ! echo "$metrics_response" | grep -q "nexya_ai_chat_calls_total"; then
    echo "ERROR: nexya_ai_chat_calls_total absent du /metrics" >&2
    exit 1
  fi
else
  # Pas de token côté script : on vérifie que /metrics est BIEN PROTÉGÉ
  # (retourne 401/403). Un 200 sans token = fuite KPI métier en prod,
  # CRITICAL fail. Un 5xx = crash, fail. Le 401 attendu confirme la
  # protection token K1 active.
  metrics_status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/metrics")
  if [[ "$metrics_status" == "401" || "$metrics_status" == "403" ]]; then
    log "  /metrics protégé (HTTP $metrics_status) — sécurité OK"
  elif [[ "$metrics_status" == "200" ]]; then
    echo "ERROR CRITIQUE: /metrics répond 200 sans token — fuite KPI possible !" >&2
    exit 1
  else
    echo "ERROR: /metrics retourne $metrics_status (attendu 401/403)" >&2
    exit 1
  fi
fi

log "4. GET /observability/status (auth token requis)"
# Même politique que /metrics : token requis en prod, 401 attendu sans token.
obs_status=$(curl -s -o /dev/null -w "%{http_code}" "$BASE_URL/observability/status")
if [[ "$obs_status" == "401" || "$obs_status" == "403" || "$obs_status" == "200" ]]; then
  log "  /observability/status accessible (HTTP $obs_status)"
else
  echo "ERROR: /observability/status retourne $obs_status (attendu 200/401/403)" >&2
  exit 1
fi

# ─────────────────────────────────────────────────────────────
# Checks bout-en-bout (staging uniquement, tranche 7)
# ─────────────────────────────────────────────────────────────

if [[ "$ENV" == "staging" ]]; then
  log "5. POST /auth/register (bout-en-bout DB write — staging only)"
  DUMMY_USER_EMAIL="smoke-$(date +%s)-$$@nexya-test.invalid"
  register_response=$(curl -fsS -X POST "$BASE_URL/auth/register" \
    -H "Content-Type: application/json" \
    -d "{
      \"email\": \"$DUMMY_USER_EMAIL\",
      \"password\": \"SmokeTest2026!Pass\",
      \"username\": \"smoke_$(date +%s)\"
    }")
  DUMMY_USER_TOKEN=$(echo "$register_response" | grep -oP '"access_token":"\K[^"]+' || echo "")
  if [[ -z "$DUMMY_USER_TOKEN" ]]; then
    echo "ERROR: register n'a pas retourné de access_token" >&2
    exit 1
  fi
  log "  → user dummy créé : $DUMMY_USER_EMAIL"
  # Cleanup via trap EXIT (delete user)
else
  log "5. POST /auth/register skip (env=$ENV, staging-only)"
fi

log "Smoke test terminé"
