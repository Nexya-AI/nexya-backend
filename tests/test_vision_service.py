"""Tests d'intégration — `VisionService` (E2).

Monkey-patch providers + budget + rate limit + object store pour
isoler le pipeline sans clé ni Postgres.
"""

from __future__ import annotations

import base64
import io
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from PIL import Image

from app.ai.vision.base import (
    VisionContentFilteredError,
    VisionResult,
    VisionUnavailableError,
)
from app.core.errors.exceptions import (
    FileTypeNotAllowedException,
    ImageTooLargeException,
    LlmQuotaExceededException,
    PlanRequiredException,
    ValidationException,
    VisionContentFilteredException,
    VisionQuotaExceededException,
    VisionUnavailableException,
)
from app.features.vision import service as vision_service_module
from app.features.vision.schemas import VisionAnalyzeRequest
from app.features.vision.service import VisionService

_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_user(is_pro: bool = False) -> Any:
    user = MagicMock()
    user.id = _USER_ID
    user.is_pro = is_pro
    return user


def _make_png(w: int = 512, h: int = 512) -> bytes:
    img = Image.new("RGB", (w, h), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class _FakeProvider:
    name = "mock"
    supports_tiers = {"flash", "pro"}

    def __init__(self, *, raise_type: str | None = None) -> None:
        self._raise = raise_type
        self.calls: list[tuple] = []

    async def analyze_images(
        self,
        images,
        prompt,
        *,
        tier="flash",
        system_prompt=None,
        max_output_tokens=1024,
    ):
        self.calls.append((len(images), prompt, tier))
        if self._raise == "content":
            raise VisionContentFilteredError("blocked", provider="mock")
        if self._raise == "unavailable":
            raise VisionUnavailableError("down", provider="mock")
        return VisionResult(
            text="fake analysis text",
            tokens_input=300,
            tokens_output=50,
            model=f"mock-vision-{tier}",
            provider="mock",
            cost_usd=0.0001,
        )


class _NoBudget:
    user_vision_images_per_day = 50

    def __init__(self) -> None:
        self.consume_calls: list[int] = []
        self.refund_calls: list[int] = []

    async def check_and_consume_vision_images(self, uid, *, images=1):
        self.consume_calls.append(images)
        return images

    async def refund_vision_images(self, uid, *, images):
        self.refund_calls.append(images)


class _FakeObjectStore:
    def __init__(self, data: bytes) -> None:
        self._data = data

    async def download_bytes(self, key: str) -> bytes:
        return self._data


class _FakeUploadedFile:
    def __init__(self, *, mime: str = "image/png") -> None:
        self.id = uuid.uuid4()
        self.storage_key = f"{_USER_ID}/uploads/xx/abc.png"
        self.mime_type = mime
        self.deleted_at = None
        self.user_id = _USER_ID


class _FakeDB:
    def __init__(self, *, existing=None) -> None:
        self._existing = existing
        self.added: list[Any] = []
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    async def execute(self, stmt, *args, **kwargs):
        result = MagicMock()
        result.scalar_one_or_none.return_value = self._existing
        return result

    def add(self, obj):
        if not getattr(obj, "id", None):
            obj.id = uuid.uuid4()
        obj.created_at = datetime.now(UTC)
        obj.updated_at = datetime.now(UTC)
        self.added.append(obj)


@pytest.fixture(autouse=True)
def _bypass_rate_limit(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(
        vision_service_module,
        "check_user_rate_limit",
        AsyncMock(return_value=None),
    )


def _install_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider=None,
    budget=None,
    image_bytes: bytes | None = None,
) -> dict:
    p = provider or _FakeProvider()
    b = budget or _NoBudget()
    monkeypatch.setattr(
        vision_service_module,
        "get_vision_provider",
        lambda tier="flash": p,
    )
    monkeypatch.setattr(vision_service_module, "get_budget_tracker", lambda: b)
    if image_bytes is not None:
        monkeypatch.setattr(
            vision_service_module,
            "get_object_store",
            lambda: _FakeObjectStore(image_bytes),
        )
    return {"provider": p, "budget": b}


# ══════════════════════════════════════════════════════════════
# Pydantic validator — mutex des 3 sources
# ══════════════════════════════════════════════════════════════


def test_request_422_when_no_source_provided() -> None:
    with pytest.raises(Exception):  # ValidationError Pydantic
        VisionAnalyzeRequest(prompt="q", image_source="upload_id")


def test_request_422_when_two_sources_provided() -> None:
    with pytest.raises(Exception):
        VisionAnalyzeRequest(
            prompt="q",
            image_source="upload_id",
            upload_id=uuid.uuid4(),
            image_base64="data:image/png;base64,AAAA",
        )


def test_request_422_when_source_field_inconsistent() -> None:
    with pytest.raises(Exception):
        # image_source='upload_id' mais on fournit library_id à la place.
        VisionAnalyzeRequest(
            prompt="q",
            image_source="upload_id",
            library_id=uuid.uuid4(),
        )


# ══════════════════════════════════════════════════════════════
# Tier pro sur Free → 403 PLAN_REQUIRED
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_analyze_pro_tier_raises_plan_required_for_free_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mocks(monkeypatch, image_bytes=_make_png())
    body = VisionAnalyzeRequest(
        prompt="q",
        image_source="image_base64",
        image_base64=f"data:image/png;base64,{base64.b64encode(_make_png()).decode()}",
        model_tier="pro",
    )
    user = _make_user(is_pro=False)
    db = _FakeDB()
    with pytest.raises(PlanRequiredException):
        await VisionService.analyze(user, db, body=body)


# ══════════════════════════════════════════════════════════════
# Mode base64 happy path
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_analyze_base64_happy_path_inserts_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = _make_png()
    mocks = _install_mocks(monkeypatch, image_bytes=png)
    data_url = f"data:image/png;base64,{base64.b64encode(png).decode()}"
    body = VisionAnalyzeRequest(
        prompt="décris",
        image_source="image_base64",
        image_base64=data_url,
    )
    user = _make_user(is_pro=False)
    db = _FakeDB()
    row = await VisionService.analyze(user, db, body=body)
    assert row.analysis_text == "fake analysis text"
    assert row.model == "mock-vision-flash"
    assert row.provider == "mock"
    assert len(db.added) == 1


# ══════════════════════════════════════════════════════════════
# Mode upload_id
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_analyze_upload_id_resolves_via_FileUploadService(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = _make_png()
    _install_mocks(monkeypatch, image_bytes=png)
    upload = _FakeUploadedFile()

    from app.features.files.service import FileUploadService

    monkeypatch.setattr(
        FileUploadService,
        "get_for_user",
        AsyncMock(return_value=upload),
    )
    body = VisionAnalyzeRequest(
        prompt="q",
        image_source="upload_id",
        upload_id=upload.id,
    )
    user = _make_user(is_pro=False)
    db = _FakeDB()
    row = await VisionService.analyze(user, db, body=body)
    assert row.source_file_id == upload.id


# ══════════════════════════════════════════════════════════════
# Rejet MIME (BMP non whitelisté)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_analyze_rejects_non_image_mime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_mocks(monkeypatch, image_bytes=b"fake")
    upload = _FakeUploadedFile(mime="image/bmp")
    from app.features.files.service import FileUploadService

    monkeypatch.setattr(
        FileUploadService,
        "get_for_user",
        AsyncMock(return_value=upload),
    )
    body = VisionAnalyzeRequest(
        prompt="q",
        image_source="upload_id",
        upload_id=upload.id,
    )
    user = _make_user(is_pro=False)
    db = _FakeDB()
    with pytest.raises(FileTypeNotAllowedException):
        await VisionService.analyze(user, db, body=body)


# ══════════════════════════════════════════════════════════════
# Cap taille image → 413
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_analyze_rejects_image_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "vision_max_image_bytes", 1000, raising=False)
    big_png = _make_png(2000, 2000)  # > 1000 bytes après encodage
    _install_mocks(monkeypatch, image_bytes=big_png)
    data_url = f"data:image/png;base64,{base64.b64encode(big_png).decode()}"
    body = VisionAnalyzeRequest(
        prompt="q",
        image_source="image_base64",
        image_base64=data_url,
    )
    user = _make_user(is_pro=False)
    db = _FakeDB()
    with pytest.raises(ImageTooLargeException):
        await VisionService.analyze(user, db, body=body)


# ══════════════════════════════════════════════════════════════
# Resize appliqué si dimensions > max
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_analyze_resizes_large_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "vision_max_dimension", 1024, raising=False)
    big_png = _make_png(4096, 2048)
    mocks = _install_mocks(monkeypatch, image_bytes=big_png)
    data_url = f"data:image/png;base64,{base64.b64encode(big_png).decode()}"
    body = VisionAnalyzeRequest(
        prompt="q",
        image_source="image_base64",
        image_base64=data_url,
    )
    user = _make_user(is_pro=False)
    db = _FakeDB()
    row = await VisionService.analyze(user, db, body=body)
    # Dimensions stockées = post-resize.
    assert row.image_width is not None
    assert row.image_width <= 1024


