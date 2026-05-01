# TODO DE RÉMÉDIATION — NEXYA BACKEND (Session 2026-05-01)

> Compagnon opérationnel du `PROMPT_REMEDIATION_BACKEND_NEXYA_2026-05-01.md`.
> ~180 items atomiques cochables au fur et à mesure.
> Chaque item = une action ou une vérification avec critère d'acceptation.

**Total estimé : 7h** — 1h × 3 fixes code + 2h × 2 préparations infra + ~30 min validation finale.

---

## PHASE 0 — Préparation (~10 min)

- [ ] Lire intégralement `PROMPT_REMEDIATION_BACKEND_NEXYA_2026-05-01.md`
- [ ] Confirmer que la suite globale est verte AVANT de commencer : `pytest tests/ -p no:warnings -q | tail -5` → attendu `1583 passed` ou équivalent récent
- [ ] Vérifier que `docker compose -f docker/docker-compose.yml ps` montre postgres + redis healthy (pour les tests qui le demandent)
- [ ] Créer une branche dédiée si workflow git (optionnel) : `git checkout -b audit/remediation-2026-05-01`
- [ ] Confirmer que `bash` est disponible (pour les tests structure scripts) : `bash --version`

---

## TÂCHE 1 — `/auth/refresh` rate limit IP (1h, S0)

### T1.1 — Lecture des fichiers (~10 min)

- [ ] `Read app/core/security/rate_limiter.py` (intégralement)
- [ ] Identifier le helper `rate_limit_login(request)` ligne 89 — modèle à reproduire
- [ ] Identifier `check_ip_rate_limit(request, action, max_requests, window_seconds)` ligne 49
- [ ] Identifier l'exception `RateLimitIPException` (provient de `app/core/errors/exceptions.py`)
- [ ] `Read app/features/auth/router.py` lignes 193-200 (endpoint `refresh()` actuel)
- [ ] `Read tests/test_auth_hardening.py` (parcours rapide pour identifier le pattern de test rate limit)
- [ ] `Read tests/test_password_reset.py` (parcours pour pattern monkeypatch redis si applicable)

### T1.2 — Implémentation `rate_limit_refresh` (~10 min)

- [ ] Ouvrir `app/core/security/rate_limiter.py`
- [ ] Ajouter le helper `rate_limit_refresh(request)` **APRÈS** `rate_limit_register` ligne 96, **AVANT** la section `RATE LIMIT — user-scoped` ligne 99
- [ ] Docstring complète FR : justifie `20/min` (vs 10/min login), explique le risque refresh leaké, mentionne que c'est moins coûteux qu'un login
- [ ] Appel `await check_ip_rate_limit(request, action="refresh", max_requests=20)` (window_seconds=60 par défaut)

### T1.3 — Câblage dans le router (~10 min)

- [ ] Ouvrir `app/features/auth/router.py`
- [ ] Ajouter `rate_limit_refresh` à l'import multi-ligne lignes 28-34 (ordre alphabétique)
- [ ] Modifier la signature de `refresh()` ligne 193-197 pour ajouter `request: Request` (avant `db`)
- [ ] Ajouter `await rate_limit_refresh(request)` comme première ligne de la fonction (avant `tokens = await auth_service.refresh(...)`)
- [ ] Mettre à jour la docstring : mentionner le rate limit 20/min

### T1.4 — Tests (~25 min)

- [ ] Ouvrir `tests/test_auth_hardening.py`
- [ ] Identifier le pattern existant pour tester un router + rate limit (chercher `dependency_overrides` + `monkeypatch.setattr.*get_redis`)
- [ ] Ajouter `test_refresh_rate_limit_allows_under_threshold` — happy path :
  - Setup : `monkeypatch` `auth_service.refresh` pour retourner un fake `TokenResponse`
  - Mock le redis incr pour simuler un compteur sous le seuil
  - 3 POST `/auth/refresh` → asserter 3 × 200, service appelé 3 fois
- [ ] Ajouter `test_refresh_rate_limit_blocks_over_threshold` — 429 :
  - Mock le redis incr pour retourner 21 (sur le 21ᵉ appel)
  - 1 POST `/auth/refresh` → asserter 429, `code == "RATE_LIMIT_IP"`, `data.retry_after > 0`
  - Asserter que `auth_service.refresh` n'a **jamais** été appelé
- [ ] Vérifier que les tests utilisent bien `request: Request` (sinon FastAPI ne peut pas injecter)

### T1.5 — Exécution + validation (~5 min)

- [ ] `pytest tests/test_auth_hardening.py -v -k refresh` → 2 tests verts
- [ ] `pytest tests/test_auth_hardening.py -v` → toute la suite reste verte
- [ ] `pytest tests/test_password_reset.py tests/test_auth_hardening_a3.py -v` → 0 régression
- [ ] Vérifier `ruff check app/features/auth/router.py app/core/security/rate_limiter.py` → pas de warning

### T1.6 — Critères d'acceptation T1

- [ ] Le fichier `rate_limiter.py` contient `async def rate_limit_refresh(request: Request) -> None`
- [ ] Le router `refresh()` accepte `request: Request` et appelle `rate_limit_refresh` AVANT `auth_service.refresh`
- [ ] 2 nouveaux tests dans `test_auth_hardening.py` verts
- [ ] La docstring mentionne explicitement le seuil `20/min` et la justification fail-fail-open vs login
- [ ] Le commit cumulé n'introduit pas de fichier ni de symbole non listé dans le prompt

