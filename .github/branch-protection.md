# NEXYA Backend — Configuration Branch Protection (manuelle)

> **Session L1 (2026-04-26)** — instructions pour Ivan : configurer
> manuellement les règles Branch Protection sur GitHub UI une fois
> les workflows CI/CD livrés. Cette config ne peut pas être versionnée
> en YAML côté repo (limitation GitHub V1) — elle vit dans Settings.

## Pré-requis

1. Le repo NEXYA Backend doit être hébergé sur GitHub.
2. Les 4 workflows GHA livrés en L1 doivent être actifs (au moins un
   premier run de chaque sur `main` ou une PR pour qu'ils apparaissent
   dans la liste des status checks).

## Étapes — UI GitHub

1. Aller sur le repo → **Settings** → **Branches** (sidebar gauche).
2. Cliquer **Add branch protection rule** (ou éditer la règle existante
   sur `main`).
3. **Branch name pattern** : `main`
4. Cocher les options suivantes :

### Pull request requirements
- [x] **Require a pull request before merging**
  - [x] Require approvals : `1` (V1 solo dev — peut rester `0` si Ivan
    travaille seul, à monter à `1` quand des contributors arrivent)
  - [x] **Dismiss stale pull request approvals when new commits are pushed**
  - [ ] Require review from Code Owners (V2 si CODEOWNERS défini)

### Status checks
- [x] **Require status checks to pass before merging**
  - [x] **Require branches to be up to date before merging**
  - **Status checks required** (cocher les 6 jobs CI) :
    - `Lint (ruff)`
    - `Typecheck (mypy)`
    - `Security scan (bandit + pip-audit)`
    - `Tests (pytest + coverage)`
    - `Docker build (validate image)`
    - `Migrations reversibility check`

### Conversation
- [x] **Require conversation resolution before merging**

### Linear history
- [x] **Require linear history**
  > Force squash ou rebase merges, refuse merge commits classiques.
  > Garde l'historique propre + facile à `git bisect`.

### Signed commits (V2)
- [ ] Require signed commits (V2 quand Ivan génère sa GPG key)

### Push restrictions
- [x] **Restrict who can push to matching branches**
  - Autoriser uniquement les **admins** + GitHub Actions bot

### Allow force pushes / deletions
- [ ] Allow force pushes (refusé sur main)
- [ ] Allow deletions (refusé sur main)

### Settings additionnels
- [x] **Do not allow bypassing the above settings**
  > Même les admins doivent passer par PR (sinon la règle ne sert à
  > rien).

## Vérification post-config

1. Créer une branche test :
   ```bash
   git checkout -b test/branch-protection
   echo "test" >> README.md
   git add README.md && git commit -m "test: branch protection"
   git push origin test/branch-protection
   gh pr create --title "Test BP" --body "Verify branch protection"
   ```
2. Le bouton « Merge » de la PR doit être grisé tant que les 6 status
   checks ne sont pas verts.
3. Tenter `git push origin main` direct → doit être refusé.

## Quand modifier ces règles

- **Plus de contributors** (V2) : monter approvals à `2` + activer
  CODEOWNERS sur les fichiers critiques (`app/core/auth/`, `migrations/`,
  `.github/workflows/`).
- **Audit sécurité externe** (M2) : activer `Require signed commits`
  + ajouter `dependency-review` action en status check.
- **Hotfix process** : créer une exception temporaire `hotfix/*`
  pattern avec moins de checks (à supprimer après usage).

## Activation des Dependabot security alerts (gratuit, recommandé)

Settings → **Code security and analysis** → activer :
- [x] Dependency graph
- [x] Dependabot alerts
- [x] Dependabot security updates
- [x] Secret scanning (gratuit pour repos publics)
- [x] Push protection (refuse les commits contenant des secrets connus)
