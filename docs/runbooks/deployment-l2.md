# Runbook — Déploiement L2 staging

> **Pour qui** : Ivan + futur SRE. Procédure pour déployer NEXYA
> backend sur Hetzner CCX23 staging (Phase L2). Prévoit un
> environnement isolé staging.nexya.ai avant prod.

---

## Pré-requis

- Compte Hetzner Cloud
- Domaine `nexya.ai` (Cloudflare DNS)
- Secrets manager au choix (Doppler / 1Password / AWS SSM / sops+age)
- GitHub repo accessible avec secrets configurés
- Image Docker GHCR `ghcr.io/nexyalabs/nexya-backend:vX.Y.Z` (build par
  `release.yml`)

---

## Architecture cible L2

```
Internet
   ↓
Cloudflare DNS + WAF + cert Let's Encrypt managed
   ↓
Hetzner CCX23 (4 vCPU + 16 GB RAM) — IP fixe
   ↓
docker compose stack :
  - nginx (reverse proxy + TLS termination)
  - nexya-backend (FastAPI uvicorn)
  - postgres pgvector pg16
  - redis 7-alpine
  - minio (RELEASE pinned)
  - arq worker (séparé du backend pour isolation)
  - prometheus
  - grafana
  - loki + tempo (V2)
```

---

## PgBouncer (cap connexions à 9M users — audit 2026-05-01 finding S0)

### Pourquoi

Sans PgBouncer, chaque worker uvicorn ouvre `db_pool_size + db_max_overflow`
connexions Postgres directes. À l'échelle :

- 1000 workers × 30 connexions = 30 000 connexions Postgres directes
- Postgres typique plafonne 100-500 connexions par instance
- Saturation immédiate dès 100k users concurrent

**PgBouncer en transaction pooling mode** multiplexe :

- 10 000 clients × ~200 connexions Postgres effectives (ratio 50:1)
- Latence ajoutée : ~0.5 ms (négligeable face à la latence DB native)

### Stack

```
uvicorn → PgBouncer (port 6432, transaction mode) → Postgres (port 5432)
```

### Config livrée (2026-05-01)

- `docker/pgbouncer/pgbouncer.ini` — transaction mode, pool_size=20,
  max_client_conn=10 000, scram-sha-256 auth, `server_reset_query=DISCARD ALL`
- `docker/pgbouncer/userlist.txt.example` — template avec procédure
  génération hash SCRAM (le vrai `userlist.txt` est exclu via `.gitignore`)
- `docker/docker-compose.pgbouncer.yml` — overlay docker-compose pour
  test local optionnel (port host 6433)
- `app/config.py:database_use_pgbouncer` — flag activable via `.env`
- `app/core/database/postgres.py:_build_engine_kwargs` — adapte les
  kwargs SQLAlchemy selon le flag

### Activation L2 staging

1. **Provisioning Postgres + PgBouncer** sur Hetzner staging :

   ```bash
   docker compose -f docker/docker-compose.yml \
                  -f docker/docker-compose.pgbouncer.yml \
                  up -d
   ```

2. **Générer le hash SCRAM-SHA-256 du user `nexya`** :

   ```bash
   docker exec -it nexya-postgres psql -U nexya -d postgres -c \
     "SET password_encryption = 'scram-sha-256';
      ALTER USER nexya WITH PASSWORD 'votre-pwd-fort-secrets-manager';
      SELECT rolname, rolpassword FROM pg_authid WHERE rolname='nexya';"
   ```

   Copier la valeur `rolpassword` (commence par `SCRAM-SHA-256$...`)
   dans `docker/pgbouncer/userlist.txt` (PAS dans `.example`).

3. **Mettre à jour `.env.production`** :

   ```bash
   DATABASE_URL=postgresql+psycopg://nexya:<pwd>@pgbouncer:6432/nexya
   DATABASE_USE_PGBOUNCER=true
   ```

4. **Smoke test** :

   ```bash
   docker compose ... exec backend python -c \
     "import asyncio; from app.core.database.postgres import check_db_connection; \
     print(asyncio.run(check_db_connection()))"
   # → True attendu
   ```

5. **Vérifier les pools depuis PgBouncer** :

   ```bash
   docker exec -it nexya-pgbouncer psql -U nexya -p 6432 pgbouncer -c "SHOW POOLS;"
   ```

### Limitations connues (transaction mode)

- **Pas de LISTEN/NOTIFY persistant** — NEXYA n'en utilise pas (vérifié
  par grep, aucun `LISTEN`/`NOTIFY` dans le code).