---

## TÂCHE 2 — Cap `max_tokens` par défaut sur experts (1h, S1)

### T2.1 — Lecture des fichiers (~10 min)

- [ ] `Read app/ai/experts.py` lignes 285-444 (registre EXPERT_REGISTRY)
- [ ] `Read app/ai/streaming.py` lignes 550-565 (vérifier `max_tokens=ctx.max_tokens or config.max_tokens`)
- [ ] `Read app/ai/providers/gemini.py` ligne 158-160 (vérifier `request.max_tokens` → `max_output_tokens`)
- [ ] `Read app/ai/providers/openai_provider.py` lignes 204-209 (vérifier `request.max_tokens` → `max_tokens` ou `max_completion_tokens` pour o1)
- [ ] `Read tests/test_experts_registry.py` (parcours pour identifier le pattern)

### T2.2 — Modification de `EXPERT_REGISTRY` (~25 min)

- [ ] Modifier `app/ai/experts.py` — entrée `general` (ligne 286-297) : ajouter `max_tokens=2048` après `temperature=0.7`
- [ ] Modifier entrée `computer` (lignes 298-309) : ajouter `max_tokens=2048` après `temperature=0.3`
- [ ] Modifier entrée `science` (lignes 310-321) : ajouter `max_tokens=4096` après `temperature=0.2`
- [ ] Modifier entrée `finance` (lignes 322-333) : ajouter `max_tokens=2048` après `temperature=0.4`
- [ ] Modifier entrée `language` (lignes 334-355) : ajouter `max_tokens=4096` après `temperature=0.5`
- [ ] Modifier entrée `cooking` (lignes 356-367) : ajouter `max_tokens=2048` après `temperature=0.7`
- [ ] Modifier entrée `studio` (lignes 369-380) : ajouter `max_tokens=2048` après `temperature=0.0`
- [ ] Modifier entrée `engineering` (lignes 381-392) : ajouter `max_tokens=4096` après `temperature=0.2`
- [ ] Modifier entrée `productivity` (lignes 393-404) : ajouter `max_tokens=2048` après `temperature=0.6`
- [ ] Modifier entrée `medicine` (lignes 405-425) : ajouter `max_tokens=3072` après `temperature=0.1`
- [ ] Modifier entrée `legal` (lignes 426-443) : ajouter `max_tokens=3072` après `temperature=0.1`
- [ ] Vérification : `grep -n "max_tokens=" app/ai/experts.py` → doit voir 11 occurrences (1 par expert)
- [ ] Vérification : aucune entrée n'a `max_tokens=None` (à part le default de la dataclass qui reste pour les usages exotiques)

### T2.3 — Tests (~15 min)

- [ ] Ouvrir `tests/test_experts_registry.py`
- [ ] Ajouter `test_all_experts_have_max_tokens_cap` :
  - Boucle sur `EXPERT_REGISTRY` : asserter `config.max_tokens is not None`
  - Asserter `config.max_tokens > 0`
  - Asserter `config.max_tokens <= 8192` (cap raisonnable)
- [ ] Ajouter `test_max_tokens_aligned_with_tier` :
  - Récupérer la liste `flash_caps` et `pro_caps`
  - Asserter `max(flash_caps) <= max(pro_caps)`
- [ ] (Optionnel) Ajouter `test_safety_critical_experts_have_capped_tokens` :
  - Pour `medicine` et `legal`, asserter `max_tokens <= 4096` (info structurée, pas de génération créative)

### T2.4 — Exécution + validation (~10 min)

- [ ] `pytest tests/test_experts_registry.py -v` → 2 (ou 3) nouveaux tests verts + suite existante 0 régression
- [ ] `pytest tests/test_llm_router.py -v` → 0 régression (le router consomme `EXPERT_REGISTRY` indirectement)
- [ ] `pytest tests/test_chat_stream_persisted.py -v` → 0 régression (le router chat consomme la résolution + max_tokens)
- [ ] `ruff check app/ai/experts.py tests/test_experts_registry.py` → 0 warning

### T2.5 — Critères d'acceptation T2

- [ ] 11 entrées de `EXPERT_REGISTRY` ont `max_tokens=N` explicite (visible via grep)
- [ ] Valeurs alignées sur la grille : flash=2048, pro=4096, safety-critical pro=3072, studio=2048
- [ ] 2 (ou 3) nouveaux tests dans `test_experts_registry.py` verts
- [ ] La classe `ExpertConfig` n'est PAS modifiée (le défaut `None` reste)
- [ ] `streaming.py` n'est PAS modifié (la chaîne `ctx.max_tokens or config.max_tokens` fonctionne déjà)

---

## TÂCHE 3 — Blacklist JWT alert si Redis down (1h, S1)

### T3.1 — Lecture des fichiers (~10 min)

