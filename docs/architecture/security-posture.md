# Security Posture — NEXYA Backend

> **Executive summary (EN).** STRIDE threat model applied per category
> with NEXYA-specific mitigations. Defense-in-depth: 4 layers on auth
> (rate limit IP/device + captcha + JWT RS256 + audit), 3 layers on
> input (whitelist MIME + magic-bytes + virus scan), 2 layers on
> output (secrets scrubber + presigned URLs). RGPD UE 2016/679 + AI
> Act EU 2024/1689 compliant by design. Production safety guard
> fail-fast at boot if config insecure (CORS, secrets, debug mode).
> All 8 SaaS integrations follow mock-first pattern (zero key required
> for dev/CI). Headers HTTP CSP/HSTS/COOP/CORP via O1 middleware.

---

## STRIDE par catégorie

### Spoofing (usurpation d'identité)

**Risque** : un attaquant se fait passer pour un user légitime.

**Mitigations NEXYA** :
1. **JWT RS256 asymétrique** — clé privée signe côté backend, clé
   publique vérifie côté serveur uniquement. Pas de HMAC partagé qui
   peut fuiter (cf. [ADR 0004](../adr/0004-jwt-rs256-vs-hs256.md)).
2. **Captcha hCaptcha** sur `/auth/register` — coupe les bots avant
   l'INSERT user. Mock-first auto si `HCAPTCHA_SECRET_KEY` vide.
3. **Device quotas** — UPSERT atomique `(device_id, date_utc)`,
   5/jour/device anti-distribuée IPs tournantes même device.
4. **Rate limits IP empilés** — 5/min `/auth/register` + 5/jour IP
   anti slow & low + 10/min `/auth/login` + 10/h forgot-password.

### Tampering (altération de données)

**Risque** : modification non autorisée du contenu en transit ou en DB.

**Mitigations** :
1. **HTTPS partout** (Cloudflare + Let's Encrypt cert).
2. **HSTS preload** en prod (cf. [O1 headers](../../app/core/security/headers.py)).
3. **SQLAlchemy parameterized queries** — jamais de f-string SQL,
   protection injection ORM-level.
4. **`content_sha256` SHA-256** sur tous les uploads (`library_items`,
   `uploaded_files`, `voice_transcriptions`, `vision_analyses`,
   `memories`) — dédup idempotente + intégrité.
5. **Magic-bytes anti-smuggling** — un user qui poste un `.exe` avec
   `Content-Type: image/png` est rejeté 415 `FILE_CONTENT_MISMATCH`
   AVANT upload MinIO (cf. `app/core/storage/mime_detector.py`).
6. **Virus scanner** (ClamAV prod, Mock dev) sur tous les uploads
   `/files/upload` — détection EICAR + signatures malware connues.

### Repudiation (déni d'action)

**Risque** : un user nie avoir effectué une action sensible (auth,
suppression compte, paiement).

**Mitigations** :
1. **`auth_events` table** — 11 types d'événements (register_success/
   failed, login_success/failed, logout, password_change, password_
   reset_request/success, account_delete, captcha_failed, device_
   quota_exceeded). FK `ON DELETE SET NULL` RGPD-safe pour préserver
   trace forensic post-purge user.
2. **`consent_log` immutable** (RGPD Article 7) — `document_hash`
   SHA-256 figé au moment du consentement, preuve juridique
   anti-modification rétroactive (cf. [`docs/compliance/rgpd.md`](../compliance/rgpd.md)).
3. **`ai_calls` enrichi AI Act** — chaque appel LLM tracé avec
   `legal_basis` (contract/legitimate_interest/consent/legal_obligation),
   `data_categories` (user_input/prompt_history/file_content/...),
   `retention_until` 90j (cf. [`docs/compliance/ai-act.md`](../compliance/ai-act.md)).
4. **structlog `trace_id` corrélation** — chaque action HTTP a un
   `X-Request-ID` propagé dans tous les logs + spans OTel + Sentry
   breadcrumbs. Forensic complet possible.

### Information disclosure (fuite de données)

**Risque** : exposition de secrets, données users tiers, ou détails
internes.

**Mitigations** :
1. **`password_hash` bcrypt** — jamais en clair, jamais dans les logs,
   tronqué 72 bytes avant hash.
