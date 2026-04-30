# PRICING_DECISIONS — Gabarit de décision Ivan

> **Document préparé le 2026-04-29 (session P1.cleanup).**
> **À trancher par Ivan AVANT le démarrage L2 staging.**
> **Délai cible : 1-2 semaines à compter de la livraison ci-dessus.**

---

## 0. Préambule — pourquoi ce document existe

### Règle métier (mémoire `feedback_pricing_decisions`)

> Les valeurs qui définissent l'**offre commerciale** NEXYA (quotas Free vs Pro,
> caps qui influencent ce que l'utilisateur peut faire à un plan donné) sont
> tranchées par **Ivan seul** — jamais par l'IA, jamais par un développeur tiers.

### Ce que contient ce document

Au cours des sessions backend D, E, F, J et K, **16 settings** ont été posés
dans `app/config.py` avec un commentaire `# TODO(Ivan): provisoire`. Ces
valeurs sont aujourd'hui des défauts raisonnables qui permettent de tourner
en dev/staging sans bloquer l'ingénierie, mais **elles ne sont pas validées
comme valeurs commerciales finales**.

Pour chacune, ce document fournit :

1. Le **nom et la ligne** dans `app/config.py`.
2. La **valeur provisoire actuelle** et la session qui l'a posée.
3. Le **calcul de coût worst-case** projeté à 950 000 utilisateurs (cible
   produit V1).
4. **Trois options** chiffrées : Conservatrice / Standard / Généreuse.
5. Une **recommandation non-engageante** (par défaut : la valeur provisoire
   actuelle).
6. Une **ligne à remplir par Ivan** : `✏️ TRANCHE IVAN : ____ (date : __________)`

### Ce que ce document n'est PAS

- ❌ **Une décision déjà prise.** Aucune valeur n'est figée par l'IA.
- ❌ **Un brouillon à compléter par un dev.** Seul Ivan peut renseigner les
  lignes `TRANCHE IVAN`.
- ❌ **Un fichier à committer rempli avec des valeurs inventées.** Si une
  ligne `TRANCHE IVAN` est complétée, c'est qu'Ivan l'a validée explicitement.

### Pourquoi en doc-as-code (Markdown dans Git) plutôt qu'en Notion/Confluence

- **Traçabilité** : `git blame` sur la ligne `TRANCHE IVAN` montre exactement
  quand Ivan a tranché et dans quel commit.
- **Audit RGPD/AI Act** : un auditeur peut vérifier que la limite de stockage
  documents Pro = 50 a bien été décidée par le DPO/dirigeant à la date X.
- **Single source of truth** : la valeur `app/config.py` et la décision
  documentée vivent dans le même repo. Pas de drift entre Notion et code.

---

## 1. Vue d'ensemble — tableau récapitulatif des 16 settings

| # | Setting | Valeur provisoire | Bloc | Tranché ? |
|---|---|---|---|---|
| 1 | `documents_max_free` | 3 | D4 | ⏳ |
| 2 | `documents_max_pro` | 50 | D4 | ⏳ |
| 3 | `documents_chunks_per_file_max` | 500 | D4 | ⏳ |
| 4 | `max_concurrent_chunking_per_user` | 2 | D4 | ⏳ |
| 5 | `voice_minutes_pro_per_day` | 120 | E1 | ⏳ |
| 6 | `voice_tts_chars_pro_per_day` | 50 000 | E1 | ⏳ |
| 7 | `image_no_watermark_price_multiplier` | 2.0 | E4 | ⏳ |
| 8 | `vision_images_free_per_day` | 3 | E2 | ⏳ |
| 9 | `vision_images_pro_per_day` | 50 | E2 | ⏳ |
| 10 | `vision_max_images_per_request` | 4 | E2 | ⏳ |
| 11 | `vision_max_output_tokens_pro` | 4 096 | E2 | ⏳ |
| 12 | `tasks_max_free` | 3 | F1 | ⏳ |
| 13 | `tasks_max_pro` | 50 | F1 | ⏳ |
| 14 | `rgpd_deletion_grace_period_days` | 30 | J1 | ⏳ |
| 15 | `rgpd_export_max_size_bytes` | 100 MB | J1 | ⏳ |
| 16 | `cost_usd_daily_alert_threshold` | 100 USD | K2 | ⏳ |