- [ ] `Read app/core/observability/prometheus.py` (lignes 1-280, comprendre le pattern Counter/Gauge/Histogram + `_safe_call` + `_reset_for_tests`)
- [ ] `Read app/core/auth/jwt.py` (intégralement, identifier `is_token_blacklisted` lignes 102-106)
- [ ] `Read tests/test_auth_hardening.py` (pattern existant pour tester avec monkeypatch)
- [ ] `Read tests/test_prometheus_metrics.py` (pattern test métrique avec `_reset_for_tests` + `setup_prometheus`)
- [ ] Identifier les tests qui asserter `metrics_count == 13` — il faudra les bumper à 14

### T3.2 — Ajouter la métrique Prometheus (~15 min)

- [ ] Ouvrir `app/core/observability/prometheus.py`
- [ ] Ligne 67 environ (à côté des autres globals) : ajouter `auth_blacklist_check_failed_total: Any = None`
- [ ] Dans `setup_prometheus` (ligne ~78-85), ajouter `global auth_blacklist_check_failed_total` à la liste des globals déclarés
- [ ] Après le bloc `cache_operations_total = Counter(...)` (ligne ~206), ajouter le `Counter("nexya_auth_blacklist_check_failed_total", ..., labelnames=("error_type",))`
- [ ] Mettre à jour `metrics_count=14` ligne 211
- [ ] Dans `_reset_for_tests` (lignes 235-263), ajouter `global auth_blacklist_check_failed_total` + `auth_blacklist_check_failed_total = None`
- [ ] Après `set_circuit_breaker_state` (ou en fin de section helpers, vers ligne 400), ajouter le helper `record_auth_blacklist_check_failed(error_type: str)` :
  - Docstring qui explique les valeurs `{redis_timeout, redis_connection, redis_unknown}`
  - Skip si `not _INIT_OK`
  - `_safe_call(auth_blacklist_check_failed_total.labels(error_type=error_type).inc)`

### T3.3 — Modifier `is_token_blacklisted` (~10 min)

- [ ] Ouvrir `app/core/auth/jwt.py`
- [ ] Remplacer la fonction `is_token_blacklisted` lignes 102-106 par la version fail-open
- [ ] Importer `record_auth_blacklist_check_failed` en **import paresseux** dans la fonction (anti-cycle)
- [ ] Try : `await redis.exists(key) > 0` (comportement nominal)
- [ ] Except `(TimeoutError, ConnectionError)` : log warning + `record_auth_blacklist_check_failed(error_type)` (`redis_timeout` ou `redis_connection`) + `return False`
- [ ] Except `Exception` (avec `# noqa: BLE001`) : log warning avec `error_type="redis_unknown"` + métrique + `return False`
- [ ] Docstring **complète** : justifie le fail-open, cite l'alerte Prometheus, explique le compromis sécurité

### T3.4 — Tests (~20 min)

- [ ] Dans `tests/test_auth_hardening.py`, ajouter `test_blacklist_check_redis_timeout_returns_false_fail_open` :
  - Mock `get_redis()` pour retourner un fake redis dont `exists` lève `TimeoutError`
  - Asserter que `is_token_blacklisted("some-jti")` retourne `False`
- [ ] Ajouter `test_blacklist_check_redis_down_increments_metric` :
  - Reset Prometheus + `setup_prometheus(settings)`
  - Mock `get_redis` avec `ConnectionError`
  - Appeler `is_token_blacklisted`
  - Inspecter `auth_blacklist_check_failed_total.collect()` → asserter au moins 1 sample avec `error_type=redis_connection` et `value > 0`
- [ ] Ajouter `test_blacklist_check_redis_ok_returns_correct_value` :
  - Mock redis OK avec `exists=AsyncMock(return_value=1)` → asserter `True`
  - Mock redis OK avec `exists=AsyncMock(return_value=0)` → asserter `False`
- [ ] Si `tests/test_prometheus_metrics.py` contient `assert metrics_count == 13` → bumper à 14

### T3.5 — Exécution + validation (~5 min)

- [ ] `pytest tests/test_auth_hardening.py -v -k blacklist_check` → 3 tests verts
- [ ] `pytest tests/test_prometheus_metrics.py -v` → 0 régression (metrics_count=14 cohérent)
- [ ] `pytest tests/test_auth_hardening.py tests/test_auth_hardening_a3.py tests/test_password_reset.py -v` → 0 régression
- [ ] `ruff check app/core/auth/jwt.py app/core/observability/prometheus.py` → 0 warning

### T3.6 — Critères d'acceptation T3

- [ ] La métrique `nexya_auth_blacklist_check_failed_total` apparaît dans le registry Prometheus
- [ ] `is_token_blacklisted` retourne `False` (fail-open) sur `TimeoutError`, `ConnectionError`, autre `Exception`
- [ ] Chaque échec incrémente la métrique avec un `error_type` distinct
- [ ] La docstring de `is_token_blacklisted` documente explicitement le compromis fail-open
- [ ] Tests d'intégration : 3 nouveaux tests verts + 0 régression
- [ ] `metrics_count` mis à jour à 14 dans `setup_prometheus()` (ligne 211)

---

## TÂCHE 4 — Préparer `scripts/backup_db.sh` + runbook (2h, S1)

### T4.1 — Lecture des fichiers existants (~15 min)

