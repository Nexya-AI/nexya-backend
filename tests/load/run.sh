#!/usr/bin/env bash
# NEXYA Load Tests — Orchestrateur (Session N4 volet A).
#
# Lance un (ou tous les) scenario(s) k6 contre la stack docker-compose
# load + écrit les rapports HTML+JSON dans tests/load/reports/.
#
# Usage :
#   bash tests/load/run.sh                      # tous les scénarios
#   bash tests/load/run.sh --scenario auth_burst
#   bash tests/load/run.sh --scenario chat_stream_concurrent --no-teardown
#   bash tests/load/run.sh --skip-bootstrap     # stack déjà up
#
# Exit codes :
#   0 = tous les scénarios respectent leurs thresholds
#   1 = au moins 1 threshold breach
#   2 = erreur d'invocation / k6 absent / docker absent

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOAD_DIR="${SCRIPT_DIR}"
SCENARIOS_DIR="${LOAD_DIR}/scenarios"
REPORTS_DIR="${LOAD_DIR}/reports"

# ─── Arg parsing ──────────────────────────────────────────────
SCENARIO="all"
SKIP_BOOTSTRAP=0
NO_TEARDOWN=0

while [ "$#" -gt 0 ]; do
    case "$1" in
        --scenario)
            SCENARIO="$2"
            shift 2
            ;;
        --skip-bootstrap)
            SKIP_BOOTSTRAP=1
            shift
            ;;
        --no-teardown)
            NO_TEARDOWN=1
            shift
            ;;
        -h|--help)
            sed -n '1,18p' "$0" | grep -E '^#' | sed 's/^# *//'
            exit 0
            ;;
        *)
            echo "Argument inconnu: $1" >&2
            exit 2
            ;;
    esac
done

# ─── Pré-vérifs ───────────────────────────────────────────────
if ! command -v k6 >/dev/null 2>&1; then
    echo "✗ k6 absent. Installer : https://k6.io/docs/get-started/installation/" >&2
    exit 2
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "✗ docker absent." >&2
    exit 2
fi

mkdir -p "${REPORTS_DIR}"

# ─── Bootstrap stack ──────────────────────────────────────────
if [ "${SKIP_BOOTSTRAP}" -eq 0 ]; then
    bash "${LOAD_DIR}/docker/bootstrap.sh"
else
    echo "▶ Skip bootstrap (--skip-bootstrap)"
fi

# ─── Sélection scénarios ──────────────────────────────────────
declare -a SCENARIO_FILES
if [ "${SCENARIO}" = "all" ]; then
    SCENARIO_FILES=(
        "auth_burst"
        "chat_stream_concurrent"
        "files_upload_concurrent"
        "conversations_list_paginated"
        "metrics_endpoint"
        "mixed_workload"
    )
else
    SCENARIO_FILES=("${SCENARIO}")
fi

# ─── Trap teardown ────────────────────────────────────────────
function _cleanup() {
    local rc=$?
    if [ "${NO_TEARDOWN}" -eq 0 ] && [ "${SKIP_BOOTSTRAP}" -eq 0 ]; then
        echo ""
        echo "▶ Teardown..."
        bash "${LOAD_DIR}/docker/teardown.sh" || true
    fi
    exit "${rc}"
}
trap _cleanup EXIT INT TERM

# ─── Run k6 ───────────────────────────────────────────────────
TIMESTAMP="$(date -u +%Y%m%d_%H%M%S)"
OVERALL_RC=0

for name in "${SCENARIO_FILES[@]}"; do
    script="${SCENARIOS_DIR}/${name}.js"
    if [ ! -f "${script}" ]; then
        echo "✗ Scénario manquant : ${script}" >&2
        OVERALL_RC=2
        continue
    fi
    json_out="${REPORTS_DIR}/${name}_${TIMESTAMP}.json"
    summary_out="${REPORTS_DIR}/${name}_${TIMESTAMP}_summary.json"

    echo ""
    echo "▶▶▶ Run scenario : ${name}"
    set +e
    BASE_URL="${BASE_URL:-http://localhost:8000}" k6 run \
        --out "json=${json_out}" \
        --summary-export="${summary_out}" \
        "${script}"
    rc=$?
    set -e

    if [ "${rc}" -ne 0 ]; then
        echo "✗ Threshold breach sur ${name} (rc=${rc})" >&2
        OVERALL_RC=1
    fi
done

if [ "${OVERALL_RC}" -eq 0 ]; then
    echo ""
    echo "✅ Tous les scénarios respectent leurs thresholds"
else
    echo ""
    echo "❌ Au moins 1 scénario a breach les thresholds (rc=${OVERALL_RC})"
fi

exit "${OVERALL_RC}"
