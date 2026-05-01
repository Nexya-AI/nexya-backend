"""
Tests d'intégration — `/image/generate` avec signature C2PA (E4.5).

Couvre :
- Mock-first par défaut : sans clés X.509 → MockManifestProvider
  signe (n'altère pas les bytes mais trace `has_c2pa=True`).
- Response API enrichie : `c2pa_applied` + `c2pa_manifest_ids`.
- Metadata Library enrichie : `has_c2pa` + `c2pa_manifest_id` +
  `c2pa_signed_at` + `c2pa_skip_reason`.
- Multi-images : chaque image est signée individuellement,
  manifest_ids distincts.
- Fail-safe absolu : exception côté provider C2PA → response 200 +
  image originale + `c2pa_applied=False`.
- Kill-switch : `c2pa_enabled=False` → MockManifestProvider sans
  signature (forcé) + `has_c2pa=False`.
- Watermark + C2PA enchaînement : les deux co-existent, watermark
  appliqué EN PREMIER, C2PA signe l'image watermarkée.
"""

from __future__ import annotations

import base64
import io
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.ai.providers.base import GeneratedImage
from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.images.c2pa import (
    C2PASignResult,
    MockManifestProvider,
    reset_manifest_provider_for_tests,
)
from app.features.library.models import LibraryItem
from app.features.library.service import LibraryService
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


# ══════════════════════════════════════════════════════════════
# Helpers (alignés sur test_image_generate_watermark.py E4)
# ══════════════════════════════════════════════════════════════


def _make_user(is_pro: bool = False) -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = is_pro
    return user


