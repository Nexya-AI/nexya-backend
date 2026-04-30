#!/usr/bin/env bash
# NEXYA Backend — Helper release locale (Session L1)
#
# Usage :
#   bash scripts/release.sh patch    # 0.1.2 → 0.1.3
#   bash scripts/release.sh minor    # 0.1.3 → 0.2.0
#   bash scripts/release.sh major    # 0.2.0 → 1.0.0
#
# Pipeline :
#   1. Vérifie branche `main` + à jour avec origin
#   2. Lit version courante dans pyproject.toml
#   3. Calcule nouvelle version selon bump type
#   4. Update pyproject.toml + commit `release: prepare vX.Y.Z`
#      (PAS de Conventional Commits préfixe — règle NEXYA)
#   5. Crée tag annotated `vX.Y.Z`
#   6. Push origin main + tags → déclenche release.yml
#   7. Affiche URL de la release GitHub à ouvrir manuellement

set -euo pipefail

readonly BUMP_TYPE="${1:-}"
readonly PYPROJECT="pyproject.toml"

usage() {
  cat <<EOF
Usage: $0 {patch|minor|major}

Bump la version du projet, crée un tag git annotated, et push.
Le push du tag déclenche le workflow release.yml qui build et push
l'image Docker sur GHCR.
EOF
}

if [[ "$BUMP_TYPE" != "patch" && "$BUMP_TYPE" != "minor" && "$BUMP_TYPE" != "major" ]]; then
  echo "ERROR: bump type invalide" >&2
  usage
  exit 1
fi

log() {
  echo "[release] $*"
}

# 1. Vérifie branche main + à jour
current_branch=$(git rev-parse --abbrev-ref HEAD)
if [[ "$current_branch" != "main" ]]; then
  echo "ERROR: tu n'es pas sur main (branche courante : $current_branch)" >&2
  exit 1
fi

log "Pull origin main (fast-forward only)"
git pull --ff-only origin main

# 2. Lit version courante
current_version=$(grep -E '^version\s*=' "$PYPROJECT" | head -1 | sed -E 's/version\s*=\s*"([^"]+)"/\1/')
if [[ -z "$current_version" ]]; then
  echo "ERROR: impossible de lire la version dans $PYPROJECT" >&2
  exit 1
fi
log "Version courante : $current_version"

# 3. Calcule nouvelle version
IFS='.' read -r major minor patch <<< "$current_version"
case "$BUMP_TYPE" in
  patch)
    new_version="$major.$minor.$((patch + 1))"
    ;;
  minor)
    new_version="$major.$((minor + 1)).0"
    ;;
  major)
    new_version="$((major + 1)).0.0"
    ;;
esac
log "Nouvelle version : $new_version (bump $BUMP_TYPE)"

# 4. Update pyproject.toml + commit
log "Update $PYPROJECT"
sed -i.bak -E "s/^version\s*=\s*\"[^\"]+\"/version = \"$new_version\"/" "$PYPROJECT"
rm -f "$PYPROJECT.bak"

git add "$PYPROJECT"
# Format commit : `release: prepare vX.Y.Z` (pas de Conventional Commits
# préfixe `chore(release):` — règle NEXYA feedback_git_commits.md).
git commit -m "release: prepare v$new_version"

# 5. Tag annotated
log "Création tag annotated v$new_version"
git tag -a "v$new_version" -m "Release v$new_version"

# 6. Push branch + tags
log "Push origin main + tags"
git push origin main
git push origin "v$new_version"

# 7. Affiche URL release
remote_url=$(git config --get remote.origin.url | sed -E 's/\.git$//')
# Convertit git@github.com:owner/repo en https://github.com/owner/repo
release_url="${remote_url/git@github.com:/https://github.com/}"
release_url="$release_url/releases/tag/v$new_version"

log "✅ Release v$new_version pushée"
log "   GitHub Actions release.yml en cours d'exécution"
log "   URL release : $release_url"
log ""
log "Wait ~5 min puis vérifier la release sur GitHub + image GHCR :"
log "   docker pull ghcr.io/<owner>/nexya-backend:v$new_version"