# ══════════════════════════════════════════════════════════════
# Dédup actif
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_analyze_dedup_returns_existing_without_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = MagicMock()
    existing.id = uuid.uuid4()
    existing.model = "mock-vision-flash"

    png = _make_png()
    provider = _FakeProvider()
    budget = _NoBudget()
    monkeypatch.setattr(
        vision_service_module,
        "get_vision_provider",
        lambda tier="flash": provider,
    )
    monkeypatch.setattr(vision_service_module, "get_budget_tracker", lambda: budget)
    monkeypatch.setattr(
        vision_service_module,
        "get_object_store",
        lambda: _FakeObjectStore(png),
    )

    data_url = f"data:image/png;base64,{base64.b64encode(png).decode()}"
    body = VisionAnalyzeRequest(
        prompt="q",
        image_source="image_base64",
        image_base64=data_url,
    )
    user = _make_user(is_pro=False)
    db = _FakeDB(existing=existing)
    row = await VisionService.analyze(user, db, body=body)
    assert row is existing
    # Provider non-appelé.
    assert provider.calls == []


# ══════════════════════════════════════════════════════════════
# Quota dépassé → 402 VISION_QUOTA_EXCEEDED
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_analyze_raises_vision_quota_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.errors.exceptions import RateLimitExceededException

    class _CappedBudget:
        user_vision_images_per_day = 3

        async def check_and_consume_vision_images(self, uid, *, images=1):
            raise RateLimitExceededException(reset_at=None)

        async def refund_vision_images(self, uid, *, images):
            pass

    png = _make_png()
    _install_mocks(monkeypatch, budget=_CappedBudget(), image_bytes=png)
    data_url = f"data:image/png;base64,{base64.b64encode(png).decode()}"
    body = VisionAnalyzeRequest(
        prompt="q",
        image_source="image_base64",
        image_base64=data_url,
    )
    user = _make_user(is_pro=False)
    db = _FakeDB()
    with pytest.raises(VisionQuotaExceededException) as ctx:
        await VisionService.analyze(user, db, body=body)
    assert ctx.value.data["plan"] == "free"