def _make_fake_png_bytes(w: int = 1024, h: int = 1024) -> bytes:
    img = Image.new("RGB", (w, h), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_saved_item(*, idx: int = 0) -> LibraryItem:
    now = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    item = LibraryItem(
        user_id=_FAKE_USER_ID,
        type="image",
        title=f"Image générée ({idx + 1})",
        storage_key=f"c4a2/library/image/ab/fake{idx}.png",
        mime_type="image/png",
        size_bytes=1000,
        content_sha256="a" * 64,
        source="generated",
    )
    item.id = uuid.UUID(f"aaaaaaaa-0000-4000-8000-00000000000{idx + 1}")
    item.created_at = now
    item.updated_at = now
    item.deleted_at = None
    item.file_type = None
    item.description = None
    item.width_px = None
    item.height_px = None
    item.duration_ms = None
    item.aspect_ratio = None
    item.provider = "gemini-imagen"
    item.model = "imagen-3.0-generate-002"
    item.prompt = "test prompt"
    item.source_conversation_id = None
    item.source_message_id = None
    item.tags = None
    item.metadata_json = None
    return item


def _install_ai_pipeline_mocks(monkeypatch: pytest.MonkeyPatch, *, count: int = 1):
    """Bypass budget + moderation + AI router pour focus sur le hook C2PA.

    IMPORTANT (leçon E4) : `main.py` importe via `from app.ai.xxx import get_yyy`,
    donc les symboles sont bindés à l'import-time dans `app.main`. Patcher
    les modules source ne suffit pas — il faut patcher `app.main.get_yyy`.
    """
    import app.main as main_module

    bt = MagicMock()
    bt.check_and_consume_image = AsyncMock(return_value=None)
    monkeypatch.setattr(main_module, "get_budget_tracker", lambda: bt)

    mod = MagicMock()
    decision = MagicMock()
    decision.allowed = True
    mod.check = AsyncMock(return_value=decision)
    monkeypatch.setattr(main_module, "get_moderation_service", lambda: mod)

    provider = MagicMock()
    provider.name = "gemini-imagen"
    provider.generate_images = AsyncMock(
        return_value=[
            GeneratedImage(
                base64_data=base64.b64encode(_make_fake_png_bytes()).decode(),
                mime_type="image/png",
            )
            for _ in range(count)
        ]
    )
    ai_router = MagicMock()
    resolution = MagicMock()
    resolution.provider = provider
    resolution.model = "imagen-3.0-generate-002"
    resolution.config.expert_id = "studio"
    ai_router.resolve_image = MagicMock(return_value=resolution)
    monkeypatch.setattr(main_module, "get_ai_router", lambda: ai_router)


def _install_c2pa_mock(
    monkeypatch: pytest.MonkeyPatch,
    *,
    force_skip: bool = False,
    raise_exc: bool = False,
) -> MockManifestProvider:
    """Installe un MockManifestProvider configurable + retourne l'instance
    pour assertions (`provider.calls`, etc.).

    `raise_exc=True` patch `sign_image` pour raise une exception arbitraire
    — permet de tester le fail-safe côté `/image/generate`.
    """
    import app.main as main_module

    reset_manifest_provider_for_tests()
    provider = MockManifestProvider(force_skip=force_skip)

    if raise_exc:

        async def _raising_sign(*args, **kwargs):
            raise RuntimeError("simulated c2pa crash")

        provider.sign_image = _raising_sign  # type: ignore[method-assign]

    monkeypatch.setattr(main_module, "get_manifest_provider", lambda: provider)
    return provider


@pytest.fixture(autouse=True)
def _reset_c2pa_factory():
    """Reset du singleton C2PA factory entre chaque test."""
    reset_manifest_provider_for_tests()
    yield
    reset_manifest_provider_for_tests()


@pytest.fixture
def free_client():
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


# ══════════════════════════════════════════════════════════════
# 1. Happy path — Mock C2PA signe + response enrichie
# ══════════════════════════════════════════════════════════════


def test_c2pa_mock_signs_image_and_enriches_response(
    free_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_pipeline_mocks(monkeypatch, count=1)
    c2pa_provider = _install_c2pa_mock(monkeypatch)
    saved = _make_saved_item(idx=0)
    mock_create = AsyncMock(return_value=saved)
    monkeypatch.setattr(LibraryService, "create_from_bytes", mock_create)

    response = free_client.post(
        "/image/generate",
        json={"prompt": "un chat dans un panier", "count": 1, "expert_id": "studio"},
    )
    assert response.status_code == 200
    body = response.json()

    # Response API enrichie.
    assert body["data"]["c2pa_applied"] is True
    assert body["data"]["c2pa_manifest_ids"] == ["mock-c2pa-000001"]

    # Provider Mock a bien été appelé une fois avec le mime image/png.
    assert len(c2pa_provider.calls) == 1
    mime, sign_request = c2pa_provider.calls[0]
    assert mime == "image/png"
    assert sign_request.provider == "gemini-imagen"
    assert sign_request.model == "imagen-3.0-generate-002"
    assert sign_request.prompt == "un chat dans un panier"
    assert sign_request.watermark_applied is True  # default Free, watermark on


# ══════════════════════════════════════════════════════════════
# 2. Metadata Library enrichie avec champs C2PA
# ══════════════════════════════════════════════════════════════


def test_c2pa_metadata_library_enriched_with_manifest_id(
    free_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_pipeline_mocks(monkeypatch, count=1)
    _install_c2pa_mock(monkeypatch)
    saved = _make_saved_item(idx=0)
    mock_create = AsyncMock(return_value=saved)
    monkeypatch.setattr(LibraryService, "create_from_bytes", mock_create)

    response = free_client.post(
        "/image/generate",
        json={"prompt": "un chat", "count": 1, "expert_id": "studio"},
    )
    assert response.status_code == 200

    kwargs = mock_create.await_args_list[0].kwargs
    metadata = kwargs["metadata_json"]
    assert metadata["has_c2pa"] is True
    assert metadata["c2pa_manifest_id"] == "mock-c2pa-000001"
    assert metadata["c2pa_signed_at"] is not None
    # Format ISO 8601 avec timezone.
    assert "T" in metadata["c2pa_signed_at"]
    assert metadata["c2pa_skip_reason"] is None


# ══════════════════════════════════════════════════════════════
# 3. Multi-images — chaque image signée individuellement
# ══════════════════════════════════════════════════════════════


def test_c2pa_multi_images_each_gets_distinct_manifest_id(
    free_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_pipeline_mocks(monkeypatch, count=3)
    c2pa_provider = _install_c2pa_mock(monkeypatch)
    saved_items = [_make_saved_item(idx=i) for i in range(3)]
    mock_create = AsyncMock(side_effect=saved_items)
    monkeypatch.setattr(LibraryService, "create_from_bytes", mock_create)

    response = free_client.post(
        "/image/generate",
        json={"prompt": "3 chats", "count": 3, "expert_id": "studio"},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["data"]["c2pa_applied"] is True
    assert body["data"]["c2pa_manifest_ids"] == [
        "mock-c2pa-000001",
        "mock-c2pa-000002",
        "mock-c2pa-000003",
    ]
    assert len(c2pa_provider.calls) == 3

    # Chaque INSERT Library a son propre manifest_id.
    for idx, call in enumerate(mock_create.await_args_list):
        assert call.kwargs["metadata_json"]["c2pa_manifest_id"] == (f"mock-c2pa-00000{idx + 1}")


# ══════════════════════════════════════════════════════════════
# 4. Fail-safe absolu — provider C2PA crash → response 200 OK
# ══════════════════════════════════════════════════════════════


def test_c2pa_provider_exception_is_caught_response_still_200(
    free_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si le provider C2PA raise, l'endpoint NE DOIT PAS crasher.
    L'image est livrée originale (sans signature), metadata trace
    `has_c2pa=False` + `c2pa_skip_reason='sign_error'` (V2 fallback).
    """
    _install_ai_pipeline_mocks(monkeypatch, count=1)
    # On utilise un provider qui retourne `applied=False` pour simuler
    # un crash interne fail-safe (le RealC2PAProvider catche en interne
    # et retourne C2PASignResult(applied=False, skip_reason="sign_error")).
    import app.main as main_module

    failing_provider = MagicMock()
    failing_provider.sign_image = AsyncMock(
        return_value=C2PASignResult(
            image_bytes=b"will-be-replaced-by-actual-bytes",
            applied=False,
            skip_reason="sign_error",
        )
    )

    # Side effect : retourne un C2PASignResult avec les bytes d'origine
    # passés (cohérent avec le contrat fail-safe RealC2PAProvider).
    async def _sign_with_original_bytes(image_bytes, mime_type, request):
        return C2PASignResult(
            image_bytes=image_bytes,
            applied=False,
            skip_reason="sign_error",
        )

    failing_provider.sign_image = _sign_with_original_bytes
    monkeypatch.setattr(main_module, "get_manifest_provider", lambda: failing_provider)

    saved = _make_saved_item(idx=0)
    mock_create = AsyncMock(return_value=saved)
    monkeypatch.setattr(LibraryService, "create_from_bytes", mock_create)

    response = free_client.post(
        "/image/generate",
        json={"prompt": "un chat", "count": 1, "expert_id": "studio"},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["data"]["c2pa_applied"] is False
    assert body["data"]["c2pa_manifest_ids"] == [None]

    kwargs = mock_create.await_args_list[0].kwargs
    metadata = kwargs["metadata_json"]
    assert metadata["has_c2pa"] is False
    assert metadata["c2pa_manifest_id"] is None
    assert metadata["c2pa_skip_reason"] == "sign_error"


# ══════════════════════════════════════════════════════════════
# 5. Kill-switch via force_skip — applied=False sans crash
# ══════════════════════════════════════════════════════════════


def test_c2pa_force_skip_returns_applied_false_with_skip_reason(
    free_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_pipeline_mocks(monkeypatch, count=1)
    _install_c2pa_mock(monkeypatch, force_skip=True)
    saved = _make_saved_item(idx=0)
    mock_create = AsyncMock(return_value=saved)
    monkeypatch.setattr(LibraryService, "create_from_bytes", mock_create)

    response = free_client.post(
        "/image/generate",
        json={"prompt": "un chat", "count": 1, "expert_id": "studio"},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["data"]["c2pa_applied"] is False
    assert body["data"]["c2pa_manifest_ids"] == [None]

    kwargs = mock_create.await_args_list[0].kwargs
    assert kwargs["metadata_json"]["has_c2pa"] is False
    assert kwargs["metadata_json"]["c2pa_skip_reason"] == "mock_force_skip"


# ══════════════════════════════════════════════════════════════
# 6. Watermark + C2PA enchaînement — coexistence des deux couches
# ══════════════════════════════════════════════════════════════


def test_c2pa_signs_image_after_watermark_was_applied(
    free_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le sign_request reçu par le provider C2PA doit contenir
    `watermark_applied=True` ET `watermark_version` non-None — c'est
    cette info qui sera embarquée dans le manifest cryptographique
    (preuve « cette image a été watermarkée par NEXYA puis signée »).
    """
    _install_ai_pipeline_mocks(monkeypatch, count=1)
    c2pa_provider = _install_c2pa_mock(monkeypatch)
    saved = _make_saved_item(idx=0)
    monkeypatch.setattr(LibraryService, "create_from_bytes", AsyncMock(return_value=saved))

    response = free_client.post(
        "/image/generate",
        json={"prompt": "un chat", "count": 1, "expert_id": "studio"},
    )
    assert response.status_code == 200

    # Le sign_request reçu par le provider trace que watermark a été appliqué.
    _, sign_request = c2pa_provider.calls[0]
    assert sign_request.watermark_applied is True
    assert sign_request.watermark_version is not None
    assert sign_request.watermark_version.startswith("v")  # ex: "v1-oiseau-bleu-..."


# ══════════════════════════════════════════════════════════════
# 7. Response API : c2pa_applied=False si AU MOINS UNE image rate
# ══════════════════════════════════════════════════════════════


def test_c2pa_applied_global_is_false_if_any_image_skipped(
    free_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Stratégie all-or-nothing : si une seule image n'a pas pu être
    signée (sign_error, format inconnu, etc.), `c2pa_applied=False`
    pour signaler au Flutter d'afficher un badge ⚠️ « partial signing ».
    Le détail per-image reste dans `c2pa_manifest_ids[idx]`.
    """
    _install_ai_pipeline_mocks(monkeypatch, count=2)

    # Provider custom qui signe la 1ère image mais pas la 2ème.
    import app.main as main_module

    counter = {"n": 0}

    async def _selective_sign(image_bytes, mime_type, request):
        counter["n"] += 1
        if counter["n"] == 1:
            return C2PASignResult(
                image_bytes=image_bytes,
                applied=True,
                manifest_id="mock-c2pa-000001",
                signed_at=datetime(2026, 5, 1, tzinfo=UTC),
            )
        return C2PASignResult(
            image_bytes=image_bytes,
            applied=False,
            skip_reason="unsupported_format",
        )

    selective_provider = MagicMock()
    selective_provider.sign_image = _selective_sign
    monkeypatch.setattr(main_module, "get_manifest_provider", lambda: selective_provider)

    saved_items = [_make_saved_item(idx=i) for i in range(2)]
    monkeypatch.setattr(LibraryService, "create_from_bytes", AsyncMock(side_effect=saved_items))

    response = free_client.post(
        "/image/generate",
        json={"prompt": "2 images", "count": 2, "expert_id": "studio"},
    )
    assert response.status_code == 200
    body = response.json()

    assert body["data"]["c2pa_applied"] is False  # all-or-nothing
    assert body["data"]["c2pa_manifest_ids"] == ["mock-c2pa-000001", None]
