#!/usr/bin/env bash
# NEXYA Backend — Teardown stack tests de charge (Session N4 volet A)
#
# Stoppe + détruit la stack docker-compose load + purge volumes.
# Idempotent : pas d'erreur si la stack n'est pas démarrée.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "${SCRIPT_DIR}"

echo "▶ NEXYA load test teardown"
docker compose -f docker-compose.load.yml down -v --remove-orphans
echo "✅ Stack purgée"