> **Hypothèse de calcul** : 950 000 utilisateurs au total, ratio Pro/Free
> conservateur **10 %** (95 000 Pro / 855 000 Free) basé sur les freemium
> SaaS comparables (Notion, Linear). Si Ivan vise un ratio plus haut/bas,
> ajuster les chiffres ci-dessous proportionnellement.

---

## 2. Bloc D4 — RAG Documents (4 settings)

### 2.1 `documents_max_free` — Documents actifs maximum par compte Free

- **Ligne** : `app/config.py:286`
- **Valeur provisoire actuelle** : `3`
- **Session** : D4 (livré 2026-04-24)
- **Définition** : nombre maximum de documents non soft-deleted qu'un Free peut
  conserver indexés (RAG) en parallèle. Limite UX qui pousse au Pro pour les
  utilisateurs sérieux.

**Coût worst-case 950k users** : marginal côté stockage (3 docs × ~5 MB = 15 MB
par Free × 855k Free = ~13 TB stockage R2 ≈ **195 USD/mois** à $0.015/GB).
Embeddings déjà payés à l'indexation.

**Trois options** :

| Option | Valeur | Implication produit |
|---|---|---|
| **Conservatrice** | `1` | Free quasi-démo. Pousse vite à l'upgrade. Risque churn. |
| **Standard** *(provisoire)* | `3` | Démo significative (3 docs = 1 facture + 1 contrat + 1 cours). |
| **Généreuse** | `5` | Free très utilisable, peut limiter conversions Pro. |

**Reco non-engageante** : `3` (valeur provisoire). 3 documents = un cas d'usage
crédible démontrable, en restant en-deçà du seuil critique d'un usage régulier.

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

### 2.2 `documents_max_pro` — Documents actifs maximum par compte Pro

- **Ligne** : `app/config.py:287`
- **Valeur provisoire actuelle** : `50`
- **Session** : D4 (livré 2026-04-24)
- **Définition** : cap quota Pro. Limite l'abus (un Pro qui dump 10 000 PDFs
  pour transformer NEXYA en NAS) tout en couvrant 99 % des usages légitimes.

**Coût worst-case 950k users (95k Pro)** : 95k × 50 docs × 5 MB = ~24 TB ≈
**360 USD/mois** stockage R2. Embeddings ~$0.02/1M tokens × 50 docs × 50k tokens
× 95k Pro = **~5 000 USD à l'ingestion** (one-shot, pas mensuel).

**Trois options** :

| Option | Valeur | Implication produit |
|---|---|---|
| **Conservatrice** | `25` | Limite stricte, faible coût stockage. Plafonne power users. |
| **Standard** *(provisoire)* | `50` | Couvre 99 % des cas (étude/recherche perso). |
| **Généreuse** | `200` | Marketing « illimité ou presque ». Risque abus NAS. |

**Reco non-engageante** : `50`. Au-delà, le RAG perd en pertinence (top-K cosinus
dilué) — limite technique avant économique.

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

### 2.3 `documents_chunks_per_file_max` — Plafond de qualité de service par document

- **Ligne** : `app/config.py:290`
- **Valeur provisoire actuelle** : `500`
- **Session** : D4 (livré 2026-04-24)
- **Définition** : taille maximale acceptée d'un document à l'ingestion. À 500
  tokens/chunk, 500 chunks = ~250 000 tokens ≈ 1 000 pages PDF dense. Au-delà,
  refus 413.

**Coût worst-case** : un user qui upload 50 docs × 500 chunks × 500 tokens =
12.5M tokens × $0.02/1M = **$0.25 par user à l'ingestion** (one-shot).

**Trois options** :

| Option | Valeur | Implication produit |
|---|---|---|
| **Conservatrice** | `200` | ~400 pages max. Refuse les rapports volumineux. |
| **Standard** *(provisoire)* | `500` | ~1000 pages, couvre livres et thèses. |
| **Généreuse** | `2000` | ~4000 pages. Coût ingestion × 4. |

