# Runbook — Database backup & restore

> **Pour qui** : ops post-incident DB. Procédure backup quotidien
> automatisé + restore manual + drill quarterly.

---

## Backup automatique

### Cron quotidien (V1 manual, V2 ansible)

```bash
# /etc/cron.d/nexya-backup
0 3 * * * deploy /opt/nexya/scripts/backup_db.sh
```

### Script `scripts/backup_db.sh` (à créer V2)

```bash
#!/usr/bin/env bash
set -euo pipefail
TS=$(date -u +%Y%m%d_%H%M%S)
BACKUP_DIR=/backups
S3_BUCKET=nexya-backups

mkdir -p "$BACKUP_DIR"

# Dump compressé
docker exec nexya-postgres pg_dump -U nexya -d nexya \
    --format=custom --compress=9 \
    > "$BACKUP_DIR/nexya-${TS}.dump"

# Hash SHA-256 pour vérification intégrité
sha256sum "$BACKUP_DIR/nexya-${TS}.dump" \
    > "$BACKUP_DIR/nexya-${TS}.dump.sha256"

# Upload S3 (chiffré server-side)
aws s3 cp "$BACKUP_DIR/nexya-${TS}.dump" \
    "s3://$S3_BUCKET/$(date +%Y/%m)/" \
    --sse AES256
aws s3 cp "$BACKUP_DIR/nexya-${TS}.dump.sha256" \
    "s3://$S3_BUCKET/$(date +%Y/%m)/" \
    --sse AES256

# Cleanup local > 7j (S3 conserve 30j via lifecycle policy)
find "$BACKUP_DIR" -name "nexya-*.dump*" -mtime +7 -delete

# Notification Slack (V2)
echo "✅ Backup ${TS} uploaded"
```

### Rétention

- **Local** : 7 jours
- **S3** : 30 jours via lifecycle policy AWS S3
- **Long-terme (off-site)** : V2 — backup mensuel S3 Glacier 1 an
  (RGPD logs sécurité)

---

## Restore manuel

### Cas 1 — Restauration ponctuelle (ex: corruption d'une table)

```bash
# 1. Stopper le backend (downtime)
docker compose -f docker/docker-compose.prod.yml stop backend arq

# 2. Identifier le dump à restaurer
aws s3 ls s3://nexya-backups/2026/04/

# 3. Download
aws s3 cp s3://nexya-backups/2026/04/nexya-20260427_030001.dump \
    /tmp/restore.dump

# 4. Vérifier intégrité
aws s3 cp s3://nexya-backups/2026/04/nexya-20260427_030001.dump.sha256 \
    /tmp/restore.sha256
sha256sum -c /tmp/restore.sha256

# 5. Backup de la DB courante AVANT restore (au cas où)
docker exec nexya-postgres pg_dump -U nexya nexya --format=custom \
    > /tmp/before-restore-$(date -u +%Y%m%d_%H%M%S).dump

# 6. Drop + recreate DB
docker exec -i nexya-postgres psql -U nexya postgres -c \
    "DROP DATABASE IF EXISTS nexya_restore;"
docker exec -i nexya-postgres psql -U nexya postgres -c \
    "CREATE DATABASE nexya_restore;"

# 7. Restore vers DB temporaire
docker exec -i nexya-postgres pg_restore -U nexya -d nexya_restore \
    < /tmp/restore.dump

# 8. Vérifier intégrité
docker exec -i nexya-postgres psql -U nexya nexya_restore -c \
    "SELECT count(*) FROM users;"
docker exec -i nexya-postgres psql -U nexya nexya_restore -c \
    "SELECT version_num FROM alembic_version;"

# 9. Swap les DBs
docker exec -i nexya-postgres psql -U nexya postgres -c \
    "ALTER DATABASE nexya RENAME TO nexya_old;"
docker exec -i nexya-postgres psql -U nexya postgres -c \
    "ALTER DATABASE nexya_restore RENAME TO nexya;"

# 10. Redémarrer backend
docker compose -f docker/docker-compose.prod.yml start backend arq

# 11. Smoke test
curl https://api.nexya.ai/healthz
curl https://api.nexya.ai/ready

# 12. Si tout OK, drop nexya_old après 24h
docker exec -i nexya-postgres psql -U nexya postgres -c \
    "DROP DATABASE nexya_old;"
```

### Cas 2 — Restauration totale (disaster recovery)

Procédure si le serveur Hetzner est entièrement perdu :

1. **Provisioning nouveau serveur Hetzner** (cf.
   [`deployment-l2.md`](deployment-l2.md))
2. **Pull image Docker** dernière version
3. **Restore le dump le plus récent** (étapes 4-9 ci-dessus, mais sur
   DB neuve)
4. **Mettre à jour Cloudflare DNS** vers nouvelle IP
5. **Smoke tests + alertes vertes**
6. **Notification users** (incident page Crisp + email V2)

**RPO target** : 24h (perte max 1 jour de données)
**RTO target** : 4h (temps max indisponibilité)

V2 : RPO 1h via WAL streaming Postgres + replica passif + RTO 30 min.

---

## Drill quarterly

**Tous les 3 mois**, exécuter un drill restore :

1. Provisioning serveur de test temporaire (Hetzner CCX21)
2. Download dernier backup S3
3. Restore complet sur serveur de test
4. Vérifier intégrité (count users, last_migration, sample row spot
   check)
5. Mesurer RTO réel (temps total restore)
6. Détruire serveur de test

**Date prochain drill** : 2026-07-27 (TODO Ivan).

---

## Restauration partielle (point-in-time recovery)

V2 — actuellement non supporté V1.

V2 plan : activer Postgres WAL archiving + `pg_basebackup`
hebdomadaire + WAL streaming continue → restauration à n'importe
quel timestamp dans les 7 derniers jours.

---

## Vérification d'intégrité

Post-restore, runner ces vérifs :

```sql
-- Count par table critique
SELECT 'users', count(*) FROM users
UNION ALL SELECT 'conversations', count(*) FROM conversations
UNION ALL SELECT 'messages', count(*) FROM messages
UNION ALL SELECT 'ai_calls', count(*) FROM ai_calls;

-- Migration alignée
SELECT version_num FROM alembic_version;

-- Foreign keys cohérentes
SELECT count(*) FROM messages WHERE conversation_id NOT IN
    (SELECT id FROM conversations);  -- doit être 0

-- Indexes présents
SELECT count(*) FROM pg_indexes WHERE schemaname = 'public';

-- Extensions actives
SELECT extname FROM pg_extension;
-- attendu : plpgsql, vector, pg_trgm
```

---

## RGPD considerations

Les backups contiennent **toutes les données users**. Il faut donc :

1. **Chiffrer S3** (SSE AES256 — déjà dans le script)
2. **ACL stricte** sur le bucket S3 (IAM policy : seuls les rôles
   `backup-writer` et `admin-restorer` y accèdent)
3. **Supprimer un user purgé RGPD post-deletion-request** :
   - Le hard delete via cron `purge_deleted_accounts` supprime de
     la DB live
   - Mais les **backups continuent de contenir** les données
     pendant 30 jours (rétention S3)
   - **Acceptable RGPD** : les backups sont un cas explicite de
     « durée nécessaire au traitement » (Article 5.1.e)
   - V2 : tag les rows purged dans backup metadata, exclure du
     restore pour anonymisation

4. **Vol de backup** = data breach RGPD Article 33 → notification
   CNIL 72h. Voir [`incident-response.md`](incident-response.md).