- [ ] `Read docs/runbooks/db-restore.md` (intégralement)
- [ ] `Read docs/runbooks/deployment-l2.md` section "Backups DB" (ligne 249+)
- [ ] `Read scripts/release.sh` (pattern bash strict NEXYA)
- [ ] `Read scripts/rollback.sh` (idem, identifier `--dry-run` + traps)
- [ ] `Read scripts/smoke_test.sh` (idem, identifier la structure)
- [ ] `Read tests/test_rollback_script.py` (pattern test structure bash)
- [ ] `Read tests/test_smoke_test_script.py` (idem)
- [ ] Vérifier dans `.gitignore` que `userlist.txt` n'est pas déjà ignoré (à ajouter en T5)

### T4.2 — Créer `scripts/backup_db.sh` (~30 min)

- [ ] Créer le fichier `scripts/backup_db.sh`
- [ ] Shebang `#!/usr/bin/env bash`
- [ ] Header docstring : objectif, env vars, exit codes, idempotence
- [ ] `set -euo pipefail`
- [ ] Constantes module-level : `LOCK_FILE`, `BACKUP_DIR`, `S3_BUCKET`, `POSTGRES_CONTAINER`, `POSTGRES_DB`, `POSTGRES_USER`, `BACKUP_RETENTION_DAYS`, `S3_REGION`, `BACKUP_GPG_RECIPIENT`
- [ ] Variable `DRY_RUN` initialisée à `false`
- [ ] Variable `TIMESTAMP=$(date -u +%Y%m%d_%H%M%S)`
- [ ] Variable `DUMP_PATH="${BACKUP_DIR}/nexya-${TIMESTAMP}.dump"`
- [ ] Helpers `log()`, `log_info()`, `log_error()` (timestamp ISO + niveau)
- [ ] Argparse `--dry-run` + `-h|--help` (avec usage complet)
- [ ] Fonction `check_dependencies()` : vérifie `docker`, `aws`, optionnel `gpg`, `flock`. Exit 3 si manquant.
- [ ] Fonction `acquire_lock()` : utilise `flock` non-bloquant. Exit 1 si lock occupé.
- [ ] Fonction `do_dump()` : `pg_dump` via `docker exec`, format custom, compress 9. En dry-run, log `[DRY-RUN] pg_dump ...` sans exécuter.
- [ ] Fonction `do_sha256()` : génère le `.sha256` du dump
- [ ] Fonction `do_gpg_encrypt()` (conditionnelle) : si `BACKUP_GPG_RECIPIENT` défini, chiffre le dump
- [ ] Fonction `do_s3_upload()` : `aws s3 cp ... --sse AES256 --region $S3_REGION` pour le dump + `.sha256`. Tolère un échec S3 en mode dry-run ou si `S3_BUCKET=test-skip`.
- [ ] Fonction `do_cleanup_local()` : `find $BACKUP_DIR -name "nexya-*.dump*" -mtime +$BACKUP_RETENTION_DAYS -delete`. Pas en dry-run.
- [ ] Fonction `print_summary()` : taille dump, durée totale, statut S3
- [ ] Trap EXIT pour cleanup lock même sur erreur
- [ ] Main flow : `check_dependencies → acquire_lock → mkdir backup_dir → do_dump → do_sha256 → do_gpg_encrypt? → do_s3_upload → do_cleanup_local → print_summary`
- [ ] `chmod +x scripts/backup_db.sh` (à faire après création)

### T4.3 — Créer `scripts/restore_db.sh` (~30 min)

- [ ] Créer le fichier `scripts/restore_db.sh`
- [ ] Shebang + header docstring
- [ ] `set -euo pipefail`
- [ ] Argparse : `<dump_s3_path>` (positional, obligatoire), `--target-db <name>` (défaut `nexya_restore`), `--dry-run`, `--swap`
- [ ] Variables d'env : mêmes que `backup_db.sh`
- [ ] `check_dependencies()` : `docker`, `aws`, `psql`, `pg_restore`
- [ ] Fonction `download_dump_from_s3()` : `aws s3 cp` + vérifier `.sha256`
- [ ] Fonction `verify_sha256()` : `sha256sum -c`
- [ ] Fonction `backup_current_db()` : pg_dump pré-restore (safety net)
- [ ] Fonction `create_target_db()` : DROP + CREATE (idempotent)
- [ ] Fonction `restore_dump()` : `pg_restore -d $TARGET_DB`
- [ ] Fonction `verify_integrity()` : 4 checks SQL (count users, last_migration, FK orphelines = 0, count messages)
- [ ] Fonction `swap_databases()` (si `--swap`) : `ALTER DATABASE nexya RENAME TO nexya_old; ALTER DATABASE nexya_restore RENAME TO nexya;`. Demande confirmation interactive sauf en `--dry-run`.
- [ ] Trap EXIT pour cleanup fichiers temporaires
- [ ] `chmod +x scripts/restore_db.sh`

### T4.4 — Modifier `docs/runbooks/db-restore.md` (~10 min)

- [ ] Localiser la section "Script `scripts/backup_db.sh` (à créer V2)" lignes 17-50
- [ ] Remplacer par : "Script `scripts/backup_db.sh` (livré 2026-05-01)" avec exemples d'usage `--dry-run`, lien vers le script
- [ ] Localiser la section "Restore manuel — Cas 1" lignes 60-117
- [ ] Ajouter avant Cas 1 : nouvelle section "Restauration automatisée (livrée 2026-05-01)" avec `scripts/restore_db.sh` + 3 exemples d'usage
- [ ] Garder Cas 1 / Cas 2 / Drill quarterly comme **fallback documentaire** (procédures manuelles si le script échoue)

