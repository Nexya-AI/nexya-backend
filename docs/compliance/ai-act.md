# AI Act EU 2024/1689 — Mapping NEXYA

> **Executive summary (EN).** EU AI Act 2024/1689 Article 13
> (transparency obligations) compliant by design. `ai_calls` table
> enriched with `legal_basis` (4 values), `data_categories` (6 values),
> `retention_until` (90j default). Admin endpoint
> `GET /rgpd/admin/ai-act-registry?format=csv|json` exports the full
> registry of AI processing for audit. Applicable date: **August 2026**.
> NEXYA classified preliminary as "limited risk" (general-purpose AI
> chatbot) — DPIA Phase M3 to confirm.

> Source : [Règlement (UE) 2024/1689](https://eur-lex.europa.eu/eli/reg/2024/1689/oj)

---

## Calendrier d'application

| Date | Mesure |
|---|---|
| 2024-08-01 | Entrée en vigueur du règlement |
| 2024-11-02 | Interdictions (manipulation, scoring social, etc.) — V1 NEXYA pas concerné |
| **2025-08-02** | **Obligations general-purpose AI models** (déjà compliant via providers — Google/OpenAI/Anthropic respectent) |
| **2026-08-02** | **Article 13 transparency obligations** — concerne NEXYA directement |
| 2027-08-02 | Article 26 high-risk providers — V1 NEXYA pas classé high-risk |

---

## Classification système IA

NEXYA = **chatbot IA généraliste** (assistant conversationnel
multi-experts). Classification probable :

- **Pas de "unacceptable risk"** (pas de scoring social, pas de
  reconnaissance biométrique, pas de manipulation comportementale).
- **Pas de "high-risk"** au sens Annex III (pas d'éducation/HR/
  scoring crédit en mode décisionnel — info uniquement).
- **Probable "limited risk"** : transparency obligations Article 13
  (l'user doit savoir qu'il interagit avec une IA).

**À confirmer DPIA Phase M3** avec consultant juridique. Si NEXYA
ajoute des fonctionnalités de scoring (ex: « capacité à obtenir un
prêt »), reclassification en high-risk possible.

---

## Article 13 — Transparency obligations

### Obligation

> « Les déployeurs informent les personnes qui interagissent avec un
> système d'IA qu'elles interagissent avec un tel système, à moins que
> cela ne ressorte clairement du contexte. »

### Mise en œuvre NEXYA

1. **Identité claire** : NEXYA se présente comme « assistant IA
   développé par Nexyalabs » (system_prompt `_NEXYA_IDENTITY` partagé
   par tous les experts dans `app/ai/experts.py`).
2. **Évals identité (N3)** : 18 prompts catégorie `identity` valident
   que NEXYA répond correctement à « qui es-tu ? », « es-tu Gemini? »,
   etc. — pas de leak « je suis Gemini » sous le capot.
3. **Disclaimers métier** : `medicine` + `legal` ont un `disclaimer`
   préfixé au premier chunk SSE (« Les informations fournies ne
   remplacent pas l'avis d'un professionnel de santé. »).

---

## Article 26 — High-risk providers (NON applicable V1)

NEXYA n'est pas concerné en tant que provider high-risk V1. Les
obligations (qualité données, traçabilité, surveillance humaine,
robustesse, transparence, cybersécurité) seront **revisitées Phase
M3** si reclassification.

**Pré-emption** : NEXYA implémente déjà la majorité des principes
high-risk par design :

| Obligation | Mise en œuvre |
|---|---|
| Qualité des données | RAG corpus G1 cleanup post-blind-test (2026-04-24), évals N3 |
| Traçabilité | `ai_calls` registre complet (Article 13) |
| Surveillance humaine | Disclaimers + endpoints feedback + escalation Crisp |
| Robustesse | Retry + CircuitBreaker + fallback chain + load tests N4 |
| Transparence | Évals identity + system_prompts publics dans BACKEND_IA |
| Cybersécurité | JWT RS256 + headers O1 + production safety guard |

---

## Registre `ai_calls` — Article 13 ready

### Schéma enrichi (migration 017_rgpd)

```sql
ALTER TABLE ai_calls ADD COLUMN legal_basis VARCHAR(32);
ALTER TABLE ai_calls ADD COLUMN data_categories VARCHAR(64)[];
ALTER TABLE ai_calls ADD COLUMN retention_until TIMESTAMPTZ;

ALTER TABLE ai_calls ADD CONSTRAINT ck_ai_calls_legal_basis
    CHECK (legal_basis IN ('contract','legitimate_interest','consent','legal_obligation'));

CREATE INDEX ix_ai_calls_legal_basis_time
    ON ai_calls (legal_basis, created_at DESC);
```

### Valeurs `legal_basis`

| Valeur | RGPD Art. 6 | Cas d'usage |
|---|---|---|
| `contract` | 6.1.b | Default — exécution service NEXYA commandé |
| `legitimate_interest` | 6.1.f | Anti-fraud, security monitoring |
| `consent` | 6.1.a | Usage secondaire (training data improvement) — opt-in |
| `legal_obligation` | 6.1.c | Logs sécurité légaux |

### Valeurs `data_categories`

| Valeur | Description |
|---|---|
| `user_input` | Message courant de l'user |
| `prompt_history` | Messages précédents (multi-turn context) |
| `file_content` | Documents uploaded (RAG) |
| `voice_audio` | Whisper STT |
| `image_content` | Vision multimodal |
| `profile_data` | Bio, voice_id, locale (memory injection D3) |

### Backfill

Tous les rows historiques pré-J1 → `legal_basis='contract'` +
`data_categories={'user_input'}` + `retention_until=created_at + 90j`.

---

## Endpoint admin

### `GET /rgpd/admin/ai-act-registry?format=csv|json`

ACL `require_admin` (J1) — fail-fast prod si `RGPD_ADMIN_EMAILS` vide.

**Format CSV** : BOM UTF-8 Excel-friendly + 14 colonnes ordonnées
figées :

```csv
\xef\xbb\xbfid,user_id,session_id,trace_id,expert_id,provider,model,prompt_tokens,completion_tokens,total_tokens,cost_usd,outcome,legal_basis,data_categories,retention_until,created_at
abc-123,user-456,sess-789,trace-...,computer,gemini,gemini-2.5-flash,150,80,230,0.000123,completed,contract,"user_input,prompt_history",2026-07-27T...,2026-04-27T...
...
```

**Format JSON** :

```json
{
  "exported_at": "2026-04-27T...",
  "row_count": 12345,
  "items": [
    {
      "id": "abc-123",
      "user_id": "user-456",
      ...
      "legal_basis": "contract",
      "data_categories": ["user_input", "prompt_history"],
      "retention_until": "2026-07-27T..."
    },
    ...
  ]
}
```

### Filtres

```
?date_from=2026-01-01&date_to=2026-04-27&format=csv&limit=100000
```

`limit` clampé 1-100 000 (anti-DoS).

---

## Conservation `retention_until`

**90 jours par défaut** (`created_at + 90 days`). Au-delà, les rows
`ai_calls` peuvent être archivées vers S3 ou supprimées (V2 cron de
purge).

V1 : pas de purge automatique — la table grossit linéairement. À
~5 milliards rows/an régime 950k users, partitionnement mensuel
nécessaire (Phase 19 multi-region).

---

## Rights of users (Article 13)

NEXYA expose les droits user via l'endpoint suivant :

| Droit | Endpoint |
|---|---|
| Information | `/rgpd/user/consent` (lecture) |
| Accès | `/rgpd/user/data-export` (Article 15) |
| Rectification | `PUT /user/profile` |
| Effacement | `/rgpd/user/account/delete-request` (Article 17) |
| Portabilité | `/rgpd/user/data-export` JSON ZIP (Article 20) |
| Limitation | Pas d'endpoint dédié V1 — l'user supprime via Art. 17 ou contact support@nexya.ai |
| Opposition | `/rgpd/user/consent DELETE {type}` (retrait consentement) |

Voir [`rgpd.md`](rgpd.md).

---

## DPIA Phase M3 — TODO

- Engager consultant DPO externe (avocat RGPD/AI Act EU FR)
- Documents à produire :
  - Description du système IA (architecture, providers, modèles)
  - Finalité du traitement (assistant conversationnel multi-experts)
  - Nécessité et proportionnalité (alternative manual reviewing
    impossible à cette échelle)
  - Risques pour les droits des personnes (biais LLM, hallucinations,
    fuite data via prompts)
  - Mesures pour atténuer (modération + évals + disclaimers + RAG +
    fine-tuning Gemma post-Phase H pour langues vernaculaires)
- Itération avec CNIL si nécessaire

---

## Sous-traitants AI providers

NEXYA fait appel à **4 sous-traitants AI** au 2026-04-27 :

| Provider | Lieu de traitement | Modèles |
|---|---|---|
| Google (Gemini) | EU (Vertex AI region) | gemini-2.5-flash, gemini-2.5-pro, imagen-3.0 |
| OpenAI | US | gpt-4o, omni-moderation, text-embedding-3-small, whisper-1, tts-1 |
| Anthropic | US | claude-sonnet-4-6, claude-haiku-4-5, claude-opus-4-6 (fallback) |
| Qwen (Alibaba) | International (DashScope Intl) | qwen2.5-72b-instruct (fallback) |

**Conformité Article 13** : NEXYA documente quel provider/model a été
utilisé pour chaque appel (`ai_calls.provider` + `ai_calls.model`)
→ traçabilité complète possible.

**Risque transferts hors EU** : OpenAI/Anthropic/Qwen sont US/Chine.
Stratégie d'atténuation V2 :
- Privilégier Gemini Vertex EU pour les users EU
- Anthropic via Claude EU (post-livraison Anthropic EU AWS region)
- DPA standards signés avec chaque provider

---

## Recommandations Ivan AVANT août 2026

1. **DPIA consultant externe** (Phase M3) — budget ~5-10k EUR
2. **AI Processing notice FR** rédigée + hash figé dans
   `consent_log` (cf. `rgpd.md`)
3. **Documenter `legitimate_interest`** anti-fraud (memo écrit pour
   audit CNIL) si on commence à utiliser cette base légale
4. **Audit registre AI Act** — exporter `/rgpd/admin/ai-act-registry`
   trimestriellement et archiver
5. **Reclassification éventuelle high-risk** si NEXYA ajoute des
   features de scoring/decision-support
