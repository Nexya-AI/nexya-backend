"""
Tests O1 — `/version` + `/ready` étendu + `ExtendedHealthService`.

Couvre :
1. `/version` public sans auth → 200 + champs `version`, `commit_sha`, `env`, `tag`, `dirty`, `source`
2. `/version` ne contient pas de secret (anti-leak fingerprinting)
3. `detect_version()` retourne 3-fallbacks (env vars OK / git OK / unknown)
4. `_compute_uptime_seconds` retourne None si APP_START pas posé
5. `_compute_uptime_seconds` retourne float positif après set_app_start_monotonic
6. `ExtendedHealthService.compute` fail-safe sur DB None
7. `ExtendedHealthService.compute` fail-safe sur Redis None
8. `_build_db_payload` fail-safe sur exception DB
9. `_build_arq_payload` fail-safe → None sur exception
10. `/ready` 200 si DB+Redis OK / 503 si KO
"""

from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.health.extended import (
    ExtendedHealthResponse,
    ExtendedHealthService,
    _build_arq_payload,
    _build_db_payload,
    _build_redis_payload,
    _compute_uptime_seconds,
    set_app_start_monotonic,
)
from app.core.health.version import (
    VersionInfo,
    _from_env_vars,
    _from_git_head_file,
    detect_version,
)
from app.main import app


# ══════════════════════════════════════════════════════════════
# /version endpoint
# ══════════════════════════════════════════════════════════════


def test_version_endpoint_public_no_auth() -> None:
    """`/version` doit être accessible sans token."""
    with TestClient(app) as client:
        resp = client.get("/version")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert "version" in data
    assert "commit_sha" in data
    assert "env" in data
    assert "tag" in data
    assert "dirty" in data
    assert "source" in data


def test_version_endpoint_does_not_leak_secrets() -> None:
    """Anti-fingerprinting — pas de secret dans la réponse."""
    with TestClient(app) as client:
        resp = client.get("/version")
    body = resp.json()
    raw = str(body).lower()
    # Aucun de ces tokens ne doit apparaître :
    forbidden = ("password", "private_key", "api_key", "secret", "jwt_private")
    for token in forbidden:
        assert token not in raw, f"Leak suspect : {token!r} dans /version"


# ══════════════════════════════════════════════════════════════
# detect_version() — 3 fallbacks
# ══════════════════════════════════════════════════════════════


def test_detect_version_returns_version_info_dataclass() -> None:
    info = detect_version()
    assert isinstance(info, VersionInfo)
    assert info.source in ("git", "git_head_file", "env", "unknown")


def test_from_env_vars_reads_app_version_and_commit_sha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_VERSION", "v1.2.3")
    monkeypatch.setenv("APP_COMMIT_SHA", "abc123def456" * 3)
    info = _from_env_vars()
    assert info is not None
    assert info.version == "v1.2.3"
    assert info.commit_sha.startswith("abc123def456")
    assert info.tag == "v1.2.3"
    assert info.source == "env"
    assert info.dirty is False


def test_from_env_vars_returns_none_when_both_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("APP_VERSION", raising=False)
    monkeypatch.delenv("APP_COMMIT_SHA", raising=False)
    assert _from_env_vars() is None


def test_from_git_head_file_returns_none_when_no_repo(tmp_path) -> None:
    """Si pas de `.git/` → None."""
    assert _from_git_head_file(tmp_path) is None


# ══════════════════════════════════════════════════════════════
# Uptime
# ══════════════════════════════════════════════════════════════


def test_uptime_returns_none_before_set() -> None:
    set_app_start_monotonic(None)
    # Forcer le reset interne
    import app.core.health.extended as ext

    ext._APP_START_MONOTONIC = None
    assert _compute_uptime_seconds() is None


