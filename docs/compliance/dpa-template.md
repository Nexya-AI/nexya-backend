# Data Processing Agreement (DPA) — Template

> **Executive summary (EN).** Article 28 GDPR template for NEXYA
> sub-processors. NEXYA acts as **data controller** (responsible) for
> end-user personal data. Sub-processors listed: OpenAI, Anthropic,
> Google (Gemini), Brevo, hCaptcha, FCM (Firebase), Crisp, Hetzner,
> Cloudflare, MinIO/S3 (if managed). This template is a **placeholder**
> — the legally binding DPA must be drafted and reviewed by a lawyer
> (Phase M3 with external DPO consultant).

> **AVERTISSEMENT** : ce document est un squelette pour démarrer la
> rédaction du DPA réel. **Il ne constitue PAS un document
> juridiquement valide en l'état**. Il doit être validé/complété par
> un avocat RGPD/CNIL avant signature.

---

## Article 28 RGPD — Obligations du sous-traitant

### Cadre légal

> « Lorsqu'un traitement doit être effectué pour le compte d'un
> responsable du traitement, celui-ci fait uniquement appel à des
> sous-traitants qui présentent des garanties suffisantes quant à la
> mise en œuvre de mesures techniques et organisationnelles
> appropriées. »
>
> — Article 28(1) RGPD UE 2016/679

### Engagement requis (Article 28.3)

Le sous-traitant doit s'engager à :
- (a) ne traiter les données **que sur instruction documentée** du
  responsable
- (b) garantir la **confidentialité** du personnel
- (c) prendre **toutes les mesures de sécurité** Article 32
- (d) ne recourir à un **autre sous-traitant** qu'avec autorisation
  écrite préalable
- (e) aider le responsable à **répondre aux droits des personnes**
- (f) aider le responsable à respecter Articles 32-36 (sécurité,
  notification breach, DPIA)
- (g) **supprimer ou retourner** les données à la fin de la prestation
- (h) mettre à disposition **toute information** nécessaire pour
  prouver le respect Article 28

---

## Sous-traitants NEXYA actuels (au 2026-04-27)

