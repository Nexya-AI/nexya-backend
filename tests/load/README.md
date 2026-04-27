# Tests de charge k6 — NEXYA Backend (Session N4 volet A)

Suite reproductible de tests de charge HTTP/SSE sur les endpoints
critiques NEXYA. Détecte les régressions de **performance** (silent
N+1, middleware bloquant, ORM lazy mal câblé) avant prod.

> **Pendant** que [`tests/evals/`](../evals/README.md) (N3) garantit
> que la **qualité IA** ne dérive pas, `tests/load/` (N4) garantit que
> la **performance** ne dérive pas. Complémentaires.

## 🎯 Ce qu'on teste

| Scénario | Profil | Cible SLO p95 |
|---|---|---|
| `auth_burst` | 50 RPS register+login / 60s | login <500ms / register <800ms |
| `chat_stream_concurrent` | 30 VUs / 5min SSE concurrent | total stream <30s |
| `files_upload_concurrent` | 20 VUs / 2min uploads 1MB | <3000ms |
| `conversations_list_paginated` | 100 RPS keyset cursor / 60s | <300ms (anti-N+1) |
| `metrics_endpoint` | 200 RPS GET /metrics / 30s | <100ms |
| `mixed_workload` | 30 VUs ramping 5min, 60/30/10 chat/list/upload | http_req_duration p95 <5s |

Tous les seuils sont codifiés dans [`thresholds.json`](thresholds.json).

## 🚀 Lancer en local

### Pré-requis
- Docker + Docker Compose installés
- k6 installé : https://k6.io/docs/get-started/installation/
- bash (Linux/Mac/WSL/Git Bash)

### Tous les scénarios
```bash
bash tests/load/run.sh
```

Le script :
1. Génère les clés JWT volatiles dans `tests/load/tmp/`
2. Lance la stack docker-compose (postgres + redis + minio + backend mock)
3. Migrations Alembic + seed dev users
4. Exécute les 6 scénarios séquentiellement
5. Tear down + purge volumes

### Un scénario seul
```bash
bash tests/load/run.sh --scenario auth_burst
```

### Garder la stack après le run (debug)
```bash
bash tests/load/run.sh --scenario chat_stream_concurrent --no-teardown
```

### Skip le bootstrap (stack déjà up)
```bash
bash tests/load/run.sh --skip-bootstrap --scenario metrics_endpoint
```

## 📊 Comment lire le rapport

k6 affiche en stdout un résumé final + les thresholds (✓ pass / ✗ fail).
Les rapports JSON détaillés sont dans `tests/load/reports/<name>_<timestamp>_summary.json`.

Indicateurs clés :
- `http_req_duration{p(95)}` : latence p95 HTTP (toutes requêtes)
- `http_req_failed` : taux de requêtes en erreur (4xx/5xx)
- `chat_total_duration_ms{p(95)}` : durée SSE end-to-end p95
- `checks` : taux de validations applicatives (cf. `check(...)` dans les .js)

## ➕ Ajouter un nouveau scénario

1. Créer `tests/load/scenarios/<name>.js` (s'inspirer d'un existant)
2. Importer la lib partagée (`auth.js`, `sse.js`, `metrics.js`)
3. Définir `options.scenarios` (executor + rate/vus + duration)
4. Définir `options.thresholds` (SLO pass/fail)
5. Ajouter le nom dans `thresholds.json`
6. Ajouter le nom dans `EXPECTED_SCENARIOS` de [`tests/test_load_thresholds.py`](../test_load_thresholds.py)
7. Ajouter le nom dans le dropdown du workflow [`load.yml`](../../.github/workflows/load.yml)

## 🔄 Mettre à jour les SLO

Les SLO vivent dans :
1. **`thresholds.json`** — source figée pour audit / diff PR
2. **`options.thresholds`** dans chaque `.js` — cible fonctionnelle k6

Quand un PR optimise / dégrade volontairement un endpoint :
1. Lancer le scénario localement pour mesurer la nouvelle réalité
2. Ajuster `thresholds.json` + le `.js` correspondant
3. Le commit visible en review : un reviewer doit pouvoir **expliquer**
   pourquoi un seuil bouge.

**Anti-pattern** : pousser un seuil pour faire passer un PR qui régresse.
Le test perd sa valeur.

## 🤖 Comment intervenir sur un breach CI

### Workflow manuel (workflow_dispatch)
1. Aller dans **Actions** → **Load Tests k6** → **Run workflow**
2. Choisir scenario (ou `all`)
3. Si breach : artifact `load-reports` téléchargé contient les détails
4. Issue auto créée avec label `load-regression`

### Cron weekly (Sunday 4h UTC)
- Run automatique toutes les semaines
- Si breach : issue auto comme ci-dessus
- Pas bloquant pour le merge — c'est un signal de tendance

## ❌ Hors scope V1 (différé V2)

- **Soak test 24h** : besoin staging stable + budget cloud k6.
- **Distributed runners** (k6 cloud, k6 operator k8s) : single-runner
  suffit pour 100 RPS V1.
- **Load tests paiements** : endpoints `/subscriptions/*` n'existent pas
  encore (Phase 11 — I1/I2/I3).
- **Profiling Python sous charge** (py-spy, scalene) : V2.
- **Chaos testing** (toxiproxy) : V2.
- **Ingestion JSON load → Prometheus Pushgateway → Grafana K2** : V2,
  V1 = HTML/JSON local + artifact CI suffit.

## 🧠 Notes pédagogiques

### Pourquoi mocker le LLM sous charge ?
Counter-intuitif mais essentiel : la latence Gemini dépend de Google,
pas de NEXYA. Si Gemini a 5s p99 demain, c'est leur problème, pas une
régression NEXYA. On teste **la chaîne HTTP/DB/Redis**, pas la latence
LLM externe.

### k6 vs Locust ?
k6 a été choisi pour : SSE handling natif, scripts JS modernes,
threshold codifiés (`p95<200ms`), `setup-k6-action` officiel GitHub
Actions, intégration Grafana cloud (V2). Locust = Python aligné stack
mais SSE moins idiomatique + scripts plus verbeux.

### Pourquoi seed_dev avant chaque run ?
Les scénarios `chat_stream_concurrent` + `conversations_list_paginated`
ont besoin de **vrais users authentifiables** (free@nexya.ai +
pro@nexya.ai). `bootstrap.sh` lance `python -m scripts.seed_dev` qui
upsert ces 2 comptes en idempotence.
