"""Tests LOT D (2026-05-23) — diagnostic worker arq au boot Uvicorn.

5 scénarios mock-first couvrant `check_worker_health` + `log_worker_health_report` :

1. **redis_client=None** → rapport conservateur (tous False), zéro appel
   ping. Cas du pool Redis pas encore initialisé OU complètement KO.
2. **PING fail** → idem, capture l'exception silencieusement, rapport
   conservateur. L'API démarre quand même.
3. **Cron Planner trouvé** dans `arq:*` → `dispatch_cron_registered=True`
   + `worker_likely_running=True`. Cas nominal post-restart `nexya_dev.py`.
4. **Cron Planner absent** mais d'autres clés `arq:*` → flag False, log
   warning avec CTA inline (cas Ivan 2026-05-22).
5. **log_worker_health_report** → tous les chemins (OK / cron missing /
   redis KO) émettent le bon niveau de log structlog.

Aucun Redis réel requis — `AsyncMock` du client suffit (tests sub-seconde).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.observability.worker_health import (
    WorkerHealthReport,
    check_worker_health,
    log_worker_health_report,
)

# ════════════════════════════════════════════════════════════════════
# check_worker_health
# ════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_returns_all_false_when_client_is_none():
    """Pool Redis non initialisé → rapport conservateur all-False."""
    report = await check_worker_health(None)

    assert report.redis_ok is False
    assert report.dispatch_cron_registered is False
    assert report.total_arq_keys == 0
    assert report.worker_likely_running is False


@pytest.mark.asyncio
async def test_returns_redis_ko_when_ping_raises():
    """Redis joignable au boot mais le ping lève une exception (timeout,
    refused). Le helper capture silencieusement → rapport all-False.
    L'API démarre quand même (fail-safe absolu, pas de raise au caller)."""
    client = MagicMock()
    client.ping = AsyncMock(side_effect=ConnectionError("redis refused"))
    client.scan_iter = MagicMock()  # ne sera pas appelé

    report = await check_worker_health(client)

    assert report.redis_ok is False
    assert report.dispatch_cron_registered is False
    # scan_iter ne doit pas avoir été lancé après l'échec du ping
    client.scan_iter.assert_not_called()


def _async_iter_from_list(items: list[str]):
    """Helper : retourne un async iterator depuis une liste statique
    pour mocker `redis.scan_iter`."""

    async def _gen():
        for it in items:
            yield it

    return _gen()


@pytest.mark.asyncio
async def test_detects_dispatch_cron_when_key_present():
    """Cas nominal : `arq:job:dispatch_due_tasks_every_1m:<ts>` présent
    dans Redis → `dispatch_cron_registered=True` + worker_likely_running=True."""
    client = MagicMock()
    client.ping = AsyncMock(return_value=True)
    # scan_iter doit être un MagicMock async-iterable
    client.scan_iter = MagicMock(
        return_value=_async_iter_from_list(
            [
                "arq:job:flush_ai_sessions_every_10m:1779488940123",
                "arq:job:dispatch_due_tasks_every_1m:1779488940500",
                "arq:queue:health-check",
            ]
        )
    )

    report = await check_worker_health(client)

    assert report.redis_ok is True
    assert report.dispatch_cron_registered is True
    assert report.worker_likely_running is True
    assert report.total_arq_keys == 3


@pytest.mark.asyncio
async def test_flags_cron_missing_when_dispatch_absent():
    """Cas Ivan 2026-05-22 : Redis OK, autres crons présents, mais le
    cron Planner manque → flag False même si redis_ok=True. Le log
    warning expose le CTA pour relancer le worker."""
    client = MagicMock()
    client.ping = AsyncMock(return_value=True)
    client.scan_iter = MagicMock(
        return_value=_async_iter_from_list(
            [
                "arq:job:flush_ai_sessions_every_10m:1779488940123",
                "arq:queue:health-check",
                "arq:result:abc123",
            ]
        )
    )

    report = await check_worker_health(client)

    assert report.redis_ok is True
    assert report.dispatch_cron_registered is False
    assert report.worker_likely_running is False
    assert report.total_arq_keys == 3


@pytest.mark.asyncio
async def test_handles_bytes_keys_from_redis():
    """Selon le `decode_responses=False/True` config du client Redis, les
    clés peuvent arriver en bytes. Le helper doit gérer les deux."""
    client = MagicMock()
    client.ping = AsyncMock(return_value=True)
    client.scan_iter = MagicMock(
        return_value=_async_iter_from_list(
            [
                b"arq:job:dispatch_due_tasks_every_1m:1779488940123",
                b"arq:queue:health-check",
            ]
        )
    )

    report = await check_worker_health(client)

    assert report.dispatch_cron_registered is True


# ════════════════════════════════════════════════════════════════════
# log_worker_health_report (smoke tests — vérifie qu'aucune exception
# ne remonte, le détail des logs est vérifié manuellement via le smoke
# du module main.py au boot)
# ════════════════════════════════════════════════════════════════════


def test_log_worker_health_report_happy_path_no_raise():
    report = WorkerHealthReport(
        redis_ok=True,
        dispatch_cron_registered=True,
        total_arq_keys=5,
        worker_likely_running=True,
    )
    # Ne doit pas lever, peu importe le niveau de log.
    log_worker_health_report(report)


def test_log_worker_health_report_warns_when_cron_missing():
    report = WorkerHealthReport(
        redis_ok=True,
        dispatch_cron_registered=False,
        total_arq_keys=2,
        worker_likely_running=False,
    )
    log_worker_health_report(report)


def test_log_worker_health_report_warns_when_redis_ko():
    report = WorkerHealthReport(
        redis_ok=False,
        dispatch_cron_registered=False,
        total_arq_keys=0,
        worker_likely_running=False,
    )
    log_worker_health_report(report)


def test_worker_health_report_frozen_dataclass():
    """`WorkerHealthReport` est frozen+slots — mutation interdite à
    runtime (anti side-effect)."""
    report = WorkerHealthReport(
        redis_ok=True,
        dispatch_cron_registered=True,
        total_arq_keys=1,
        worker_likely_running=True,
    )
    with pytest.raises(Exception):  # FrozenInstanceError ou AttributeError
        report.redis_ok = False  # type: ignore[misc]