**Reco non-engageante** : `500`. Au-delà, le RAG perd en précision sur un seul doc.

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

### 2.4 `max_concurrent_chunking_per_user` — Parallélisme worker chunking

- **Ligne** : `app/config.py:292`
- **Valeur provisoire actuelle** : `2`
- **Session** : D4 (livré 2026-04-24)
- **Définition** : nombre max de jobs de chunking en parallèle pour un même
  user. Capacité que paye le Pro vs Free. Un Pro qui upload 10 docs simultané-
  ment voit 2 être traités en parallèle, les 8 autres en file d'attente.

**Coût worst-case** : 95k Pro × 2 workers parallèles → si 1 % actifs simultané-
ment = 1 900 jobs concurrents → **~5 workers arq dimensionnement Hetzner CCX23**
suffisent (chaque job ~30 s).

**Trois options** :

| Option | Valeur | Implication produit |
|---|---|---|
| **Conservatrice** | `1` | Série stricte. UX lente sur batch upload. |
| **Standard** *(provisoire)* | `2` | Compromis UX/coût raisonnable. |
| **Généreuse** | `5` | Batch upload fluide. Coût × 2.5 dimensionnement workers. |

**Reco non-engageante** : `2` côté Pro. Pour Free, fixer à `1` séparément si
distinction souhaitée (setting actuellement uniforme — Ivan peut demander
split Free/Pro à cette occasion).

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

## 3. Bloc E1 — Voice (2 settings, Pro only)

### 3.1 `voice_minutes_pro_per_day` — Minutes STT Whisper par Pro/jour

- **Ligne** : `app/config.py:313`
- **Valeur provisoire actuelle** : `120`
- **Session** : E1 (livré 2026-04-23)
- **Définition** : quota quotidien transcription audio (Whisper API). Free passe
  par STT natif Flutter (gratuit pour NEXYA), seuls les Pro consomment ce quota.

**Coût worst-case 950k users (95k Pro)** : Whisper API = $0.006/min. Si 10 % des
Pro utilisent leur quota max chaque jour : 9 500 × 120 min × $0.006 = **6 840
USD/jour ≈ 205 000 USD/mois**. Si seulement 1 % utilisent max : ~20 500 USD/mois.

**Trois options** :

| Option | Valeur | Implication produit | Coût mensuel worst-case (10 % actifs max) |
|---|---|---|---|
| **Conservatrice** | `30` | 30 min/j = 1 réunion. Pousse à l'add-on. | ~51 000 USD/mois |
| **Standard** *(provisoire)* | `120` | 2h/j = travail focus session. | ~205 000 USD/mois |
| **Généreuse** | `300` | 5h/j = power user transcripteur pro. | ~512 000 USD/mois |

**Reco non-engageante** : `120` paraît raisonnable, mais **risque budgétaire
élevé** si 10 % des Pro consomment max. À envisager : ramener à `60` et offrir
add-on payant pour quota supérieur.

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

### 3.2 `voice_tts_chars_pro_per_day` — Caractères TTS OpenAI par Pro/jour

- **Ligne** : `app/config.py:314`
- **Valeur provisoire actuelle** : `50 000`
- **Session** : E1 (livré 2026-04-23)
- **Définition** : quota quotidien synthèse vocale (OpenAI tts-1). Free passe par
  `flutter_tts` natif (voix robotiques mais gratuit).

**Coût worst-case** : tts-1 = $15/1M chars. 95k Pro × 50k chars × 10 % actifs =
4.75 Mds chars/jour × $15/Md = **71 USD/jour ≈ 2 130 USD/mois**. Beaucoup plus
modeste que le STT.

**Trois options** :

| Option | Valeur | Implication produit | Coût mensuel worst-case |
|---|---|---|---|
| **Conservatrice** | `10 000` | ~7 min audio/j (lecture article). | ~430 USD/mois |
| **Standard** *(provisoire)* | `50 000` | ~35 min audio/j (livre court). | ~2 130 USD/mois |
| **Généreuse** | `200 000` | ~2h audio/j (audiobook). | ~8 540 USD/mois |