### T4.5 — Modifier `docs/runbooks/deployment-l2.md` (~5 min)

- [ ] Localiser la section "Backups DB" lignes 249-269
- [ ] Remplacer par version qui pointe vers `scripts/backup_db.sh` (livré) et qui mentionne `BACKUP_GPG_RECIPIENT` configurable

### T4.6 — Tests structure (~20 min)

- [ ] Créer `tests/test_backup_scripts.py`
- [ ] Imports : `shutil`, `subprocess`, `Path`, `pytest`
- [ ] Constants `ROOT`, `BACKUP`, `RESTORE`
- [ ] Fixture `bash_available` (skipif bash absent)
- [ ] Test `test_backup_script_exists`
- [ ] Test `test_backup_script_strict_bash` (cherche `set -euo pipefail`)
- [ ] Test `test_backup_script_syntax_check` (via `bash -n`)
- [ ] Test `test_backup_script_has_dry_run_flag`
- [ ] Test `test_backup_script_dry_run_executes_without_side_effects` (lance `bash backup_db.sh --dry-run` et asserter exit 0 ou 3)
- [ ] Test `test_backup_script_uses_env_vars` (cherche `BACKUP_DIR`, `S3_BUCKET`, `POSTGRES_CONTAINER`, `POSTGRES_DB`, `POSTGRES_USER`, `BACKUP_RETENTION_DAYS`)
- [ ] Test `test_backup_script_uses_pg_dump` (cherche `pg_dump`, `--format=custom`, `--compress=9`)
- [ ] Test `test_backup_script_uses_sse_aes256` (cherche `--sse AES256`)
- [ ] Test `test_backup_script_has_lock` (cherche `flock`)
- [ ] Test `test_backup_script_supports_gpg` (cherche `BACKUP_GPG_RECIPIENT`, `gpg`)
- [ ] Test `test_restore_script_exists`
- [ ] Test `test_restore_script_strict_bash`
- [ ] Test `test_restore_script_syntax_check`
- [ ] Test `test_restore_script_validates_post_restore` (cherche `count`, `users`, `alembic_version`)
- [ ] Test `test_restore_script_supports_swap` (cherche `--swap`, `ALTER DATABASE`)
- [ ] Test `test_restore_script_supports_dry_run`

### T4.7 — Test manuel local (optionnel mais recommandé) (~5 min)

- [ ] `bash scripts/backup_db.sh --dry-run` → exit 0 ou 3 (deps), affiche `[DRY-RUN]` lines
- [ ] (Si Docker dev tourne) `bash scripts/backup_db.sh` avec `BACKUP_DIR=/tmp/backup-test S3_BUCKET=test-skip` → dump local créé
- [ ] Vérifier `ls /tmp/backup-test/` → fichier `nexya-YYYYMMDD_HHMMSS.dump` + `.sha256` présents
- [ ] `bash -n scripts/backup_db.sh && bash -n scripts/restore_db.sh` → 0 erreur

### T4.8 — Exécution + validation (~5 min)

- [ ] `pytest tests/test_backup_scripts.py -v` → ~14 tests verts
- [ ] `pytest tests/ -p no:warnings -q` → suite globale 0 régression
- [ ] `ruff check tests/test_backup_scripts.py` → 0 warning

### T4.9 — Critères d'acceptation T4

- [ ] `scripts/backup_db.sh` créé, exécutable, strict bash, `--dry-run`, flock, gpg optionnel, sse aes256
- [ ] `scripts/restore_db.sh` créé, exécutable, strict bash, `--dry-run`, `--swap`, vérifs intégrité 4 checks
- [ ] `tests/test_backup_scripts.py` créé avec ~14 tests structurels verts
- [ ] `docs/runbooks/db-restore.md` à jour, pointe vers les scripts réels
- [ ] `docs/runbooks/deployment-l2.md` section backup à jour
- [ ] `bash -n` syntax check OK sur les 2 scripts
- [ ] **AUCUNE régression** sur la suite globale 1583 tests

---

## TÂCHE 5 — Préparer `docker/pgbouncer/` config + runbook (2h, S0)

### T5.1 — Lecture des fichiers existants (~15 min)

- [ ] `Read app/core/database/postgres.py` (intégralement, 73 lignes)
- [ ] `Read app/config.py` section "Pool DB" lignes 596-610
- [ ] `Read docker/docker-compose.yml` (intégralement, identifier le pattern services + healthcheck)
- [ ] `Read docs/runbooks/deployment-l2.md` section "Architecture cible L2" + "Secrets"
- [ ] `Read .env.example` section database
- [ ] Vérifier `.gitignore` racine — ajouter `docker/pgbouncer/userlist.txt` si pas déjà ignoré

### T5.2 — Créer `docker/pgbouncer/pgbouncer.ini` (~20 min)