- **Pas de prepared statements server-side** — désactivés via
  `prepare_threshold=None` côté psycopg dans
  [`app/core/database/postgres.py`](../../app/core/database/postgres.py).
- **Sessions courtes obligatoires** — chaque transaction libère sa
  connexion serveur. Compatible SQLAlchemy async (qui ouvre/ferme une
  transaction par requête HTTP via `get_db()`).

### Monitoring

Métriques Prometheus à exposer V2 (Phase 14) :

- `pgbouncer_pool_max`, `pgbouncer_clients_active`,
  `pgbouncer_clients_waiting` (via `pgbouncer_exporter`)

Alerte K2 à ajouter Phase 14 : `NexyaPgBouncerSaturation`
(`pgbouncer_clients_waiting > 0` sur 5 min) — signal que le pool serveur
est trop petit, augmenter `default_pool_size` dans `pgbouncer.ini`.

---

## Steps

### 1. Provisioning Hetzner (10 min)

```bash
# Via Hetzner CLI
hcloud server create \
  --type ccx23 \
  --image ubuntu-24.04 \
  --location nbg1 \
  --name nexya-staging \
  --ssh-key <key-id>

# Récupérer IP fixe
hcloud server describe nexya-staging | grep IP
```

### 2. Setup serveur initial (30 min)

```bash
ssh root@<IP>

# Mise à jour
apt update && apt upgrade -y

# Docker
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker

# UFW firewall
ufw default deny incoming
ufw allow ssh
ufw allow 80
ufw allow 443
ufw enable

# Fail2ban (anti brute-force SSH)
apt install -y fail2ban
systemctl enable --now fail2ban

# User non-root deploy
adduser deploy
usermod -aG docker deploy
mkdir -p /home/deploy/.ssh
cp /root/.ssh/authorized_keys /home/deploy/.ssh/
chown -R deploy:deploy /home/deploy/.ssh
```

### 3. DNS Cloudflare (5 min)

```
Cloudflare dashboard → DNS records :
  A     api-staging       <IP>          Proxied
  A     grafana-staging   <IP>          Proxied (token-protected)
```

Cert SSL automatique via Cloudflare Universal SSL.

### 4. Secrets injection (15 min)

Configurer `.env.production` via secrets manager. Variables critiques
(prod safety guard fail-fast au boot si absentes/invalides) :

```bash
# .env.production
ENV=staging  # ou 'production' pour vraie prod
APP_SECRET=<openssl rand -hex 32>
APP_VERSION=v0.4.2  # posé par CI/CD release
APP_COMMIT_SHA=<git SHA>

DATABASE_URL=postgresql+psycopg://nexya:<strong-pwd>@postgres:5432/nexya
REDIS_URL=redis://redis:6379/0
ALLOWED_ORIGINS=https://app.nexya.ai

JWT_PRIVATE_KEY=<contenu private.pem>
JWT_PUBLIC_KEY=<contenu public.pem>

# Providers IA
GEMINI_API_KEY=<...>
OPENAI_API_KEY=<...>
ANTHROPIC_API_KEY=<...>

# Storage
S3_ENDPOINT_URL=http://minio:9000
S3_ACCESS_KEY=<...>
S3_SECRET_KEY=<...>
S3_BUCKET_NAME=nexya-prod

# SaaS
BREVO_API_KEY=<...>
HCAPTCHA_SECRET_KEY=<...>
FCM_SERVICE_ACCOUNT_FILE=/run/secrets/firebase.json
CRISP_WEBSITE_ID=<...>
CRISP_API_KEY=<...>

# Observabilité (O1+K1)
PROMETHEUS_SCRAPE_TOKEN=<openssl rand -hex 32>
GRAFANA_ADMIN_PASSWORD=<strong>
SENTRY_DSN=https://...
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp.your-collector.io
SECURITY_HEADERS_PRESET=prod  # IMPÉRATIF en prod (cf. O1)

# RGPD (J1)
RGPD_ADMIN_EMAILS=dpo@nexya.ai,ivan@nexya.ai
```

### 5. Build & push image GHCR (CI auto)

Le workflow `release.yml` se déclenche sur tag git semver `vX.Y.Z` :

```bash
git tag -a v0.4.2 -m "Release 0.4.2"
git push origin v0.4.2
```

→ image `ghcr.io/nexyalabs/nexya-backend:v0.4.2` disponible.

