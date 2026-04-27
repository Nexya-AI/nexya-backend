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

`scripts/backup_db.sh` (à créer V2) :
```bash
#!/usr/bin/env bash
set -euo pipefail
TS=$(date -u +%Y%m%d_%H%M%S)
docker exec nexya-postgres pg_dump -U nexya nexya \
    | gzip > /backups/nexya-${TS}.sql.gz
# Upload S3 backup bucket (rétention 30j)
aws s3 cp /backups/nexya-${TS}.sql.gz s3://nexya-backups/
# Cleanup local > 7j
find /backups -name "*.sql.gz" -mtime +7 -delete
```

Voir [`db-restore.md`](db-restore.md) pour la restauration.

---

## TODO post-L2

- [ ] AlertManager runtime + destinations email/Slack/PagerDuty
- [ ] Loki + Tempo (V2 — log aggregation + tracing UI)
- [ ] Configurer hstspreload.org soumission (engagement long terme)
- [ ] Backup encryption GPG (V2)
- [ ] Disaster recovery drill quarterly (V2)
- [ ] Multi-region failover (Phase 19, post 5-10k users payants)