- [ ] Créer le dossier `docker/pgbouncer/`
- [ ] Créer `docker/pgbouncer/pgbouncer.ini`
- [ ] Header commenté : explique transaction mode, limitations, lien doc officielle
- [ ] Section `[databases]` : entrée `nexya = host=postgres port=5432 dbname=nexya pool_size=20 reserve_pool_size=5`
- [ ] Section `[pgbouncer]` :
  - `listen_addr = 0.0.0.0`
  - `listen_port = 6432`
  - `auth_type = scram-sha-256`
  - `auth_file = /etc/pgbouncer/userlist.txt`
  - `auth_user = nexya`
  - `pool_mode = transaction`
  - `max_client_conn = 10000`
  - `default_pool_size = 20`
  - `reserve_pool_size = 5`
  - `reserve_pool_timeout = 5`
  - `server_idle_timeout = 600`
  - `server_lifetime = 3600`
  - `query_wait_timeout = 30`
  - `client_idle_timeout = 0`
  - `server_reset_query = DISCARD ALL`
  - `log_connections = 0`
  - `log_disconnections = 0`
  - `log_pooler_errors = 1`
  - `log_stats = 1`
  - `stats_period = 60`
  - `admin_users = nexya`
  - `stats_users = nexya`
  - Commentaires sur le bloc TLS (désactivé par défaut, à activer L2 prod)

### T5.3 — Créer `docker/pgbouncer/userlist.txt.example` (~5 min)

- [ ] Créer `docker/pgbouncer/userlist.txt.example`
- [ ] Header : explique le format, la procédure de génération du hash SCRAM-SHA-256 en 5 étapes
- [ ] Warning sécurité : ne pas commit le vrai `userlist.txt`
- [ ] Ligne template : `"nexya" "SCRAM-SHA-256$4096:REPLACE_AVEC_LE_VRAI_HASH"`
- [ ] Ajouter `docker/pgbouncer/userlist.txt` au `.gitignore` (pas le `.example`)

### T5.4 — Créer `docker/docker-compose.pgbouncer.yml` (~10 min)

- [ ] Créer `docker/docker-compose.pgbouncer.yml`
- [ ] Header commenté : usage `docker compose -f ... -f docker-compose.pgbouncer.yml`
- [ ] Service `pgbouncer` :
  - Image pinned `bitnami/pgbouncer:1.23.1`
  - `container_name: nexya-pgbouncer`
  - Port `6433:6432` (host:container, 6433 pour cohérence postgres natif vs docker)
  - Volumes mounted `:ro` : `pgbouncer.ini` + `userlist.txt`
  - `depends_on: postgres { condition: service_healthy }`
  - Healthcheck `pg_isready -h 127.0.0.1 -p 6432 -U nexya`
  - `restart: unless-stopped`

### T5.5 — Modifier `app/config.py` (~10 min)

- [ ] Ouvrir `app/config.py`, localiser la section "Pool DB" lignes 596-610
- [ ] Ajouter avant les `db_pool_size` un long commentaire bloc qui explique le mode direct vs PgBouncer
- [ ] Après `db_max_overflow` ligne 605, ajouter le setting `database_use_pgbouncer: bool = False` avec docstring complète
- [ ] Vérifier qu'aucun autre code ne consomme déjà cette variable (grep `database_use_pgbouncer`)

### T5.6 — Modifier `app/core/database/postgres.py` (~15 min)

- [ ] Ouvrir `app/core/database/postgres.py`
- [ ] Mettre à jour la docstring du module : documenter les 2 modes (direct / PgBouncer transaction)
- [ ] Créer la fonction `_build_engine_kwargs()` qui retourne les bons kwargs selon `settings.database_use_pgbouncer` :
  - Mode PgBouncer : `pool_pre_ping=False`, `pool_recycle=300`, `connect_args={"connect_timeout": 5, "prepare_threshold": None}`
  - Mode direct : `pool_pre_ping=True`, `pool_recycle=3600`, `connect_args={"connect_timeout": 5}`
- [ ] Modifier l'init de `engine` : `engine = create_async_engine(settings.database_url, **_build_engine_kwargs())`
- [ ] Ajouter un `log.info("database.engine_initialized", use_pgbouncer=..., pool_size=..., max_overflow=...)` au boot pour visibilité
- [ ] Vérifier que `check_db_connection`, `dispose_engine`, `get_db` fonctionnent inchangés

### T5.7 — Modifier `.env.example` (~5 min)

- [ ] Ouvrir `.env.example`, localiser section database
- [ ] Ajouter `DATABASE_USE_PGBOUNCER=false` avec commentaire de quand activer
- [ ] Ajouter (en commentaire) la version `DATABASE_URL` pour L2 staging avec PgBouncer port 6432

### T5.8 — Modifier `docs/runbooks/deployment-l2.md` (~15 min)

- [ ] Ouvrir `docs/runbooks/deployment-l2.md`
- [ ] Ajouter une nouvelle section "PgBouncer (cap connexions à 9M users)" après "Architecture cible L2"
- [ ] Sous-sections : Pourquoi (justification scaling), Stack (diagramme uvicorn → PgBouncer → Postgres), Config livrée (liens vers les fichiers), Activation L2 staging (5 étapes), Limitations connues (LISTEN/NOTIFY, prepared statements, sessions courtes), Monitoring (métriques V2)

### T5.9 — Tests structure (~20 min)