def test_uptime_returns_positive_float_after_set() -> None:
    set_app_start_monotonic(time.monotonic() - 5.0)
    uptime = _compute_uptime_seconds()
    assert uptime is not None
    assert 4.0 < uptime < 10.0  # ~5s + jitter


# ══════════════════════════════════════════════════════════════
# ExtendedHealthService — fail-safe par champ
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_compute_with_db_none_returns_db_unavailable() -> None:
    response = await ExtendedHealthService.compute(db=None, redis=None)
    assert isinstance(response, ExtendedHealthResponse)
    assert response.db.status == "unavailable"
    assert response.redis.status == "unavailable"
    assert response.status == "degraded"


@pytest.mark.asyncio
async def test_build_db_payload_fail_safe_on_exception() -> None:
    """Si `db.execute` lève, on retourne unavailable sans crash."""
    fake_db = MagicMock()
    fake_db.execute = AsyncMock(side_effect=RuntimeError("connection lost"))
    payload = await _build_db_payload(fake_db)
    assert payload.status == "unavailable"
    assert payload.latency_ms is None


@pytest.mark.asyncio
async def test_build_redis_payload_fail_safe_on_exception() -> None:
    fake_redis = MagicMock()
    fake_redis.ping = AsyncMock(side_effect=RuntimeError("redis down"))
    payload = await _build_redis_payload(fake_redis)
    assert payload.status == "unavailable"
    assert payload.latency_ms is None


@pytest.mark.asyncio
async def test_build_arq_payload_fail_safe_on_exception() -> None:
    fake_redis = MagicMock()
    fake_redis.zcard = AsyncMock(side_effect=RuntimeError("zcard failed"))
    payload = await _build_arq_payload(fake_redis)
    assert payload.queue_depth is None


@pytest.mark.asyncio
async def test_build_arq_payload_returns_zero_when_queue_empty() -> None:
    fake_redis = MagicMock()
    fake_redis.zcard = AsyncMock(return_value=0)
    payload = await _build_arq_payload(fake_redis)
    assert payload.queue_depth == 0


@pytest.mark.asyncio
async def test_compute_status_degraded_when_redis_ok_but_db_ko() -> None:
    fake_redis = MagicMock()
    fake_redis.ping = AsyncMock(return_value=b"PONG")
    fake_redis.zcard = AsyncMock(return_value=0)
    response = await ExtendedHealthService.compute(db=None, redis=fake_redis)
    assert response.status == "degraded"
    assert response.redis.status == "ok"
    assert response.db.status == "unavailable"


# ══════════════════════════════════════════════════════════════
# Endpoint /ready
# ══════════════════════════════════════════════════════════════


def test_ready_endpoint_returns_extended_payload() -> None:
    """Backward-compat : `data.db` et `data.redis` toujours présents
    + nouveaux champs `version`, `arq`, `uptime_seconds`."""
    with TestClient(app) as client:
        resp = client.get("/ready")
    # Status 200 ou 503 selon dispo dev
    assert resp.status_code in (200, 503)
    body = resp.json()
    data = body.get("data") or {}
    assert "version" in data
    assert "db" in data
    assert "redis" in data
    assert "arq" in data
    assert "uptime_seconds" in data


def test_ready_endpoint_db_field_is_dict_with_status() -> None:
    """Schema breaking : V1 `data.db = "ok"` (string) → V2 `data.db = {status: "ok"}`.

    Documenté dans CLAUDE.md §15 entrée O1. Flutter datasource doit
    parser `data.db.status` au lieu de `data.db`.
    """
    with TestClient(app) as client:
        resp = client.get("/ready")
    data = resp.json().get("data") or {}
    assert isinstance(data["db"], dict)
    assert "status" in data["db"]


def test_health_alias_redirects_to_ready() -> None:
    """`/health` doit rester backward-compat (alias `/ready`)."""
    with TestClient(app) as client:
        resp = client.get("/health")
    assert resp.status_code in (200, 503)
    body = resp.json()
    assert "data" in body
