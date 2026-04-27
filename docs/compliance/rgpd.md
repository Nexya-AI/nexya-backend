# RGPD UE 2016/679 — Mapping NEXYA

> **Executive summary (EN).** Article-by-article mapping of NEXYA
> backend implementation against EU GDPR 2016/679. Articles 7
> (consent), 15 (access), 17 (erasure), 20 (portability), 32
> (security), 33 (breach 72h notification placeholder), 35 (DPIA
> placeholder Phase M3) covered. All sensitive endpoints rate-limited,
> audited via `auth_events` table. Dual-step deletion workflow with 30
> days grace period. ZIP export 23 files with anonymized IPs (/24
> IPv4, /48 IPv6).

> Source : [Règlement (UE) 2016/679](https://eur-lex.europa.eu/eli/reg/2016/679/oj)

---

## Article 5 — Principes du traitement

| Principe | Mise en œuvre NEXYA |
|---|---|
| **a) licéité, loyauté, transparence** | `consent_log` Article 7 + ToS document_hash SHA-256 figé + AI Act registre `ai_calls.legal_basis` |
| **b) limitation des finalités** | `ai_calls.data_categories` (user_input/prompt_history/file_content/voice_audio/image_content/profile_data) — usage strict défini |
| **c) minimisation** | Aucune donnée collectée hors strict nécessaire. Pas de tracking analytics tiers V1. |
| **d) exactitude** | `PUT /user/profile` permet correction. Audit `auth_events` trace les modifs. |
| **e) limitation de la conservation** | `ai_calls.retention_until` (90j défaut). Cron `purge_deleted_accounts` 03:47 UTC. |
| **f) intégrité et confidentialité** | JWT RS256 + bcrypt + HSTS + scrubber A3 + presigned URLs (cf. `security-posture.md`) |
| **g) responsabilité** | DPIA Phase M3 + DPO email `RGPD_ADMIN_EMAILS` + audit `auth_events` |

---

## Article 6 — Bases légales

`ai_calls.legal_basis` énumère 4 valeurs CHECK SQL :

| Valeur | Cas d'usage |
|---|---|
| `contract` (6.1.b) | Exécution du service NEXYA commandé par l'user. **Défaut** pour tous les appels IA déclenchés par l'user. |
| `consent` (6.1.a) | Usages secondaires (analytics, training data improvement) — opt-in via `/rgpd/user/consent`. |
| `legitimate_interest` (6.1.f) | Anti-fraud, security monitoring. Documentation impact assessment requise (Phase M3). |
| `legal_obligation` (6.1.c) | Conservation logs sécurité (auth_events) — règlements nationaux. |

Backfill auto sur les rows historiques pré-J1 (`contract` par défaut).

---

## Article 7 — Conditions du consentement

### `consent_log` table

```sql
CREATE TABLE consent_log (
    id UUID PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    consent_type VARCHAR(32) NOT NULL,    -- 7 catégories
    status VARCHAR(16) NOT NULL,          -- 'granted' | 'revoked'
    document_version VARCHAR(32) NOT NULL, -- ex 'tos-2026-04-26'
    document_hash CHAR(64) NOT NULL,       -- SHA-256 figé du ToS au moment du consentement
    granted_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ,
    source VARCHAR(32) NOT NULL,           -- 'web_signup' | 'mobile_signup' | 'settings' | ...
    ip_address INET,
    user_agent VARCHAR(256),
    metadata_json JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
```

### Preuve juridique anti-modification

Le `document_hash` est calculé une fois au moment du consentement et
**ne change jamais**. Si NEXYA modifie ses ToS, un **nouveau
`document_version`** est créé, l'user doit re-consenter.

→ Un audit légal peut prouver « tel jour, l'user X a consenti à la
ToS dont le SHA-256 vaut Y, hash inchangé depuis ».

### 7 catégories de consentement

```
- terms_of_service              # ToS générale
- privacy_policy                # Politique privacy
- ai_processing                 # Article 13 AI Act
- analytics_optional            # Tracking opt-in
- marketing_emails              # Communications marketing
- data_export_processing        # Sous-traitants
- training_data_optional        # Future fine-tuning consent
```

### Endpoints publics

- `GET /rgpd/user/consent` — liste des consentements actifs user
- `POST /rgpd/user/consent` — enregistre un consentement
  (idempotent : si même type + same version existe granted+actif, no-op ;
  si nouvelle version, revoke ancien + INSERT nouveau granted)
