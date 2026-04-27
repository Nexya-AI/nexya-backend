# Runbook — Incident Response

> **Pour qui** : ops 3h du matin réveillé par un pager. 5 incidents
> majeurs documentés avec procédure step-by-step + escalation matrix.

---

## Escalation matrix

| Sévérité | Délai notif | Escalation |
|---|---|---|
| Critical (5xx > 5%, breaker open primary, RGPD breach) | Immédiat | Ivan (CTO) → DPO ext |
| High (LLM unavailable, FCM massive failure, queue saturated) | 30 min | Ivan |
| Medium (5xx 1-5%, slow responses, single provider down) | 2h | Ivan via Slack/email |
| Low (warnings, degraded staging) | Daily digest | Ivan via GitHub issue |

---

## Incident 1 — 5xx spike

**Symptôme** : Grafana dashboard `nexya-overview` affiche 5xx rate > 1%
sur 5 min OU alerte `Nexya5xxRateHigh` déclenchée.

**Procédure** :
1. **Vérifier `/observability/status`** :
   ```bash
   curl https://api.nexya.ai/observability/status \
     -H "X-Prometheus-Token: $TOKEN"
   ```
2. **Identifier le provider en panne** dans Grafana
   `nexya-ai` panel "provider failures" → quel `provider+model`.
3. **Vérifier circuit breaker** :
   - Si `nexya_ai_circuit_breaker_state == 2` (OPEN) → attendre
     30s cooldown automatique
   - Si stuck OPEN > 5 min → restart pod `kubectl rollout restart`
4. **Vérifier Sentry** pour la stack trace top issue.
5. **Vérifier statuts publics providers** :
   - https://status.openai.com/
   - https://status.anthropic.com/
   - https://status.cloud.google.com/
6. **Mitigation** : si LLM down massif → poser `EMBEDDINGS_MOCK_ENABLED=true`
   provisoirement (V2 — kill-switch via secrets manager).

---

## Incident 2 — LLM unavailable cascade

**Symptôme** : alerte `NexyaBreakerOpen` sur primary + secondary
providers. Tous les `/chat/stream` retournent 503 `LLM_UNAVAILABLE`.

**Procédure** :
1. Vérifier statuts publics (cf. Incident 1).
2. Vérifier `OPENROUTER_API_KEY` configurée → fallback chain
   `general` ajoute OpenRouter en 3ème.
3. Si tous les providers cloud sont down → **mode dégradé** :
   - Annoncer maintenance via Crisp public message
   - Désactiver `/chat/stream` côté Cloudflare WAF rule (V2 —
     procédure manuelle V1)
4. Investiguer cause racine après recovery (timeout DNS ?
   rate limit aggregator ? quota OpenAI ? règle Cloudflare ?).
5. Post-mortem dans 24h.

---

## Incident 3 — DB pool saturated

**Symptôme** : `/ready` retourne 503 + `db.latency_ms > 1000`. Logs
backend `database.connection_failed` répétés.

**Procédure** :
1. **Vérifier Postgres usage** :
   ```bash
   psql $DATABASE_URL -c "SELECT count(*) FROM pg_stat_activity;"
   psql $DATABASE_URL -c "SELECT * FROM pg_stat_activity WHERE state != 'idle';"
   ```
2. **Identifier les queries long-running** :
   ```sql
   SELECT pid, now() - query_start AS duration, query
   FROM pg_stat_activity
   WHERE state = 'active' AND query_start < now() - interval '30 seconds'
   ORDER BY duration DESC;
   ```
3. **Kill une query bloquante** (en dernier recours) :
   ```sql
   SELECT pg_terminate_backend(<pid>);
   ```
4. **Augmenter pool size** temporairement (`DB_POOL_SIZE` env var +
   restart) — V2 = auto-scaling.
5. **Investiguer N+1 query** ou index manquant via Sentry/OTel
   spans `db.execute`.

---

## Incident 4 — Payment webhook failure

**Symptôme** : alerte custom (V2 — pas encore créée) ou ticket
Crisp escalation Phase 18 hook avec category=`payment`.

**Procédure** :
1. **Vérifier signature HMAC** : si invalid → attaque possible OU
   provider a changé sa clé. Vérifier dashboard provider.
2. **Vérifier idempotence `processed_webhooks`** : si row existe déjà
   → webhook re-livré, ignorer silencieusement (200 OK).
3. **Vérifier statut compte provider** (CinetPay/NotchPay/Stripe
   dashboard).
4. **Recovery manuel** : si paiement réel mais webhook KO en boucle
   → mark le subscription manuellement via UPDATE SQL + audit
   `auth_events` event `manual_payment_recovery`.
5. **Notification user** : si user a payé mais sub pas active après
   30 min → email manuel + remboursement géré au cas par cas.

---

## Incident 5 — RGPD data breach (notification 72h)

**Symptôme** : suspicion ou détection avérée de fuite de données
(intrusion, leak via dépendance compromise, accès non autorisé admin).

**Procédure** :
1. **CONTAINMENT immédiat** :
   - Couper accès du compte/clé compromise
   - Rotate `JWT_PRIVATE_KEY` + révoquer tous les refresh tokens
     (`UPDATE refresh_tokens SET revoked_at = NOW();`)
   - Si MinIO/S3 compromis → rotate access keys
2. **Évaluer impact** :
   - Quelles données ? (cf. catégories `data_categories` AI Act)
   - Combien d'users affectés ?
   - Catégorie particulière (Article 9) impactée ?
3. **Notification CNIL dans 72h** (RGPD Article 33) :
   - Description nature du breach
   - Catégories + nombre approx de personnes concernées
   - Conséquences probables
   - Mesures prises pour atténuer
   - Contact DPO
4. **Notification users concernés** (Article 34) si risque élevé :
   - Email via Brevo (template à créer V2)
   - In-app notification via FCM
5. **Investigation root cause** + post-mortem public sous 30 jours.
6. **Update DPIA** Phase M3.

---

## Outils & accès

| Outil | URL |
|---|---|
| Grafana | https://grafana.nexya.ai (post L2) |
| Prometheus | http://prometheus.internal (post L2) |
| Sentry | https://sentry.io/orgs/nexyalabs/ (post L2) |
| Cloudflare | https://dash.cloudflare.com |
| Hetzner | https://console.hetzner.cloud |
| OpenAI status | https://status.openai.com |
| Crisp panel | https://app.crisp.chat |

Identifiants via secrets manager (Doppler / 1Password / AWS SSM —
choix Phase L2).

---

## Post-mortem template

Après chaque incident Critical, rédiger dans `docs/runbooks/
post-mortems/<date>-<slug>.md` :

```
# Post-mortem <date> — <description courte>

## TL;DR
1-2 lignes.

## Timeline (UTC)
- HH:MM Ivan reçoit alerte X
- HH:MM Constat Y
- HH:MM Mitigation appliquée
- HH:MM Service rétabli

## Root cause
Section technique détaillée.

## Impact
- Users affectés : X
- Durée : Y minutes
- Données impactées : Z

## What went well
- ...

## What went poorly
- ...

## Action items
- [ ] Item 1 (owner, deadline)
- [ ] Item 2

## Lessons learned
```
