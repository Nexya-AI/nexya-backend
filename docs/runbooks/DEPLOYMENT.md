# Runbook — Déploiement production NEXYA (stack auto-hébergée)

> **Doc de référence opérationnelle.** Topologie « tout-en-un auto-hébergé » V1 :
> tout NEXYA tourne dans `docker compose` sur un seul VPS Hetzner CX32, derrière
> Caddy (HTTPS automatique), DNS chez Namecheap.
>
> Pour le déploiement L2 staging Cloudflare/nginx historique, voir
> [`deployment-l2.md`](deployment-l2.md) (conservé, non utilisé en V1).

---

## 1. Architecture cible

```
                  📱 APK Android (testeurs)
                          │  HTTPS
                          ▼
            api.nexyalabs.com   (DNS A @ Namecheap → IPv4 VPS)
                          │
                          ▼
   ┌──────────────────────────────────────────────────────────┐
   │            VPS HETZNER CX32 — Ubuntu 24.04                 │
   │   UFW : ports ouverts 22, 80, 443 SEULEMENT                │
   │                                                            │
   │   docker compose (réseau interne « nexya-prod ») :         │
   │     caddy        :80/:443  — HTTPS auto Let's Encrypt      │
   │       └─ reverse_proxy ──► nexya-api :8000 (uvicorn ×4)    │
   │                              │      │      │               │
   │                              ▼      ▼      ▼               │
   │                         postgres  redis  minio :9000      │
   │                          pgvector  7    stockage objet    │
   │                              ▲                             │
   │                         nexya-worker (arq — crons + jobs)  │
   │                                                            │
   │   Volumes persistants : postgres_data, redis_data,         │
   │                  minio_data, caddy_data, nexya_logs        │
   │   Backups : /opt/nexya/backups (cron pg_dump quotidien)    │
   └────────────────────────────────────────────────────────────┘
```

- **Seul Caddy publie des ports** (80/443). `postgres`/`redis`/`minio` ne sont
  joignables que depuis le réseau Docker interne.
- **Migration vers des services managés** (Postgres/Redis managés, replica de
  lecture, multi-region) = **Phase 19**, post 5-10k users payants. Pas avant.

---

## 2. Fichiers clés du dépôt