# ══════════════════════════════════════════════════════════════
# Provider content filter → 400
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_analyze_maps_content_filter_to_400_and_refunds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = _make_png()
    mocks = _install_mocks(
        monkeypatch,
        provider=_FakeProvider(raise_type="content"),
        image_bytes=png,
    )
    data_url = f"data:image/png;base64,{base64.b64encode(png).decode()}"
    body = VisionAnalyzeRequest(
        prompt="q",
        image_source="image_base64",
        image_base64=data_url,
    )
    user = _make_user(is_pro=False)
    db = _FakeDB()
    with pytest.raises(VisionContentFilteredException):
        await VisionService.analyze(user, db, body=body)
    # Refund appelé.
    assert mocks["budget"].refund_calls == [1]


# ══════════════════════════════════════════════════════════════
# Provider unavailable → 503 + refund
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_analyze_maps_unavailable_to_503_and_refunds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    png = _make_png()
    mocks = _install_mocks(
        monkeypatch,
        provider=_FakeProvider(raise_type="unavailable"),
        image_bytes=png,
    )
    data_url = f"data:image/png;base64,{base64.b64encode(png).decode()}"
    body = VisionAnalyzeRequest(
        prompt="q",
        image_source="image_base64",
        image_base64=data_url,
    )
    user = _make_user(is_pro=False)
    db = _FakeDB()
    with pytest.raises(VisionUnavailableException):
        await VisionService.analyze(user, db, body=body)
    assert mocks["budget"].refund_calls == [1]


# ══════════════════════════════════════════════════════════════
# Cap tokens image totaux → LLM_QUOTA_EXCEEDED
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_analyze_rejects_when_estimated_tokens_exceed_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    # Cap tokens image très bas pour déclencher le rejet.
    monkeypatch.setattr(settings, "vision_max_input_tokens_per_request", 10, raising=False)
    png = _make_png(2048, 2048)  # ≥ 2000 tokens estimés
    _install_mocks(monkeypatch, image_bytes=png)
    data_url = f"data:image/png;base64,{base64.b64encode(png).decode()}"
    body = VisionAnalyzeRequest(
        prompt="q",
        image_source="image_base64",
        image_base64=data_url,
    )
    user = _make_user(is_pro=False)
    db = _FakeDB()
    with pytest.raises(LlmQuotaExceededException):
        await VisionService.analyze(user, db, body=body)


# ══════════════════════════════════════════════════════════════
# Library_id résout via LibraryService
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_analyze_library_id_resolves_via_LibraryService(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.library.service import LibraryService

    png = _make_png()
    item = MagicMock()
    item.id = uuid.uuid4()
    item.type = "image"
    item.mime_type = "image/png"
    item.storage_key = f"{_USER_ID}/library/image/xx/abc.png"
    monkeypatch.setattr(LibraryService, "get", AsyncMock(return_value=item))
    _install_mocks(monkeypatch, image_bytes=png)
    body = VisionAnalyzeRequest(
        prompt="q",
        image_source="library_id",
        library_id=item.id,
    )
    user = _make_user(is_pro=False)
    db = _FakeDB()
    row = await VisionService.analyze(user, db, body=body)
    assert row.source_library_id == item.id


# ══════════════════════════════════════════════════════════════
# Library_id pointe non-image → 422
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_analyze_library_id_rejects_non_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.features.library.service import LibraryService

    item = MagicMock()
    item.type = "audio"  # pas image
    item.mime_type = "audio/mpeg"
    monkeypatch.setattr(LibraryService, "get", AsyncMock(return_value=item))
    _install_mocks(monkeypatch, image_bytes=b"fake")
    body = VisionAnalyzeRequest(
        prompt="q",
        image_source="library_id",
        library_id=uuid.uuid4(),
    )
    user = _make_user(is_pro=False)
    db = _FakeDB()
    with pytest.raises(ValidationException):
        await VisionService.analyze(user, db, body=body)