2. **Secrets scrubber A3** (`core/errors/handlers.py::_scrub`) —
   masque récursif des champs sensibles (`password`, `token`,
   `secret`, `api_key`, `webhook_secret`, `device_token`, etc.) dans
   les logs Pydantic errors + Sentry breadcrumbs (alias public
   `scrub_secrets` exporté pour le hook Sentry K1).
3. **Presigned URLs MinIO TTL 1h** — `storage_key` brut JAMAIS exposé,
   uniquement URLs signées avec expiration courte (`library_items`,
   `uploaded_files`, RGPD export).
4. **404 IDOR-safe** sur toutes les ressources user-scope — pas de 403
   qui confirmerait l'existence de la ressource d'un autre user
   (anti-énumération d'UUID valides).
5. **404 idempotent** sur DELETE — ne distingue pas « ressource
   inexistante » vs « pas à toi ».
6. **RGPD anonymisation logique** — `DELETE /user/account` efface
   `email`/`username`/`display_name`/`avatar_url`/`bio` + `is_active=
   false` + `deleted_at=NOW()`. Hard delete physique différé via
   `/rgpd/user/account/delete-request` workflow 2-step + 30j grâce.
7. **`auth_events.ip` anonymisée /24** dans l'export RGPD ZIP
   (Article 32) — l'équipe support ne voit jamais l'IP brute.
8. **Audit `auth_events` avec hash email** — `_hash_email_log` SHA-256
   [:12] dans les logs forensic, on corrèle N tentatives sur même
   email sans stocker PII en clair Redis/DB audit.

### Denial of Service

**Risque** : épuisement de ressources backend (CPU, mémoire, DB pool,
quota LLM, budget USD).

**Mitigations** :
1. **Rate limits multi-couches** — IP + user + device + endpoint
   spécifique. Ex `/files/upload` 20/h/user, `/chat/reports` 10/h/user,
   `/rgpd/user/data-export` 1/24h/user, `/vision/analyze` 30/h/user.
2. **BudgetTracker Redis** — quotas tokens journaliers user (chat,
   image, voice minutes, vision images, embeddings) + cap modèle
   global. INCR + DECR rollback atomique si dépassé.
3. **TokenEstimator pré-flight** — cap 30 000 tokens/requête avant
   appel provider. 402 `LLM_QUOTA_EXCEEDED` avec data
   `{estimated_tokens, max_allowed}` côté client.
4. **CircuitBreaker** par `(provider, model)` — open après 5 échecs/
   30s, court-circuite la chaîne fallback sans appeler le provider en
   panne.
5. **Heartbeat SSE 15s** — évite les coupures TCP 2G/3G qui forcent
   reconnect (anti-amplification mobile).
6. **Cap multipart upload 100MB** + lecture streaming chunks 8KB +
   interruption précoce si cap dépassé (pas lu les 5GB d'un attaquant).
7. **Pool DB** SQLAlchemy avec `pool_size` + `max_overflow` configurés
   + `pool_pre_ping` (vérification connexion vivante) +
   `pool_recycle=3600` (anti-timeout réseau).
8. **`SELECT FOR UPDATE SKIP LOCKED`** sur queue Planner —
   plusieurs workers arq concurrent-safe sans race condition.
9. **`asyncio.create_task`** fire-and-forget pour tracking IA et
   escalation Crisp — le SSE/handler ne bloque JAMAIS sur écriture
   secondaire.

### Elevation of privilege

**Risque** : un user normal accède à des fonctions admin.

**Mitigations** :
1. **`require_admin` guard** (J1) — ACL email-list via
   `settings.rgpd_admin_emails` case-insensitive. `_enforce_production
   _safety` fail-fast au boot en prod si la liste est vide.
2. **Endpoints admin séparés** — préfixe `/admin/*` (helpdesk metrics,
   AI Act registry). Tag OpenAPI `admin` distinct (cf. [O1 customizer](../../app/core/openapi/customizer.py)).
3. **JWT decoding strict** — `decode_access_token` valide signature
   RS256 + expiration + jti (anti-replay) + purpose claim (anti
   confusion access/refresh/reset/unsubscribe tokens).
4. **Refresh token rotation** — chaque usage invalide l'ancien hash
   et émet un nouveau. Si un attaquant capture un refresh, le user
   légitime sera kicked à sa prochaine refresh.
5. **Blacklist Redis access tokens** — `logout` blacklist le `jti`
   jusqu'à expiration TTL. `decode_access_token` vérifie le jti dans
   Redis avant valid.
6. **Pas de `is_admin` champ user** — la vérification se fait par
   email-list dans guard, pas par flag DB modifiable. Anti-élévation
   par UPDATE direct DB.

---

## Defense in depth — `/auth/register`

Exemple concret : 4 couches empilées :

```
Client → Cloudflare WAF (DDoS basique)
       → Rate limit IP 5/min (rafales courtes)
       → Rate limit IP 5/jour (slow & low)
       → Device quota 5/jour/device (IPs tournantes même device)
       → Captcha hCaptcha (humain vs bot)
       → Pydantic validation (email format, password policy)
       → Sanitizer NFC/null bytes/zero-width/bidi (anti-injection)
       → DB INSERT user
       → Audit auth_events
```

Chaque couche a un coût croissant pour l'attaquant :
- IP 5/min : 1 IP = 5 attempts/min, 720 IPs nécessaires pour 1 register/s
- IP 5/jour : tient 24h
- Device quota : multi-IPs avec même device fingerprint = stop
- Captcha : économiquement bloquant (résolution CAPTCHA = $1-2 / 1000)

---

## Production safety guard (fail-fast au boot)

`Settings._enforce_production_safety` raise `ValueError` si
`is_production` ET :

| Garde-fou | Raison |
|---|---|
| `ALLOWED_ORIGINS=*` | Trou béant CSRF + token theft |
| `APP_SECRET` faible | Secret cassé |
| `JWT_PRIVATE_KEY/PUBLIC_KEY` vides | Impossible de signer |
| `DEBUG=true` | Stack traces exposées |
| `DB_ECHO=true` | Imprime les queries (et parfois params) sur stdout |
| `PROMETHEUS_SCRAPE_TOKEN` vide | `/metrics` ouvert = fuite KPI métier + DDoS |
| `GRAFANA_ADMIN_PASSWORD` vide ou `admin` | Takeover dashboard observabilité |
| `RGPD_ADMIN_EMAILS` vide | Endpoint `/rgpd/admin/*` sans ACL = fuite registre AI Act |
| `SECURITY_HEADERS_PRESET` ∉ {prod, off} | Headers laxistes en prod |

Un déploiement en prod avec un seul de ces problèmes échoue au boot
avec un message explicite. Aucune chance de fuite silencieuse.

---

## Headers HTTP (O1)

Middleware `NexyaSecurityHeadersMiddleware` 4 presets :

| Preset | CSP | HSTS | COOP | CORP | Cas d'usage |
|---|---|---|---|---|---|
| `dev` | — | — | — | — | Dev local (Swagger UI fonctionne) |
| `staging` | `default-src 'self' 'unsafe-inline'` | `max-age=31536000` | — | — | Staging post-L2 |
| `prod` | strict (sans unsafe-inline) | `+ includeSubDomains; preload` | `same-origin` | `same-origin` | Production |
| `off` | — | — | — | — | Kill-switch incident |

+ `X-Content-Type-Options: nosniff` partout, `X-Frame-Options: DENY`
en staging/prod, `Referrer-Policy: strict-origin-when-cross-origin`,
`Permissions-Policy: camera=(), microphone=(), geolocation=(),
payment=(), usb=(), magnetometer=()`.

Skip CSP sur `/docs`, `/redoc`, `/openapi.json` en non-prod (Swagger UI
inline JS nécessaire). En prod, `docs_url=None` désactive complètement.

---

## Conformité

Voir [`docs/compliance/`](../compliance/) :
- [`rgpd.md`](../compliance/rgpd.md) — Articles 7/15/17/20 mapping
- [`ai-act.md`](../compliance/ai-act.md) — EU 2024/1689 Article 13
- [`security-checklist.md`](../compliance/security-checklist.md) — OWASP Top 10
- [`dpa-template.md`](../compliance/dpa-template.md) — Article 28 DPA

---

## TODO post-launch (Phase 19)

- **Pentest externe** (Phase M1) — bug bounty privé HackerOne ou agence FR
- **DPIA RGPD** (Phase M3) — Data Protection Impact Assessment pour
  l'AI Act high-risk classification
- **Multi-region failover** (Phase 19) — réplication Postgres + Redis
  Sentinel post 5-10k users payants
- **Hardening additionnel** : CSP `nonce` dynamique (V2 si dashboard
  SPA frontend custom), HSTS preload soumission post-stabilisation L2
