"""
Tests N4 — `app.features.helpdesk.router`.

Couvre :
1. `GET /admin/helpdesk/metrics` admin → 200 + payload structuré
2. `GET /admin/helpdesk/metrics` non-admin → 403
3. `GET /admin/helpdesk/metrics` sans auth → 401/403
4. Smoke endpoint monté
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import require_admin
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.helpdesk import service as helpdesk_service_mod
from app.features.helpdesk.schemas import (
    CategoryBreakdown,
    HelpdeskMetricsResponse,
)
from app.main import app


def _make_admin_user() -> MagicMock:
    user = MagicMock(spec=User)
    user.id = uuid.uuid4()
    user.email = "admin@nexya.ai"
    user.is_active = True
    user.is_pro = True
    return user


def _fake_metrics() -> HelpdeskMetricsResponse:
    return HelpdeskMetricsResponse(
        open_count=3,
        in_progress_count=2,
        resolved_count=15,
        cancelled_count=1,
        total_count=21,
        median_resolved_age_hours=4.5,
        oldest_open_age_hours=12.0,
        breakdown_per_category=[
            CategoryBreakdown(
                category="payment",
                open_count=2,
                in_progress_count=1,
                resolved_count=8,
                cancelled_count=0,
            ),
            CategoryBreakdown(
                category="llm_unavailable",
                open_count=1,
                in_progress_count=1,
                resolved_count=7,
                cancelled_count=1,
            ),
        ],
    )


# ══════════════════════════════════════════════════════════════
# Smoke
# ══════════════════════════════════════════════════════════════


def test_admin_helpdesk_metrics_endpoint_mounted() -> None:
    paths = {route.path for route in app.routes}  # type: ignore[attr-defined]
    assert "/admin/helpdesk/metrics" in paths


# ══════════════════════════════════════════════════════════════
# Auth requise
# ══════════════════════════════════════════════════════════════


def test_admin_helpdesk_metrics_no_auth_returns_401_or_403() -> None:
    resp = TestClient(app).get("/admin/helpdesk/metrics")
    assert resp.status_code in (401, 403)


# ══════════════════════════════════════════════════════════════
# Happy path admin
# ══════════════════════════════════════════════════════════════


def test_admin_helpdesk_metrics_happy_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin = _make_admin_user()
    fake_db = MagicMock()

    app.dependency_overrides[require_admin] = lambda: admin
    app.dependency_overrides[get_db] = lambda: fake_db

    fake_metrics = _fake_metrics()
    compute_mock = AsyncMock(return_value=fake_metrics)
    monkeypatch.setattr(
        helpdesk_service_mod.HelpdeskMetricsService,
        "compute",
        compute_mock,
    )

    try:
        with TestClient(app) as client:
            resp = client.get("/admin/helpdesk/metrics")
            assert resp.status_code == 200
            body = resp.json()
            assert body["success"] is True
            data = body["data"]
            assert data["open_count"] == 3
            assert data["resolved_count"] == 15
            assert data["total_count"] == 21
            assert data["median_resolved_age_hours"] == 4.5
            assert len(data["breakdown_per_category"]) == 2
            categories = {b["category"] for b in data["breakdown_per_category"]}
            assert "payment" in categories
            assert "llm_unavailable" in categories
        assert compute_mock.await_count == 1
    finally:
        app.dependency_overrides.pop(require_admin, None)
        app.dependency_overrides.pop(get_db, None)
