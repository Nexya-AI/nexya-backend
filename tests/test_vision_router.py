"""Tests d'intégration — router `POST /vision/analyze` (E2)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import (
    FileTypeNotAllowedException,
    ImageTooLargeException,
    PlanRequiredException,
    RateLimitAbuseException,
    VisionContentFilteredException,
    VisionQuotaExceededException,
    VisionUnavailableException,
)
from app.features.auth.models import User
from app.features.vision.models import VisionAnalysis
from app.features.vision.service import VisionService
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_user(is_pro: bool = False) -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = is_pro
    return user


def _make_fake_analysis() -> VisionAnalysis:
    row = VisionAnalysis(
        user_id=_FAKE_USER_ID,
        image_sha256="a" * 64,
        prompt_sha256="b" * 64,
        prompt="décris",
        analysis_text="C'est un chat roux.",
        model="gemini-2.0-flash",
        provider="gemini",
        tokens_input=400,
        tokens_output=80,
        cost_usd=Decimal("0.000042"),
        image_width=1024,
        image_height=768,
    )
    row.id = uuid.uuid4()
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    row.deleted_at = None
    row.metadata_json = None
    row.source_file_id = None
    row.source_library_id = None
    return row


@pytest.fixture
def client() -> TestClient:
    fake_user = _make_user(is_pro=False)
    fake_db = MagicMock()

    async def _user_override() -> User:
        return fake_user

    async def _db_override():
        yield fake_db

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_db] = _db_override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


def _base_body(tier: str = "flash") -> dict:
    return {
        "prompt": "décris cette image",
        "image_source": "image_base64",
        "image_base64": "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAA",
        "model_tier": tier,
    }


# ══════════════════════════════════════════════════════════════
# 1. Happy path
# ══════════════════════════════════════════════════════════════


def test_analyze_201_returns_analysis(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    row = _make_fake_analysis()
    monkeypatch.setattr(VisionService, "analyze", AsyncMock(return_value=row))
    r = client.post("/vision/analyze", json=_base_body())
    assert r.status_code == 201
    body = r.json()
    assert body["success"] is True
    assert body["data"]["analysis_text"] == "C'est un chat roux."
    assert body["data"]["model"] == "gemini-2.0-flash"
    assert body["data"]["tokens_input"] == 400


# ══════════════════════════════════════════════════════════════
# 2. Validators Pydantic
# ══════════════════════════════════════════════════════════════


def test_analyze_422_on_empty_prompt(client: TestClient) -> None:
    body = _base_body()
    body["prompt"] = ""
    r = client.post("/vision/analyze", json=body)
    assert r.status_code == 422


def test_analyze_422_on_prompt_over_4000_chars(client: TestClient) -> None:
    body = _base_body()
    body["prompt"] = "x" * 4001
    r = client.post("/vision/analyze", json=body)
    assert r.status_code == 422


def test_analyze_422_when_no_source_provided(client: TestClient) -> None:
    r = client.post(
        "/vision/analyze",
        json={"prompt": "q", "image_source": "upload_id"},
    )
    assert r.status_code == 422


def test_analyze_422_when_multiple_sources_provided(
    client: TestClient,
) -> None:
    r = client.post(
        "/vision/analyze",
        json={
            "prompt": "q",
            "image_source": "upload_id",
            "upload_id": str(uuid.uuid4()),
            "image_base64": "data:image/png;base64,AA",
        },
    )
    assert r.status_code == 422


# ══════════════════════════════════════════════════════════════
# 3. Propagation erreurs service
# ══════════════════════════════════════════════════════════════


def test_analyze_403_when_pro_tier_for_free_user(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        VisionService,
        "analyze",
        AsyncMock(side_effect=PlanRequiredException()),
    )
    r = client.post("/vision/analyze", json=_base_body(tier="pro"))
    assert r.status_code == 403
    assert r.json()["code"] == "PLAN_REQUIRED"


def test_analyze_402_on_vision_quota_exceeded(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        VisionService,
        "analyze",
        AsyncMock(side_effect=VisionQuotaExceededException(current=3, maximum=3, plan="free")),
    )
    r = client.post("/vision/analyze", json=_base_body())
    assert r.status_code == 402
    assert r.json()["code"] == "VISION_QUOTA_EXCEEDED"
    assert r.json()["data"]["plan"] == "free"


def test_analyze_413_on_image_too_large(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        VisionService,
        "analyze",
        AsyncMock(side_effect=ImageTooLargeException(size_bytes=12_000_000, max_bytes=10_485_760)),
    )
    r = client.post("/vision/analyze", json=_base_body())
    assert r.status_code == 413
    assert r.json()["code"] == "IMAGE_TOO_LARGE"


def test_analyze_415_on_unsupported_mime(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        VisionService,
        "analyze",
        AsyncMock(side_effect=FileTypeNotAllowedException(mime_type="image/bmp")),
    )
    r = client.post("/vision/analyze", json=_base_body())
    assert r.status_code == 415


def test_analyze_429_on_rate_limit(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        VisionService,
        "analyze",
        AsyncMock(side_effect=RateLimitAbuseException(retry_after=3600)),
    )
    r = client.post("/vision/analyze", json=_base_body())
    assert r.status_code == 429


def test_analyze_400_on_content_filtered(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        VisionService,
        "analyze",
        AsyncMock(side_effect=VisionContentFilteredException(provider="gemini")),
    )
    r = client.post("/vision/analyze", json=_base_body())
    assert r.status_code == 400
    assert r.json()["code"] == "VISION_CONTENT_FILTERED"


def test_analyze_503_on_vision_unavailable(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        VisionService,
        "analyze",
        AsyncMock(side_effect=VisionUnavailableException(provider="gemini", reason="api down")),
    )
    r = client.post("/vision/analyze", json=_base_body())
    assert r.status_code == 503
    assert r.json()["code"] == "VISION_UNAVAILABLE"