- [ ] Créer `tests/test_pgbouncer_config.py`
- [ ] Imports : `re`, `Path`, `pytest`, `yaml`
- [ ] Constants `ROOT`, `PGBOUNCER_INI`, `USERLIST_EXAMPLE`, `COMPOSE_OVERLAY`
- [ ] Test `test_pgbouncer_ini_exists`
- [ ] Test `test_pgbouncer_ini_uses_transaction_mode` (regex `^pool_mode\s*=\s*transaction`)
- [ ] Test `test_pgbouncer_ini_uses_scram_sha256` (regex `^auth_type\s*=\s*scram-sha-256`)
- [ ] Test `test_pgbouncer_ini_uses_discard_all_reset` (cherche `DISCARD ALL`)
- [ ] Test `test_pgbouncer_ini_listens_on_6432` (regex `^listen_port\s*=\s*6432`)
- [ ] Test `test_pgbouncer_ini_max_client_conn_reasonable` (extract entier, asserter 1000-100k)
- [ ] Test `test_userlist_example_exists`
- [ ] Test `test_userlist_example_documents_scram_generation` (cherche `SCRAM-SHA-256` + procédure)
- [ ] Test `test_userlist_example_no_real_credentials` (cherche `REPLACE`/`EXAMPLE`/`PLACEHOLDER`)
- [ ] Test `test_compose_overlay_exists`
- [ ] Test `test_compose_overlay_is_valid_yaml` (yaml.safe_load + asserter `services.pgbouncer`)
- [ ] Test `test_compose_overlay_pinned_image` (asserter `:` présent et tag != `latest/main/master`)
- [ ] Test `test_compose_overlay_mounts_config_readonly` (asserter `:ro` sur les 2 mounts)
- [ ] Test `test_postgres_engine_supports_pgbouncer_mode` (lit le source `postgres.py`, cherche `database_use_pgbouncer`, `pool_pre_ping`, `prepare_threshold`)
- [ ] Test `test_settings_has_pgbouncer_flag` (importe `Settings`, asserter défaut `False`)
- [ ] Test `test_deployment_l2_runbook_documents_pgbouncer` (lit `deployment-l2.md`, cherche `PgBouncer`, `transaction mode`, `userlist.txt`)

### T5.10 — Test manuel local (optionnel) (~10 min)

- [ ] `docker compose -f docker/docker-compose.yml -f docker/docker-compose.pgbouncer.yml config` → YAML mergé valide
- [ ] (Optionnel) `docker compose ... up -d pgbouncer` → service démarre
- [ ] (Optionnel) `docker compose ... ps` → STATUS healthy
- [ ] (Optionnel) Cleanup `docker compose ... down`

### T5.11 — Exécution + validation (~5 min)

- [ ] `pytest tests/test_pgbouncer_config.py -v` → ~16 tests verts
- [ ] `pytest tests/ -p no:warnings -q` → suite globale 0 régression
- [ ] `ruff check tests/test_pgbouncer_config.py app/core/database/postgres.py app/config.py` → 0 warning

### T5.12 — Critères d'acceptation T5

- [ ] `docker/pgbouncer/pgbouncer.ini` créé, transaction mode, SCRAM, max_client_conn=10000
- [ ] `docker/pgbouncer/userlist.txt.example` créé avec procédure génération hash
- [ ] `docker/pgbouncer/userlist.txt` ajouté au `.gitignore`
- [ ] `docker/docker-compose.pgbouncer.yml` créé, image pinned, mounts read-only
- [ ] `app/config.py` ajoute `database_use_pgbouncer: bool = False`
- [ ] `app/core/database/postgres.py` adapte les kwargs selon le flag
- [ ] `.env.example` documenté
- [ ] `docs/runbooks/deployment-l2.md` enrichi
- [ ] `tests/test_pgbouncer_config.py` créé avec ~16 tests verts
- [ ] **AUCUNE régression** sur la suite globale

---

## PHASE FINALE — Validation + commit + docs (~30 min)

### F.1 — Suite complète

- [ ] `pytest tests/ -p no:warnings -q | tail -10`
- [ ] Compter le delta : avant ~1583 verts, après ~1608 verts (T1=2 + T2=2 + T3=3 + T4=14 + T5=16 = 37 nouveaux ; certains tests existants peuvent être étendus)
- [ ] **0 failed, 0 errors** sinon stop + fix

### F.2 — Lint + format

- [ ] `ruff check app/ tests/ scripts/` → 0 erreur
- [ ] `ruff format --check app/ tests/` → 0 fichier à reformater
- [ ] `bash -n scripts/backup_db.sh scripts/restore_db.sh` → 0 erreur
- [ ] (Optionnel) `mypy app/` → comportement inchangé (le projet est en `ignore_errors=true`)

### F.3 — Vérifications structurelles

- [ ] `grep -n "database_use_pgbouncer" app/config.py app/core/database/postgres.py` → 2 fichiers, valeurs cohérentes
- [ ] `grep -n "rate_limit_refresh" app/core/security/rate_limiter.py app/features/auth/router.py` → définition + appel
- [ ] `grep -c "max_tokens=" app/ai/experts.py` → ≥ 11 occurrences (1 par expert, hors le `: int | None = None` du dataclass field)
- [ ] `grep -n "auth_blacklist_check_failed_total" app/core/observability/prometheus.py app/core/auth/jwt.py` → définition + appel
- [ ] `ls scripts/backup_db.sh scripts/restore_db.sh docker/pgbouncer/pgbouncer.ini docker/pgbouncer/userlist.txt.example docker/docker-compose.pgbouncer.yml` → 5 fichiers présents
- [ ] `cat .gitignore | grep "userlist.txt"` → présent (sauf le `.example`)

