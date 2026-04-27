# NEXYA Backend — Makefile (Session L1)
#
# Orchestre les commandes locales fréquentes. `make help` génère
# automatiquement l'aide depuis les commentaires `## ...` de chaque
# target.
#
# Usage : `make <target>` (ex: `make ci` pour le pipeline CI complet
# en local avant push).

.DEFAULT_GOAL := help
.PHONY: help install test test-fast lint format typecheck security \
        build run migrate seed coverage clean ci check

# ── 1/16 ─────────────────────────────────────────────────────
help:           ## Affiche cette aide (cible par défaut)
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-15s\033[0m %s\n", $$1, $$2}'

# ── 2/16 ─────────────────────────────────────────────────────
install:        ## Installe les deps + dev deps via uv
	uv pip install -e ".[dev]"
	@echo "✅ Deps installées. Lancer 'make ci' pour valider."

# ── 3/16 ─────────────────────────────────────────────────────
test:           ## Run pytest full suite (avec warnings désactivés)
	pytest tests/ -p no:warnings -q

# ── 4/16 ─────────────────────────────────────────────────────
test-fast:      ## pytest sans tests live optionnels (skip @pytest.mark.live)
	pytest tests/ -p no:warnings -q -m "not live"

# ── 5/16 ─────────────────────────────────────────────────────
lint:           ## ruff check + format check (sans modifier les fichiers)
	ruff check .
	ruff format --check .

# ── 6/16 ─────────────────────────────────────────────────────
format:         ## ruff format (modifie les fichiers en place)
	ruff format .
	ruff check . --fix

# ── 7/16 ─────────────────────────────────────────────────────
typecheck:      ## mypy app/ (V1 assoupli, durcissement Phase 19)
	mypy app/

# ── 8/16 ─────────────────────────────────────────────────────
security:       ## bandit + pip-audit (CVE deps + patterns dangereux)
	bandit -r app/ -ll -c pyproject.toml
	pip-audit --strict --desc --skip-editable

# ── 9/16 ─────────────────────────────────────────────────────
build:          ## Build Docker image locale
	docker build -f docker/Dockerfile -t nexya-backend:local .

# ── 10/16 ────────────────────────────────────────────────────
run:            ## Lance uvicorn dev (port 8000, reload activé)
	uvicorn app.main:app --reload --port 8000

# ── 11/16 ────────────────────────────────────────────────────
migrate:        ## alembic upgrade head (applique les migrations DB)
	alembic upgrade head

# ── 12/16 ────────────────────────────────────────────────────
seed:           ## Peuple la DB avec des données de test (free@/pro@)
	python -m scripts.seed_dev

# ── 13/16 ────────────────────────────────────────────────────
coverage:       ## pytest avec coverage HTML (ouvre htmlcov/index.html)
	coverage run -m pytest tests/ -p no:warnings -q
	coverage report --skip-covered
	coverage html
	@echo "📊 Coverage HTML : htmlcov/index.html"

# ── 14/16 ────────────────────────────────────────────────────
clean:          ## Clean caches Python + Docker + coverage
	rm -rf .pytest_cache .mypy_cache .ruff_cache htmlcov .coverage coverage.xml
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@echo "🧹 Caches nettoyés"

# ── 15/16 ────────────────────────────────────────────────────
ci:             ## Run TOUS les checks comme en CI (lint+typecheck+security+test)
	@echo "🔍 1/4 lint"
	@$(MAKE) lint
	@echo "🔍 2/4 typecheck"
	@$(MAKE) typecheck
	@echo "🔍 3/4 security"
	@$(MAKE) security
	@echo "🔍 4/4 tests"
	@$(MAKE) test
	@echo "✅ CI local OK"

# ── 16/16 ────────────────────────────────────────────────────
check:          ## Alias court de 'make ci' pour Ivan
	@$(MAKE) ci

# ── 17/19 — DD-ready exports (Session O2) ────────────────────
export-openapi: ## Dump app.openapi() → docs/api/openapi.json
	python -m scripts.export_openapi

# ── 18/19 ────────────────────────────────────────────────────
export-schema:  ## Dump pg_dump --schema-only → docs/architecture/schema.sql
	bash scripts/export_schema.sh

# ── 19/19 ────────────────────────────────────────────────────
export-dd:      ## Re-génère openapi.json + schema.sql (DD freshness)
	@$(MAKE) export-openapi
	@$(MAKE) export-schema