| Fichier | Rôle |
|---------|------|
| `docker/docker-compose.prod.yml` | Stack 6 services auto-hébergée |
| `docker/Caddyfile` | Reverse proxy + HTTPS auto |
| `.env.production.example` | Template des variables (le `.env.production` réel n'est jamais commité) |
| `scripts/server-setup.sh` | Bootstrap du VPS (idempotent) |
| `scripts/generate-secrets.sh` | Génère secrets + clés JWT |
| `scripts/deploy.sh` | Déploiement d'une version (le « 5 min ») |
| `scripts/rollback.sh` | Rollback (alternative à `deploy.sh <tag>`) |
| `scripts/backup_db.sh` / `restore_db.sh` | Sauvegarde / restauration DB |
| `.github/workflows/deploy.yml` | CD automatique (SSH post-release) |

---

## 3. Premier déploiement (résumé)

> Procédure détaillée pas à pas : voir `DEPLOIEMENT_BACKEND_NEXYA_DIRECTIVE_COMPLETE.md`
> (phases D4 → D8). Résumé condensé ci-dessous.

1. **VPS** — Hetzner CX32, Ubuntu 24.04, Falkenstein/Nuremberg. Noter l'IPv4.
2. **DNS** — Namecheap → Advanced DNS → `A` record, Host `api`, Value `<IPv4>`.
   Vérifier la propagation : `nslookup api.nexyalabs.com`.
3. **Bootstrap** — copier `server-setup.sh` sur le VPS, `bash server-setup.sh`.
4. **Cloner le repo** :
   ```bash
   cd /opt/nexya
   git clone git@github.com:Nexya-AI/nexya-backend.git .
   ```
5. **Authentifier GHCR** (image privée) :
   ```bash
   echo "<PAT_read:packages>" | docker login ghcr.io -u <user-github> --password-stdin
   ```
6. **Générer les secrets** :
   ```bash
   bash scripts/generate-secrets.sh
   ```
7. **Créer `.env.production`** :
   ```bash
   cp .env.production.example .env.production
   # Remplir : coller le bloc de generate-secrets.sh + les clés des comptes externes.
   ```
8. **Déclencher la release** (depuis la machine de dev) :
   ```bash
   bash scripts/release.sh patch    # ou : git tag v1.0.0 && git push origin v1.0.0
   ```
   → `release.yml` build + pousse l'image GHCR (~5-8 min).
9. **Déployer** sur le VPS :
   ```bash
   bash scripts/deploy.sh v1.0.0
   ```

---

## 4. Déployer une nouvelle version — le « 5 minutes »

### Option A — Automatique (recommandé)

```
1. Coder la feature, commiter, pousser sur main.
2. bash scripts/release.sh patch     (ou git tag vX.Y.Z && git push origin vX.Y.Z)
3. … c'est tout.
```

`release.yml` builde l'image → `deploy.yml` se connecte en SSH au VPS et lance
`deploy.sh` (migrations + redémarrage). **Aucune intervention manuelle.**

> Pré-requis CD : 3 secrets GitHub configurés (cf. §8).

### Option B — Manuel (filet de sécurité)

```bash
ssh root@<IPv4-VPS>
cd /opt/nexya
bash scripts/deploy.sh v1.1.0
```

`deploy.sh` : `git pull` infra → `docker compose pull` → migrations Alembic →
`up -d` → attente healthcheck → smoke test.

---

## 5. Rollback

Si un déploiement casse quelque chose, **redéployer la version précédente** :

```bash
bash scripts/deploy.sh v1.0.0      # tag précédent
# ou : bash scripts/rollback.sh v1.0.0
```

---

## 6. Opérations courantes

Toutes les commandes se lancent depuis `/opt/nexya` sur le VPS.

```bash
# Raccourci : on préfixe toujours par le fichier prod + l'env-file.
alias dc='docker compose -f docker/docker-compose.prod.yml --env-file .env.production'

# État des services
dc ps
docker stats --no-stream

# Logs
dc logs --tail=100 nexya-api
dc logs -f nexya-worker

# Accès psql à la base
dc exec postgres psql -U nexya -d nexya

# Migrations manuelles
dc run --rm nexya-api alembic upgrade head

# Redémarrer un service
dc restart nexya-api

# Console MinIO — depuis ta machine, via tunnel SSH :
ssh -L 9001:localhost:9001 root@<IPv4-VPS>
#   puis ouvrir http://localhost:9001 dans le navigateur.
```

### Healthchecks

| URL | Rôle | Code attendu |
|-----|------|--------------|
| `GET /healthz` | Liveness (toujours 200) | 200 |
| `GET /ready` | Readiness (DB + Redis + queue) | 200 / 503 si dégradé |
| `GET /version` | Version publique | 200 |
| `GET /metrics` | Prometheus (token requis) | 200 |

---

## 7. Backups & restauration

### Backup quotidien (cron)

`scripts/backup_db.sh` fait un `pg_dump` du conteneur `nexya-postgres`,
compresse, calcule le SHA-256 et applique une rétention de 7 jours.

Installer le cron (une seule fois, sur le VPS) :

```bash
sudo tee /etc/cron.d/nexya-backup > /dev/null <<'EOF'
# Backup DB NEXYA — tous les jours à 03:00 UTC, vers /opt/nexya/backups (local).
0 3 * * * root cd /opt/nexya && BACKUP_DIR=/opt/nexya/backups S3_BUCKET=test-skip bash scripts/backup_db.sh >> /var/log/nexya-backup.log 2>&1
EOF
```

- `S3_BUCKET=test-skip` → backup **local uniquement** sur le VPS (V1, par
  défaut). Aucun outil ni compte AWS requis.
- Pour un backup **offsite** (option future) : il faut un bucket
  **S3-compatible** (Cloudflare R2, AWS S3, Garage…), installer `awscli` sur le
  VPS, puis poser `S3_BUCKET=<bucket> S3_REGION=<region>` + les credentials.
  La Hetzner Storage Box (SFTP) n'est pas S3-compatible — elle demanderait
  rclone/rsync à la place.
- Vérifier un backup manuel : `bash scripts/backup_db.sh --dry-run`.

### Restauration

```bash
bash scripts/restore_db.sh <fichier_backup>
```

Voir [`db-restore.md`](db-restore.md) pour la procédure complète.

---

## 8. Secrets GitHub pour le CD (`deploy.yml`)

Repo GitHub → **Settings → Secrets and variables → Actions** → *New repository secret* :

| Secret | Valeur |
|--------|--------|
| `VPS_HOST` | IPv4 du VPS |
| `VPS_USER` | `root` (V1) |
| `VPS_SSH_KEY` | contenu de la clé SSH **privée** (ed25519) |

La clé **publique** correspondante doit être dans `authorized_keys` du VPS
(c'est la même clé que celle utilisée pour le SSH manuel).

---

## 9. Dépannage

| Symptôme | Cause probable | Solution |
|----------|----------------|----------|
| Caddy n'obtient pas le certificat TLS | DNS pas encore propagé | Attendre la propagation (`nslookup api.nexyalabs.com`) puis `dc restart caddy` |
| `docker compose pull` → `denied` / `unauthorized` | VPS non authentifié à GHCR | Refaire `docker login ghcr.io` avec le PAT `read:packages` |
| `/ready` renvoie 503 | DB ou Redis KO | `dc ps` + `dc logs postgres` / `dc logs redis` |
| API ne démarre pas, fail-fast au boot | `.env.production` invalide | Lire les logs (`dc logs nexya-api`) : le validateur liste les variables fautives |
| RAM saturée (CX32 = 8 Go) | Pic de charge | Le swapfile 2 Go absorbe ; surveiller `docker stats` ; envisager CX42 si récurrent |
| Migrations échouent | Conflit de schéma | NE PAS forcer ; lire l'erreur Alembic, corriger, redéployer |

---

## 10. Coûts mensuels (régime test/lancement)

| Poste | Coût |
|-------|------|
| VPS Hetzner CX32 | ~6,50 € |
| Gemini + OpenAI (faible volume) | ~10-35 $ |
| Brevo / hCaptcha / Sentry / GHCR | gratuit (tiers gratuits) |
| **Total phase test** | **~20-45 € / mois** |