- `DELETE /rgpd/user/consent/{type}` — retrait consentement (204
  idempotent), audit `consent_revoked`

---

## Article 15 — Droit d'accès

### Endpoint `GET /rgpd/user/data-export`

Rate limit **1/24h/user** (anti-DoS). Retourne un ZIP en mémoire
(`zipfile.ZipFile(BytesIO(), ZIP_DEFLATED, level=6)`) avec **23
fichiers** :

| Fichier | Contenu |
|---|---|
| `manifest.json` | record_counts par table + schema_version + exported_at + truncated flag |
| `README.txt` | FR Article 12 RGPD (information dans format clair) |
| `users.json` | Profil (sans `password_hash` — redact) |
| `consents.json` | Historique consent_log complet |
| `auth_events.json` | Avec IP anonymisée /24 IPv4 ou /48 IPv6 (`ipaddress.ip_network(strict=False)`) |
| `device_tokens.json` | Token masqué `***` + 8 derniers chars |
| `chat/conversations.json` + `messages.json` + `abuse_reports.json` | |
| `projects/projects.json` + `files.json` | |
| `library/items.json` + `blob_urls.json` | Presigned URLs MinIO TTL 7j |
| `memory/memories.json` | Sans `embedding` 1536 dim — trop volumineux |
| `notifications/*.json` + `preferences.json` | |
| `planner/tasks.json` + `results.json` | |
| `files/uploaded.json` + `chunks.json` + `blob_urls.json` | Chunks sans embedding |
| `voice/transcriptions.json` | |
| `vision/analyses.json` | |
| `ai_calls/ai_calls.json` | Sans `extra` (peut contenir prompt content) |

**Cap soft** `rgpd_export_max_size_bytes=100 MB`. Au-delà, flag
`truncated=true` dans manifest + l'user refait un export ciblé V2.

### Anti-leak garanti

- **0 password_hash** (redact set explicite)
- **0 cross-user leak** (`WHERE user_id == user.id` strict)
- **0 storage_key brut** (uniquement presigned URLs avec TTL)
- **0 IP brute** (anonymisée /24)
- **0 prompt content dans ai_calls** (champ `extra` exclu)

Audit `data_exported` event tracé dans `auth_events`.

---

## Article 17 — Droit à l'effacement

### Workflow 2-step (anti-clic accidentel + anti-compte compromis)

```
Step 1 : POST /rgpd/user/account/delete-request
  → Anonymisation logique immédiate (email/username/display_name/
    avatar/bio effacés, is_active=false, deleted_at=NOW())
  → INSERT deletion_requests (status='pending',
    scheduled_purge_at=NOW() + 30 days)
  → Audit account_delete_requested
  → Capture l'email original AVANT anonymisation dans
    purge_summary_json.email_for_confirmation pour mail post-purge

Step 2 (30 jours plus tard) : Cron purge_deleted_accounts (03:47 UTC)
  → SELECT FROM deletion_requests WHERE status='pending'
    AND scheduled_purge_at <= NOW() FOR UPDATE SKIP LOCKED LIMIT 50
  → Pour chaque : mark_processing → collect_storage_keys MinIO
    → DELETE FROM users (cascade SQL 22 tables)
    → Suppression blobs MinIO fail-safe par-key
    → Email de confirmation à l'email_for_confirmation captured
```

### Possibilité de rétractation

```
POST /rgpd/user/account/delete-request/cancel
  → Trouve la deletion_requests pending de l'user
  → status='pending' → 'cancelled', restore is_active=true,
    deleted_at=NULL
  → Audit account_delete_cancelled
```

L'email/username restent anonymisés (la rétractation ne reconstitue
pas l'identité d'origine, l'user contacte support).

### Anonymisation logique vs hard delete

| Méthode | Quand |
|---|---|
| Anonymisation logique | Immédiat sur `DELETE /user/account` (workflow A1) |
| Hard delete | 30 jours après `delete-request` (workflow J1) |

Cron `purge_deleted_accounts` runs idempotent — si Crisp ou Brevo down
au moment du purge, retry au prochain tick.

---

## Article 20 — Portabilité

### Format requis

> « format structuré, couramment utilisé et lisible par machine »

NEXYA exporte **JSON** (ZIP) — format universel. Voir Article 15
ci-dessus.

### Future export CSV ?

V2 si demande user (formulaire admin → `/rgpd/admin/export-csv` —
non prévu V1).

---

## Article 28 — Sous-traitants (DPA)

NEXYA fait appel à **8 sous-traitants** au 2026-04-27 :