**Reco non-engageante** : `50 000`. Coût absorbable, UX confortable.

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

## 4. Bloc E2 — Vision (4 settings, asymétrie tier Free/Pro)

### 4.1 `vision_images_free_per_day` — Images analysées Free/jour (Gemini Flash imposé)

- **Ligne** : `app/config.py:360`
- **Valeur provisoire actuelle** : `3`
- **Session** : E2 (livré 2026-04-23)
- **Définition** : Free a accès à l'analyse multimodale tier=`flash` (cheap),
  3 images/jour suffit pour démo.

**Coût worst-case 950k users (855k Free)** : Gemini 2.0 Flash = $0.075/1M tokens
in. 855k × 3 images × ~1k tokens × 10 % actifs = **~19 USD/jour ≈ 580 USD/mois**.
Très absorbable.

**Trois options** :

| Option | Valeur | Implication produit |
|---|---|---|
| **Conservatrice** | `1` | Démo minimale. Pousse vite à Pro. |
| **Standard** *(provisoire)* | `3` | Démo significative quotidienne. |
| **Généreuse** | `10` | Free très utilisable, coût × 3 |

**Reco non-engageante** : `3`. Coût marginal, UX démo crédible.

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

### 4.2 `vision_images_pro_per_day` — Images analysées Pro/jour (tier flash ou pro)

- **Ligne** : `app/config.py:361`
- **Valeur provisoire actuelle** : `50`
- **Session** : E2 (livré 2026-04-23)

**Coût worst-case (95k Pro, max tier=pro Gemini)** : Gemini Pro = $1.25/1M tokens
in. 95k × 50 × 1k tokens × 10 % actifs = **~594 USD/jour ≈ 17 800 USD/mois**.

**Trois options** :

| Option | Valeur | Implication produit | Coût mensuel worst-case |
|---|---|---|---|
| **Conservatrice** | `20` | Limite analyse intensive. | ~7 100 USD/mois |
| **Standard** *(provisoire)* | `50` | Power user crédible. | ~17 800 USD/mois |
| **Généreuse** | `200` | Pro illimité de fait. | ~71 000 USD/mois |

**Reco non-engageante** : `50`. Au-delà, l'utilisation devient industrielle —
add-on dédié plus pertinent.

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

### 4.3 `vision_max_images_per_request` — Images max par requête Vision

- **Ligne** : `app/config.py:362-364`
- **Valeur provisoire actuelle** : `4`
- **Session** : E2 (livré 2026-04-23)
- **Définition** : 1 image principale + jusqu'à 3 images additionnelles dans la
  même requête (multi-image comparison). Capacité Pro.

**Coût worst-case** : multiplie le coût d'une requête vision par 4 si l'user
joint le max. Compté dans `vision_images_pro_per_day` global.

**Trois options** :

| Option | Valeur | Implication produit |
|---|---|---|
| **Conservatrice** | `1` | Pas de comparison multi-image. UX limitée. |
| **Standard** *(provisoire)* | `4` | Comparison crédible (avant/après × 2). |
| **Généreuse** | `10` | Album / contact-sheet analysis. |

**Reco non-engageante** : `4`. Au-delà, le contexte multimodal sature et la
qualité de réponse chute.

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

### 4.4 `vision_max_output_tokens_pro` — Tokens max sortie Vision Pro

- **Ligne** : `app/config.py:365-367`
- **Valeur provisoire actuelle** : `4 096`
- **Session** : E2 (livré 2026-04-23)
- **Définition** : cap output Gemini Pro / GPT-4o pour analyse vision. 4 096
  tokens ≈ 3 000 mots ≈ rapport détaillé.

**Coût worst-case** : output Gemini Pro = $5.00/1M tokens. Compté par requête.

**Trois options** :

| Option | Valeur | Implication produit |
|---|---|---|
| **Conservatrice** | `1 024` | Réponse courte, ~750 mots. |
| **Standard** *(provisoire)* | `4 096` | Rapport détaillé. |
| **Généreuse** | `8 192` | Document long, max API. |