| # | Sous-traitant | Catégorie service | Catégories de données | Lieu | DPA standard |
|---|---|---|---|---|---|
| 1 | **Google LLC** (Gemini Vertex AI) | LLM chat + image gen | Prompt user + history | EU (Belgium europe-west1) | [DPA Google Cloud](https://cloud.google.com/terms/data-processing-addendum) |
| 2 | **OpenAI Inc.** | LLM (modération, embeddings, Whisper STT, TTS, GPT-4o vision) | Prompt user + audio + image | US | [DPA OpenAI](https://openai.com/policies/data-processing-addendum) |
| 3 | **Anthropic PBC** | LLM (Claude — fallback) | Prompt user | US | [DPA Anthropic](https://www.anthropic.com/legal/dpa) |
| 4 | **Alibaba Cloud (Qwen)** | LLM (fallback) | Prompt user | International (DashScope Intl) | À demander |
| 5 | **Sendinblue / Brevo** | Email transactionnel | Email + name | EU (France) | [DPA Brevo](https://www.brevo.com/legal/dpa/) |
| 6 | **Intuition Machines (hCaptcha)** | Captcha | IP + user-agent + browser fingerprint | US | [DPA hCaptcha](https://www.hcaptcha.com/legal/data-protection-addendum) |
| 7 | **Google LLC (Firebase FCM)** | Push notifications | Device token + payload data (task title preview ≤ 140 chars) | US | [DPA Firebase](https://firebase.google.com/terms/data-processing-terms) |
| 8 | **Crisp.chat** | Customer support | Email + name + ticket content | EU (France) | [DPA Crisp](https://crisp.chat/en/security/) |
| 9 | **Hetzner Online GmbH** | Hosting (toutes les données) | Toutes — Postgres + Redis + MinIO | EU (Allemagne) | [DPA Hetzner](https://www.hetzner.com/AV/AV.pdf) + ISO 27001 |
| 10 | **Cloudflare Inc.** | DNS + CDN + WAF | IP + DNS queries + logs HTTP | US/Global | [DPA Cloudflare](https://www.cloudflare.com/cloudflare-customer-dpa/) |

**Pré-launch L2** : DPA **standards** signés avec chaque provider via
acceptation de leur ToS commerciale (Brevo, Hetzner, etc.). Les DPAs
sur-mesure sont possibles pour les contrats enterprise (V2 si NEXYA
atteint un volume justifiant).

---

## Template DPA (à compléter par avocat)

### 1. Parties

- **Responsable du traitement** : Nexyalabs SARL (à créer/préciser),
  numéro RCS [TODO], siège [TODO], DPO `dpo@nexya.ai`
- **Sous-traitant** : [Nom officiel du provider, RCS/numéro
  d'identification, siège, contact DPO]

### 2. Objet

Le présent contrat encadre le traitement des données à caractère
personnel effectué par le sous-traitant pour le compte du responsable
dans le cadre de la fourniture de [DESCRIPTION SERVICE — ex:
hébergement, envoi email, modération IA].

### 3. Nature et finalité du traitement

[Description précise du traitement effectué — ex:
- Réception de prompts user envoyés par l'API NEXYA backend
- Inférence LLM (génération de texte de réponse)
- Retour du résultat à NEXYA
- Pas de stockage permanent côté provider (vérifier ToS provider)]

### 4. Catégories de personnes concernées

- Utilisateurs finaux de l'application NEXYA (mobile + web)
- Plage d'âge : 13+ (cf. ToS NEXYA — pas de mineurs < 13 ans).

### 5. Catégories de données traitées

[Lister les catégories selon le sous-traitant — ex pour OpenAI :]
- Identifiants pseudonymes (user_id UUID, session_id UUID)
- Contenu des messages user (prompts, conversations)
- Métadonnées techniques (IP, user-agent — anonymisée /24)
- Audio (Whisper STT) / Images (Vision multimodal)

**Catégories particulières** (Article 9) :
- ❌ Pas de données de santé (NEXYA refuse les prescriptions
  nominatives via `moderation_rules`)
- ❌ Pas de données biométriques
- ❌ Pas de données concernant l'orientation sexuelle/religion
  (filtrées par `SENSITIVE_KEYWORDS` dans `workers/memory_tasks.py` —
  RGPD Article 9 strict)

### 6. Durée du traitement

- Durée du contrat de prestation entre NEXYA et le sous-traitant.
- À la fin du contrat : suppression complète des données dans un délai
  de 30 jours (Article 28.3.g).

### 7. Mesures de sécurité (Article 32)

Le sous-traitant doit garantir :
- Chiffrement en transit (TLS 1.3+)
- Chiffrement au repos (selon ToS provider)
- Contrôle d'accès au personnel
- Logs d'audit
- Tests de pénétration réguliers
- Plan de réponse aux incidents

### 8. Sous-sous-traitants autorisés

Le sous-traitant ne peut faire appel à un autre sous-traitant qu'avec
**autorisation écrite préalable** du responsable.

[Lister les sous-sous-traitants connus et autorisés — ex pour
Hetzner : centres de données Falkenstein, Nuremberg, Helsinki ; sous-
traitants opérationnels listés dans leur DPA standard.]

### 9. Notification d'incident (Article 33)

Le sous-traitant s'engage à notifier NEXYA **dans les 24 heures** de
toute violation de données concernant les données traitées pour le
compte de NEXYA.

### 10. Droits des personnes (Articles 12-22)

Le sous-traitant doit aider NEXYA à répondre aux demandes des users :
- Article 15 (accès)
- Article 16 (rectification)
- Article 17 (effacement)
- Article 20 (portabilité)

Réponse dans un délai de [X jours ouvrés] sur demande écrite NEXYA.

### 11. Audits

NEXYA peut auditer le sous-traitant sur place ou à distance, avec un
préavis de [X jours]. Coût supporté par [NEXYA / sous-traitant selon
contrat].

### 12. Fin de contrat

À la fin du contrat, le sous-traitant doit (au choix de NEXYA) :
- Supprimer toutes les données personnelles
- OU retourner les données à NEXYA dans un format structuré

Délai : 30 jours maximum.

### 13. Responsabilité et indemnisation

Le sous-traitant indemnise NEXYA en cas de violation de ses
obligations DPA causant un préjudice à NEXYA (amende CNIL, dommages
users).

[Plafond responsabilité à négocier avec chaque provider — typiquement
12 mois de fees pour les prestations cloud SaaS.]

### 14. Loi applicable et juridiction

[Loi française si sous-traitant français, loi du provider sinon.
Juridiction de Paris ou siège du provider.]

### 15. Signatures

[Date / Signatures représentants légaux NEXYA + sous-traitant]

---

## Checklist pré-signature

- [ ] Vérifier que le DPA standard du provider couvre Article 28.3 (a-h)
- [ ] Demander la liste des sous-sous-traitants
- [ ] Vérifier les transferts hors EU (clauses contractuelles types)
- [ ] Vérifier le délai de notification breach (≤ 24h idéal)
- [ ] Identifier le DPO du sous-traitant
- [ ] Archiver le DPA signé dans un dossier RGPD
- [ ] Mettre à jour le **registre des activités de traitement**
  (Article 30) — TODO V1 (template Excel + endpoint admin V2)

---

## TODO Ivan AVANT prod L2

1. **Créer Nexyalabs SARL** (ou structure juridique) si pas déjà fait
2. **Désigner DPO interne** (Ivan V1, externe V2 post 50k users)
3. **Créer alias `dpo@nexya.ai`** + `support@nexya.ai`
4. **Engager consultant DPO/avocat RGPD** Phase M3 pour DPIA + DPA
   sur-mesure des principaux providers
5. **Établir le registre des activités de traitement** Article 30
   (template Excel V1, endpoint admin V2 post-launch)
6. **Vérifier les ToS commerciales** signées avec chaque provider —
   inclut souvent un DPA standard suffisant V1
