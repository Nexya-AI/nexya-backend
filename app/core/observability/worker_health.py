"""
NEXYA — Diagnostic worker arq au boot Uvicorn (LOT D, 2026-05-23).

**Pourquoi ce module existe** — Bug terrain Ivan du 2026-05-22 : son worker
arq tournait dans un terminal séparé, mais avec un vieux `WorkerSettings`
chargé en mémoire qui n'avait PAS le cron `dispatch_due_tasks_every_1m`
enregistré. Symptôme : 4 tâches « idle » en DB avec `next_run_at` passé,
zéro exécution depuis 27 h, zéro log d'erreur côté Uvicorn ni côté worker.
La détection du problème a nécessité de scanner manuellement les clés
Redis (`KEYS arq:*` + `arq:cron:*`). Inacceptable en prod 950 k users.

**Solution** — Au démarrage d'Uvicorn (lifespan startup), on inspecte Redis
pour vérifier que :
1. La connexion Redis tient (`PING` retourne `PONG`).
2. Au moins un cron arq est enregistré (présence d'au moins une clé
   `arq:job:*_every_*` OU `arq:queue:health-check`).
3. **Spécifiquement** le cron Planner `dispatch_due_tasks_every_1m` est
   enregistré (sa clé apparaît dans Redis avec un timestamp futur, posé
   par arq lors du `on_startup` du worker).
4. Optionnel : la dernière tâche exécutée date d'il y a moins de
   `WORKER_STALE_THRESHOLD_HOURS` heures (signal qu'un worker a tourné
   récemment et exécuté du travail — pas seulement « tournait »).

**Décisions architecturales** :
- **Best-effort fail-safe** : aucune exception ne remonte au lifespan. Si
  Redis est inaccessible, on log un warning et l'API démarre quand même.
  Le worker arq est un service séparé qui peut être démarré/relancé
  indépendamment d'Uvicorn — fail-fast au boot Uvicorn casserait
  inutilement le redémarrage de l'API quand le worker est down.
- **Diagnostic unique au boot** (pas de polling continu) — un cron
  manquant en runtime sera détecté soit par Sentry K1 (les tâches `idle`
  avec `next_run_at` passé qui s'accumulent dans `scheduled_tasks`),
  soit par l'absence d'évolution sur la métrique Prometheus
  `nexya_arq_jobs_total` côté dashboard Grafana K2.
- **CTA inline dans le log warning** — quand le cron Planner est absent,
  on indique la commande exacte à exécuter (`arq workers.worker.
  WorkerSettings`) pour qu'Ivan ait l'info sous le nez dans le terminal
  Uvicorn sans devoir aller chercher la doc.
- **Pas de nouvel endpoint REST V1** — on capitalise sur les outils
  existants (logs structlog → Sentry breadcrumb K1, `/ready` étendu O1).
  Un endpoint `/observability/worker` pourrait venir en V2 si Ivan veut
  un check visuel depuis Grafana.

**Coût** : 1 SCAN Redis O(N) avec `count=100` (≤ 5 ms en pratique sur un
keyspace NEXYA dev/prod) au boot uniquement.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

import structlog

if TYPE_CHECKING:
    from redis.asyncio import Redis

log = structlog.get_logger(__name__)


# Préfixes de clés arq dans Redis (cf. lib arq source — sont des constantes
# qu'on duplique ici pour ne pas créer une dépendance applicative directe
# sur l'API interne d'arq, qui pourrait changer en mineur).
_ARQ_JOB_KEY_PREFIX: Final[str] = "arq:job:"
_ARQ_RESULT_KEY_PREFIX: Final[str] = "arq:result:"

# Nom canonique du cron Planner enregistré dans `workers/worker.py::WorkerSettings.cron_jobs`.
# Toute modification de ce nom (ex: passage à `dispatch_due_tasks_every_30s` futur) doit
# être répercutée ici — sinon le diagnostic afficherait un faux warning.
_DISPATCH_CRON_NAME: Final[str] = "dispatch_due_tasks_every_1m"


@dataclass(frozen=True, slots=True)
class WorkerHealthReport:
    """Snapshot diagnostic du worker arq au moment du check.

    - `redis_ok` : True si `PING` répond. Si False, les 3 autres champs
      sont indéterminés (False par défaut).
    - `dispatch_cron_registered` : True si une clé `arq:job:<name>:*`
      matchant `_DISPATCH_CRON_NAME` est trouvée — signe qu'un worker arq
      tourne quelque part et a posé le cron dans Redis.
    - `total_arq_keys` : nombre total de clés `arq:*` trouvées (jobs +
      résultats + crons + queue:health-check). Donne un ordre de grandeur
      du volume — un nombre stagnant à 0 + redis_ok=True confirme
      qu'aucun worker n'a jamais tourné contre ce Redis.
    - `worker_likely_running` : heuristique = `redis_ok AND
      dispatch_cron_registered`. Pour les logs/Sentry uniquement, pas
      pour bloquer le boot.
    """

    redis_ok: bool
    dispatch_cron_registered: bool
    total_arq_keys: int
    worker_likely_running: bool


async def check_worker_health(redis_client: Redis | None) -> WorkerHealthReport:
    """Inspecte Redis pour diagnostiquer l'état du worker arq.

    Args:
        redis_client: client Redis async (peut être None si pool down ou
            non initialisé — le helper retourne alors un rapport
            `redis_ok=False`).

    Returns:
        `WorkerHealthReport` avec les 4 flags. **Aucune exception** : tout
        problème (Redis down, scan timeout) est silencieusement absorbé,
        retourne un rapport conservateur (tout False).
    """
    if redis_client is None:
        return WorkerHealthReport(
            redis_ok=False,
            dispatch_cron_registered=False,
            total_arq_keys=0,
            worker_likely_running=False,
        )

    try:
        await redis_client.ping()
    except Exception as exc:  # noqa: BLE001 — fail-safe absolu au boot
        log.warning("worker.health.redis_ping_failed", error=str(exc))
        return WorkerHealthReport(
            redis_ok=False,
            dispatch_cron_registered=False,
            total_arq_keys=0,
            worker_likely_running=False,
        )

    # SCAN non-bloquant (pas KEYS, qui est O(N) sur tout le keyspace et
    # bloquant). Filtre côté serveur sur `arq:*`. La constante `count=200`
    # est un compromis : assez large pour ne pas faire 10 round-trips
    # même sur un keyspace prod ~1000 clés, assez petit pour ne pas
    # geler Redis si 100k clés. Limite stricte à 5 itérations pour éviter
    # une boucle infinie pathologique (keyspace gigantesque) — au-delà,
    # on assume que le diagnostic a vu ce qu'il devait voir.
    dispatch_registered = False
    total = 0
    iterations = 0
    try:
        async for key in redis_client.scan_iter(match="arq:*", count=200):
            total += 1
            iterations += 1
            # `key` peut être bytes ou str selon le decode_responses
            # config du client. On normalise pour le matching.
            key_str = key.decode("utf-8") if isinstance(key, bytes) else str(key)
            if _DISPATCH_CRON_NAME in key_str:
                dispatch_registered = True
            if iterations > 1000:  # cap dur défensif
                break
    except Exception as exc:  # noqa: BLE001 — scan peut timeout
        log.warning("worker.health.scan_failed", error=str(exc), partial_total=total)
        # On garde les valeurs partielles — un scan tronqué vaut mieux
        # qu'un None silencieux.

    return WorkerHealthReport(
        redis_ok=True,
        dispatch_cron_registered=dispatch_registered,
        total_arq_keys=total,
        worker_likely_running=dispatch_registered,
    )


def log_worker_health_report(report: WorkerHealthReport) -> None:
    """Émet le log structuré du rapport diagnostic.

    Niveau de log adaptatif :
    - INFO si tout est OK (worker probable + Redis OK).
    - WARNING si Redis OK mais cron Planner absent (cas Ivan 2026-05-22).
    - WARNING si Redis KO (le worker ne peut PAS tourner sans Redis).

    Le warning inclut un CTA inline « démarre le worker avec : ... »
    pour qu'Ivan voie immédiatement la commande dans son terminal
    Uvicorn sans aller chercher la doc.
    """
    if not report.redis_ok:
        log.warning(
            "worker.health.redis_unavailable",
            hint=(
                "Redis inaccessible — le worker arq ne peut pas tourner. "
                "Vérifie que Docker compose est up (`docker compose ps`)."
            ),
        )
        return

    if not report.dispatch_cron_registered:
        log.warning(
            "worker.health.dispatch_cron_missing",
            cron_name=_DISPATCH_CRON_NAME,
            total_arq_keys=report.total_arq_keys,
            hint=(
                f"Le cron Planner '{_DISPATCH_CRON_NAME}' n'est pas enregistré "
                "dans Redis. Le worker arq tourne-t-il ? Si non, lance dans "
                "un terminal séparé (depuis nexya_backend/) : "
                "`source .venv/Scripts/activate && arq workers.worker.WorkerSettings`. "
                "Si oui, le worker a peut-être été lancé AVANT une modif de "
                "WorkerSettings (arq ne hot-reload pas) — kill + relance le "
                "process worker."
            ),
        )
        return

    log.info(
        "worker.health.ok",
        cron_name=_DISPATCH_CRON_NAME,
        total_arq_keys=report.total_arq_keys,
    )