### F.4 — Mise à jour des docs (BLOQUANTE)

- [ ] **`CLAUDE.md` §15** — ajouter une nouvelle entrée `| 2026-05-01 | Audit remediation — 5 tâches priorisées (3 fixes code + 2 préparations infra L2 staging) | ...` détaillant tous les fichiers impactés
- [ ] **`CLAUDE.md` §7** — vérifier qu'aucune ligne ne devient incorrecte (ex: si une ligne disait « 13 métriques NEXYA » il faut bumper à 14)
- [ ] **`docs/ROADMAP.md`** — marquer dans la section "Audit findings post 2026-05-01" : T1, T2, T3 fermés ; T4, T5 marqués "préparés (déploiement L2)"
- [ ] **`docs/BACKEND_SESSIONS_PLAN.md`** — ajouter l'entrée session "Audit Remediation 2026-05-01" dans le bloc le plus pertinent
- [ ] **`COURS_NEXYA_BACKEND.md`** — ajouter une section pédagogique 5 sous-sections (rate limit refresh, max_tokens cap, fail-open + métrique, PgBouncer transaction mode, pg_dump custom + sse)

### F.5 — Commit final

- [ ] `git status` → vérifier la liste des fichiers modifiés/créés
- [ ] `git diff --stat` → vérifier le nombre de lignes (~500-1000 ajoutées attendu)
- [ ] **AUCUN fichier non listé** dans le PROMPT (pas de prolifération)
- [ ] **AUCUN `Co-Authored-By: Claude`**
- [ ] **AUCUN préfixe Conventional Commits** (`chore:`, `feat:`, etc.)
- [ ] Créer le commit avec un message ~10 lignes (cf. PROMPT §8.4)
- [ ] `git log -1 --stat` → vérifier le commit propre

### F.6 — Annonce finale

- [ ] Composer le message d'annonce respectant `feedback_docs_update.md` (cf. PROMPT §8.5)
- [ ] Inclure : récap 5 tâches, delta tests, docs à jour, prochaine étape (DPO consultant)

---

## Validation acceptance globale

Cocher l'ensemble :

- [ ] **T1** : `/auth/refresh` rate limit IP 20/min implémenté + 2 tests verts
- [ ] **T2** : 11 experts ont `max_tokens` explicite + 2 tests verts
- [ ] **T3** : Blacklist JWT fail-open + métrique Prometheus 14ᵉ + 3 tests verts
- [ ] **T4** : `scripts/backup_db.sh` + `restore_db.sh` + 14 tests structure
- [ ] **T5** : `docker/pgbouncer/` config + overlay docker-compose + 16 tests structure
- [ ] **Validation** : ~1608 tests verts, 0 régression, lint OK, docs à jour, commit propre
- [ ] **Discipline NEXYA** : règle A (prompt si perfectible), règle E (prompt anglais avant exécution), règle H (pédagogie après chaque module), `feedback_docs_update.md`, `feedback_french_quality.md`, `feedback_git_commits.md`

---

## Métriques de succès

| Métrique | Avant | Cible | Mesure |
|---|---|---|---|
| Tests verts | ~1583 | ~1608 (+25) | `pytest tests/ -p no:warnings -q` |
| Endpoints publics rate-limités | 4/5 | 5/5 (+1 refresh) | grep `rate_limit_*` dans `auth/router.py` |
| Experts avec `max_tokens` cap | 0/11 | 11/11 | grep `max_tokens=` dans `experts.py` |
| Métriques Prometheus NEXYA | 13 | 14 (+blacklist failed) | `metrics_count` dans prometheus.py |
| Scripts backup automation | 0 | 2 (`backup_db.sh`, `restore_db.sh`) | `ls scripts/*.sh` |
| Stack PgBouncer prête | 0 | 1 overlay docker-compose + ini + userlist.example | `ls docker/pgbouncer/` + `docker compose config` valide |
| S0 audit ouverts | 3 | 1 (PgBouncer) ou 0 si test L2 staging réussit | Top 10 audit |
| S1 audit ouverts | 7 | 4 (max_tokens cap, blacklist, refresh fail-open déchargés) | idem |

---

## Métriques de risque (à monitorer après remédiation)

| Risque résiduel | Mitigation aujourd'hui | Reste à faire (Phase 14+) |
|---|---|---|
| Refresh leaké brute-forcé | Rate limit 20/min/IP | Lockout par compte (M, 3j) |
| Output runaway facture | Cap `max_tokens` par expert | Monitoring drift modèle Gemini |
| Redis blacklist down | Fail-open + métrique alert | Fallback DB blacklist L3 |
| Saturation Postgres 1M users | PgBouncer config prête | Activation effective L2 staging + load test k6 |
| Perte données catastrophique | Scripts backup + runbook | Cron déployé Hetzner + drill quarterly |

---

*Fin de la TODO. Cocher au fur et à mesure. À la fin, valider l'acceptance globale puis commit.*