| Sous-traitant | Catégorie de données | Lieu | Article 28 ? |
|---|---|---|---|
| Google (Gemini API) | Prompt user + history | EU/US (Vertex AI region) | DPA standard Google Cloud |
| OpenAI | Prompt user (modération + embeddings + Whisper STT) | US | DPA standard OpenAI |
| Anthropic | Prompt user (fallback) | US | DPA standard Anthropic |
| Brevo (ex-Sendinblue) | Email + name | EU (France) | DPA EU |
| hCaptcha | IP + user-agent | US | DPA Intuition Machines |
| Crisp | Email + name + ticket content | EU (France) | DPA EU |
| Hetzner | Hosting (toutes les données) | EU (Allemagne) | DPA EU + ISO 27001 |
| Cloudflare | IP + DNS | US/Global | DPA US (Privacy Shield successor) |

Voir [`dpa-template.md`](dpa-template.md) pour le template Article 28.

---

## Article 32 — Sécurité du traitement

Voir [`docs/architecture/security-posture.md`](../architecture/security-posture.md)
pour le détail :
- JWT RS256 (cryptographie asymétrique)
- bcrypt password hashing
- HSTS preload prod
- TLS 1.3 obligatoire (Cloudflare)
- Rate limits IP/user/device
- Captcha hCaptcha
- Virus scanner uploads
- Magic-bytes anti-smuggling
- Production safety guard fail-fast au boot

---

## Article 33 — Notification violation

**Placeholder V1** : si un data breach est détecté, NEXYA s'engage à
notifier la CNIL **dans les 72 heures** + les users concernés.
Procédure documentée dans [`docs/runbooks/incident-response.md`](../runbooks/incident-response.md)
section « RGPD breach 72h notification ».

V2 : automate de notification (envoi email batch via Brevo + tableau
de bord admin breach status).

---

## Article 35 — DPIA (Data Protection Impact Assessment)

**Placeholder V1**. NEXYA est un système **AI Act high-risk**
potentiellement (assistant IA généraliste, possibilité d'aide à la
décision médicale/juridique). DPIA obligatoire AVANT déploiement
masse :

- Phase M3 (post-pentest M1) — engagement consultant DPO externe
- Documents requis : description du traitement + nécessité +
  proportionnalité + risques + mesures pour atténuer
- Itération avec CNIL si demandé

V1 : NEXYA pré-launch beta limité (< 5k users) — le risque est
contenu, DPIA exigée seulement si catégorie « élevé probabilité
risque ».

---

## Article 37 — DPO

**Placeholder V1** :
- DPO interne = Ivan (responsabilité juridique).
- Email de contact : `dpo@nexya.ai` (à créer avant prod).
- `RGPD_ADMIN_EMAILS` env var liste les emails autorisés à accéder
  `/rgpd/admin/*` (production safety guard fail-fast au boot si vide).

V2 (post 50k users) : DPO externe (avocat ou cabinet DPO).

---

## Endpoints RGPD livrés

| Endpoint | Article | Auth |
|---|---|---|
| `GET /rgpd/user/data-export` | 15 + 20 | JWT (rate 1/24h) |
| `GET /rgpd/user/consent` | 7 | JWT |
| `POST /rgpd/user/consent` | 7 | JWT |
| `DELETE /rgpd/user/consent/{type}` | 7 | JWT (idempotent) |
| `POST /rgpd/user/account/delete-request` | 17 | JWT (409 si déjà pending) |
| `POST /rgpd/user/account/delete-request/cancel` | 17 | JWT (404 si aucune active) |
| `GET /rgpd/admin/ai-act-registry?format=csv|json` | 13 (AI Act) | `require_admin` |

Voir [`docs/api/openapi.json`](../api/openapi.json) pour les schémas
détaillés.

---

## Recommandations Ivan AVANT prod L2

1. **Rédiger ToS + Privacy Policy + AI Processing notice FR**
   (responsabilité juridique humaine, pas LLM). Calculer hash SHA-256
   une fois et figer en constante backend.
2. **Décider banner cookies V1** (frontend) ou V2.
3. **Planifier audit CNIL** post-go-live ~3-6 mois.
4. **Préparer dossier registre AI Act** pour Article 13 applicable
   août 2026 (le `ai_calls` enrichi J1 le couvre côté code, reste
   documentation Markdown).
5. **Configurer `RGPD_ADMIN_EMAILS`** via secret manager — au minimum
   email DPO Ivan en V1, prévoir DPO externe pour 50k users V2.