**Reco non-engageante** : `4 096`. Couvre 99 % des usages, double si Ivan vise
le segment « rapport médical / analyse architecte ».

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

## 5. Bloc E4 — Watermark (1 setting)

### 5.1 `image_no_watermark_price_multiplier` — Ratio prix image sans vs avec watermark

- **Ligne** : `app/config.py:345-347`
- **Valeur provisoire actuelle** : `2.0`
- **Session** : E4 (livré 2026-04-25)
- **Définition** : multiplicateur de prix appliqué quand un Pro demande une
  image **sans watermark NEXYA**. À implémenter en wallet V2 (post-V1 lancement).
  Voir mémoire `project_nexya_pricing_model_v2.md`.

**Implication produit** : signal de positionnement marque. À 2.0×, retirer le
watermark coûte deux fois plus que le générer avec — incite à laisser la marque
visible (acquisition virale).

**Trois options** :

| Option | Valeur | Positionnement marque |
|---|---|---|
| **Conservatrice** | `1.5` | Léger surcoût, retirer = trivial. |
| **Standard** *(provisoire)* | `2.0` | Surcoût net, vrai choix. |
| **Premium** | `3.0` | Watermark NEXYA = avantage évident. Acquisition virale forte. |

**Reco non-engageante** : `2.0` cohérent avec la psychologie du double-payement
visible (« c'est cher mais clair »).

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

## 6. Bloc F1 — Planificateur (2 settings)

### 6.1 `tasks_max_free` — Tâches planifiées maximum par compte Free

- **Ligne** : `app/config.py:394`
- **Valeur provisoire actuelle** : `3`
- **Session** : F1 (livré 2026-04-25)

**Coût worst-case** : marginal (1 row DB par tâche, exécution = 1 appel LLM
chat compté ailleurs).

**Trois options** :

| Option | Valeur | Implication produit |
|---|---|---|
| **Conservatrice** | `1` | Démo minimale. |
| **Standard** *(provisoire)* | `3` | Routines quotidiennes crédibles. |
| **Généreuse** | `5` | Free très utilisable. |

**Reco non-engageante** : `3`. Cohérent avec `documents_max_free=3` et
`vision_images_free_per_day=3` — pattern UX uniforme.

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

### 6.2 `tasks_max_pro` — Tâches planifiées maximum par compte Pro

- **Ligne** : `app/config.py:395`
- **Valeur provisoire actuelle** : `50`
- **Session** : F1 (livré 2026-04-25)

**Coût worst-case** : 95k Pro × 50 tâches × 1 exécution/jour × ~1k tokens chat
× $1/1M = **~5 USD/jour ≈ 150 USD/mois**.

**Trois options** :

| Option | Valeur | Implication produit |
|---|---|---|
| **Conservatrice** | `25` | Power user couvert, anti-abus. |
| **Standard** *(provisoire)* | `50` | Couvre quasi tous les cas légitimes. |
| **Généreuse** | `200` | Quasi-illimité, anti-abus distant. |

**Reco non-engageante** : `50`. Cohérent avec `documents_max_pro=50` —
pattern UX uniforme.

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

## 7. Bloc J1 — RGPD (2 settings)

### 7.1 `rgpd_deletion_grace_period_days` — Délai avant hard delete RGPD

- **Ligne** : `app/config.py:678`
- **Valeur provisoire actuelle** : `30`
- **Session** : J1 (livré 2026-04-26)
- **Définition** : délai entre `DELETE /user/account` (soft delete + queue) et
  la purge physique cron `purge_deleted_accounts`. Article 17 RGPD : « sans
  retard injustifié ». La CNIL recommande 30 j pour permettre la rétractation.

**Implication compliance + UX** :

| Option | Valeur | Compliance | UX user |
|---|---|---|---|
| **Express** | `7` | OK CNIL minimum. | Réinscription rapide possible mais oubli garanti. |
| **Standard** *(provisoire)* | `30` | Reco CNIL standard. | Bonne fenêtre rétractation. |
| **Conservatrice** | `90` | Plus que CNIL. | Rétractation très large mais fenêtre risque RGPD. |

**Reco non-engageante** : `30` (reco CNIL). En-dessous, risque erreur user
irrécupérable. Au-dessus, signal RGPD négatif (« vous gardez nos données ? »).

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

### 7.2 `rgpd_export_max_size_bytes` — Taille max ZIP export RGPD

- **Ligne** : `app/config.py:682-684`
- **Valeur provisoire actuelle** : `100 * 1024 * 1024` = **100 MB**
- **Session** : J1 (livré 2026-04-26)
- **Définition** : cap soft sur l'export Article 15. Au-delà, ZIP créé avec flag
  `truncated=True` dans manifest.

**Coût worst-case** : 100 MB × 95k Pro × 1 export/an = ~9.5 TB/an × $0.015/GB =
~150 USD/an stockage temporaire R2 (TTL 7j → quasi-zero coût mensuel).

**Trois options** :

| Option | Valeur | Implication |
|---|---|---|
| **Conservatrice** | `25 MB` | Téléchargement rapide. Risque truncate fréquent. |
| **Standard** *(provisoire)* | `100 MB` | Couvre 99 % des comptes. |
| **Généreuse** | `500 MB` | Aucun truncate sauf comptes industriels. |

**Reco non-engageante** : `100 MB`. Au-delà, l'user devrait passer par un
export segmenté.

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

## 8. Bloc K2 — Alerting (1 setting)

### 8.1 `cost_usd_daily_alert_threshold` — Seuil USD/jour alerte cost-explosion

- **Ligne** : `app/config.py:738`
- **Valeur provisoire actuelle** : `100.0`
- **Session** : K2 (livré 2026-04-26)
- **Définition** : seuil de l'alerte Prometheus `NexyaCostUSDDailyExceeded`.
  À 100 USD/jour, un signal d'attaque/abus (clé API leakée, boucle infinie)
  déclenche un page oncall.

**Calibrage** :

| Option | Valeur | Sensibilité |
|---|---|---|
| **Sentinelle paranoïaque** | `20 USD` | Page sur tout pic anormal (high false positive en croissance). |
| **Sentinelle équilibrée** *(provisoire)* | `100 USD` | Signal d'attaque en V1 lancement. |
| **Sentinelle high-volume** | `500 USD` | Mode normal post-traction (>10k Pro actifs). |

**Reco non-engageante** : `100 USD` pour V1 lancement (faible volume légitime,
sensible aux fuites). À ajuster à `500 USD` quand 10k Pro actifs atteints.

✏️ **TRANCHE IVAN** : `____` (date : __________)

---

## 9. Action — Quand Ivan tranche

1. Compléter chaque ligne `✏️ TRANCHE IVAN` avec la valeur retenue + date.
2. Ouvrir une PR `pricing-final-v1` qui :
   - Pose les valeurs définitives dans `app/config.py` (remplace les `default=`).
   - Retire les commentaires `# TODO(Ivan): provisoire` (et les blocs PRICING).
   - Met à jour ce fichier avec un en-tête `> ✅ Validé Ivan le YYYY-MM-DD —
     toutes lignes tranchées.`
3. Merge sur `main` AVANT de lancer L2 staging Hetzner.

---

## 10. Anti-pattern à proscrire

- ❌ **Ne jamais commiter ce fichier avec des valeurs `TRANCHE IVAN` remplies
  par autre chose qu'Ivan lui-même.** Ce document est un gabarit, pas un
  brouillon collaboratif.
- ❌ **Ne pas modifier `app/config.py` pour mettre à jour ces 16 valeurs sans
  qu'Ivan ait validé chaque ligne dans ce document.**
- ❌ **Ne pas ajouter de nouveaux settings pricing TODO sans en discuter
  d'abord avec Ivan** (règle mémoire `feedback_pricing_decisions` étendue à
  toute future session).

---

> *Préparé par session P1.cleanup le 2026-04-29 — voir `nexya_backend/CLAUDE.md`
> §15 entrée du jour pour le contexte. À trancher AVANT L2 staging.*