### 6. Pull + démarrage

```bash
ssh deploy@<IP>
cd /opt/nexya
git clone https://github.com/nexyalabs/nexya nexya
cd nexya/nexya_backend

# Mettre à jour docker-compose.prod.yml avec le tag
sed -i 's/IMAGE_TAG=.*/IMAGE_TAG=v0.4.2/' .env

# Pull image
docker compose -f docker/docker-compose.prod.yml pull

# Smoke test sur image (sans la mettre live)
docker run --rm -e ENV=staging ... ghcr.io/nexyalabs/nexya-backend:v0.4.2 \
    python -c "from app.main import app; print('OK')"

# Migrations
docker compose -f docker/docker-compose.prod.yml run --rm backend \
    alembic upgrade head

# Démarrage stack complète
docker compose -f docker/docker-compose.prod.yml up -d

# Vérifier healthz
curl https://api-staging.nexya.ai/healthz
curl https://api-staging.nexya.ai/ready  # 200 attendu
curl https://api-staging.nexya.ai/version  # version + commit_sha
```

### 7. Smoke tests post-deploy (10 min)

```bash
bash scripts/smoke_test.sh https://api-staging.nexya.ai
# - /healthz, /ready, /metrics, /observability/status
# - POST /auth/register staging-only (cf. ENV=staging)
# - GET /docs accessible (Swagger UI)
```

### 8. Configurer alertes K2 (15 min)

- Importer dashboards Grafana via provisioning (auto via
  `grafana/provisioning/`)
- Configurer destination AlertManager (Slack/email/PagerDuty selon
  choix Ivan)
- Tester une alerte manuellement (forcer 5xx → vérifier notification)

### 9. Configurer secrets GHA Actions (5 min)

Dans GitHub repo Settings → Secrets :
- `GEMINI_API_KEY` (pour evals nightly N3)
- `CRISP_API_KEY`, `CRISP_WEBSITE_ID` (pour escalation N4)
- (placeholders V2) `STRIPE_SECRET_KEY`, `CINETPAY_API_KEY`,
  `NOTCHPAY_SECRET_KEY` (Phase 11)

### 10. Test évals + load V1 contre staging

```bash
# Évals (mock judge — gratuit)
python -m tests.evals --judge=mock --category=all

# Load tests
bash tests/load/run.sh --scenario auth_burst
```

---

## Rollback

Si problème détecté :

```bash
bash scripts/rollback.sh v0.4.1  # tag précédent
```

Le script :
1. Pull image v0.4.1
2. Backup compose YAML
3. Swap tag
4. Down + Up
5. Wait healthz + smoke test
6. Restore .bak si KO + relance

Voir [`scripts/rollback.sh`](../../scripts/rollback.sh) pour le détail.

---

## Backups DB

Cron quotidien (V1 manual setup, V2 ansible/terraform) :

```bash
# /etc/cron.d/nexya-backup
0 3 * * * deploy /opt/nexya/scripts/backup_db.sh
```

**Le script `scripts/backup_db.sh` est livré (2026-05-01)** avec :

- Mode `--dry-run` (validation sans side effects)
- Lock `flock` anti double-run concurrent
- `pg_dump --format=custom --compress=9` (compression maximale)
- SHA-256 systématique pour vérification d'intégrité
- Chiffrement GPG optionnel (`BACKUP_GPG_RECIPIENT` recommandé prod)
- Upload S3 SSE AES256, région configurable
- Cleanup local après `BACKUP_RETENTION_DAYS` jours

Ajouter au `.env.production` (recommandation prod) :

```bash
BACKUP_DIR=/backups
S3_BUCKET=nexya-backups-prod
S3_REGION=eu-central-1
BACKUP_RETENTION_DAYS=7
BACKUP_GPG_RECIPIENT=ops@nexya.ai      # active le chiffrement GPG
```

Voir [`scripts/backup_db.sh`](../../scripts/backup_db.sh) +
[`db-restore.md`](db-restore.md) pour la procédure restore via
`scripts/restore_db.sh`.

---

## TODO post-L2

- [ ] AlertManager runtime + destinations email/Slack/PagerDuty
- [ ] Loki + Tempo (V2 — log aggregation + tracing UI)
- [ ] Configurer hstspreload.org soumission (engagement long terme)
- [ ] Backup encryption GPG (V2)
- [ ] Disaster recovery drill quarterly (V2)
- [ ] Multi-region failover (Phase 19, post 5-10k users payants)
