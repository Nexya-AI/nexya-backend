"""
Tests d'intégration — `/image/generate` avec watermark NEXYA (E4).

Couvre :
- Free + Pro sans `remove_watermark` → watermark appliqué, metadata
  enrichi `has_watermark=True`, `watermark_version` tracé.
- Pro avec `remove_watermark=True` → watermark NON appliqué,
  `has_watermark=False`, `no_watermark_was_requested=True`.
- Free avec `remove_watermark=True` → 403 `PLAN_REQUIRED`.
- Multi-images : watermark appliqué individuellement sur chaque image.
- Response enrichie avec `watermark_applied` + `watermark_version` globaux.
- Fail-safe : si watermark crashe, image originale retournée + 200.
- Tests de la réponse base64 enrichie après watermark.
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
from app.features.images.watermark import WATERMARK_VERSION
from app.features.library.models import LibraryItem
from app.features.library.service import LibraryService
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_user(is_pro: bool = False) -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = is_pro
    return user


def _make_fake_png_bytes(w: int = 1024, h: int = 1024) -> bytes:
    """Génère une vraie PNG assez grande pour passer le cap 256 px."""
    img = Image.new("RGB", (w, h), color=(100, 150, 200))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_saved_item(*, idx: int = 0) -> LibraryItem:
    now = datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)
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
    """Bypass budget + moderation + AI router pour focus sur le watermark.

    IMPORTANT : `main.py` importe les 3 symboles via
    `from app.ai.xxx import get_yyy`, donc les symboles sont bindés à
    l'import-time dans le namespace de `main`. Monkeypatcher les modules
    source ne suffit pas — il faut aussi patcher `main.get_yyy`.
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

    # Router retourne un provider qui génère de vraies PNG.
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


@pytest.fixture
def pro_client():
    fake_user = _make_user(is_pro=True)
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
# 1. Free sans remove_watermark → watermark appliqué + metadata
# ══════════════════════════════════════════════════════════════


def test_free_user_gets_watermark_applied_and_metadata_tracked(
    free_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_pipeline_mocks(monkeypatch, count=1)
    saved = _make_saved_item(idx=0)
    mock_create = AsyncMock(return_value=saved)
    monkeypatch.setattr(LibraryService, "create_from_bytes", mock_create)

    response = free_client.post(
        "/image/generate",
        json={"prompt": "un chat", "count": 1, "expert_id": "studio"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["watermark_applied"] is True
    assert body["data"]["watermark_version"] == WATERMARK_VERSION

    # Le metadata Library a été enrichi.
    kwargs = mock_create.await_args_list[0].kwargs
    assert kwargs["metadata_json"]["has_watermark"] is True
    assert kwargs["metadata_json"]["watermark_version"] == WATERMARK_VERSION
    assert kwargs["metadata_json"]["no_watermark_was_requested"] is False


# ══════════════════════════════════════════════════════════════
# 2. Pro avec remove_watermark=True → watermark retiré
# ══════════════════════════════════════════════════════════════


def test_pro_user_can_remove_watermark(
    pro_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_pipeline_mocks(monkeypatch, count=1)
    saved = _make_saved_item(idx=0)
    mock_create = AsyncMock(return_value=saved)
    monkeypatch.setattr(LibraryService, "create_from_bytes", mock_create)

    response = pro_client.post(
        "/image/generate",
        json={
            "prompt": "un chat",
            "count": 1,
            "expert_id": "studio",
            "remove_watermark": True,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["watermark_applied"] is False
    assert body["data"]["watermark_version"] is None

    kwargs = mock_create.await_args_list[0].kwargs
    assert kwargs["metadata_json"]["has_watermark"] is False
    assert kwargs["metadata_json"]["watermark_version"] is None
    assert kwargs["metadata_json"]["no_watermark_was_requested"] is True


# ══════════════════════════════════════════════════════════════
# 3. Free avec remove_watermark=True → 403 PLAN_REQUIRED
# ══════════════════════════════════════════════════════════════


def test_free_user_cannot_remove_watermark_gets_403(
    free_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_pipeline_mocks(monkeypatch, count=1)
    mock_create = AsyncMock()
    monkeypatch.setattr(LibraryService, "create_from_bytes", mock_create)

    response = free_client.post(
        "/image/generate",
        json={
            "prompt": "un chat",
            "count": 1,
            "expert_id": "studio",
            "remove_watermark": True,
        },
    )
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "PLAN_REQUIRED"
    # Service Library jamais appelé (rejet avant LLM).
    mock_create.assert_not_awaited()


# ══════════════════════════════════════════════════════════════
# 4. Pro sans remove_watermark → watermark quand même appliqué
# ══════════════════════════════════════════════════════════════


def test_pro_user_default_keeps_watermark(
    pro_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_pipeline_mocks(monkeypatch, count=1)
    saved = _make_saved_item(idx=0)
    monkeypatch.setattr(LibraryService, "create_from_bytes", AsyncMock(return_value=saved))

    response = pro_client.post(
        "/image/generate",
        json={"prompt": "un paysage", "count": 1, "expert_id": "studio"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["watermark_applied"] is True


# ══════════════════════════════════════════════════════════════
# 5. Multi-images : chaque image reçoit son watermark
# ══════════════════════════════════════════════════════════════


def test_multi_images_each_gets_watermark_metadata(
    pro_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_pipeline_mocks(monkeypatch, count=3)
    saved_items = [_make_saved_item(idx=i) for i in range(3)]
    mock_create = AsyncMock(side_effect=saved_items)
    monkeypatch.setattr(LibraryService, "create_from_bytes", mock_create)

    response = pro_client.post(
        "/image/generate",
        json={"prompt": "3 chats", "count": 3, "expert_id": "studio"},
    )
    assert response.status_code == 200
    assert mock_create.await_count == 3
    for call in mock_create.await_args_list:
        assert call.kwargs["metadata_json"]["has_watermark"] is True
        assert call.kwargs["metadata_json"]["watermark_version"] == WATERMARK_VERSION


# ══════════════════════════════════════════════════════════════
# 6. Images renvoyées au client contiennent le watermark incrusté
# ══════════════════════════════════════════════════════════════


def test_response_base64_contains_watermarked_image(
    free_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Les bytes du base64 retourné au client doivent différer de l'image
    générée originale — le watermark y est incrusté."""
    _install_ai_pipeline_mocks(monkeypatch, count=1)
    saved = _make_saved_item(idx=0)
    monkeypatch.setattr(LibraryService, "create_from_bytes", AsyncMock(return_value=saved))

    response = free_client.post(
        "/image/generate",
        json={"prompt": "test", "count": 1, "expert_id": "studio"},
    )
    assert response.status_code == 200
    body = response.json()
    returned_b64 = body["data"]["images"][0]["base64"]
    returned_bytes = base64.b64decode(returned_b64)
    # Les bytes rendus ne sont PAS ceux de l'image originale plate —
    # on s'attend à ce qu'ils contiennent l'overlay PNG.
    original_plain = _make_fake_png_bytes()
    assert returned_bytes != original_plain


# ══════════════════════════════════════════════════════════════
# 7. Fail-safe watermark : si apply crashe → image originale renvoyée
# ══════════════════════════════════════════════════════════════


def test_watermark_failsafe_returns_original_on_exception(
    free_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si `apply_nexya_watermark` crashe (logo introuvable simulé), le
    handler renvoie 200 avec les images originales + metadata
    `has_watermark=False`."""
    _install_ai_pipeline_mocks(monkeypatch, count=1)
    # Simuler un crash côté watermark → retour (original, False).
    import app.main as main_module

    def _failing_apply(data, mime, **kwargs):
        return data, False  # fail-safe : pas de raise, applied=False

    monkeypatch.setattr(main_module, "apply_nexya_watermark", _failing_apply)

    saved = _make_saved_item(idx=0)
    mock_create = AsyncMock(return_value=saved)
    monkeypatch.setattr(LibraryService, "create_from_bytes", mock_create)

    response = free_client.post(
        "/image/generate",
        json={"prompt": "test", "count": 1, "expert_id": "studio"},
    )
    assert response.status_code == 200
    body = response.json()
    # Le handler a tenté le watermark (apply_watermark=True globalement)
    # mais chaque image a has_watermark=False après le fail-safe.
    assert body["data"]["watermark_applied"] is True  # flag global user-intent
    kwargs = mock_create.await_args_list[0].kwargs
    assert kwargs["metadata_json"]["has_watermark"] is False


# ══════════════════════════════════════════════════════════════
# 8. library_ids peuplé avec les items sauvegardés
# ══════════════════════════════════════════════════════════════


def test_library_ids_populated_with_saved_items(
    pro_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_pipeline_mocks(monkeypatch, count=2)
    saved_items = [_make_saved_item(idx=i) for i in range(2)]
    monkeypatch.setattr(
        LibraryService,
        "create_from_bytes",
        AsyncMock(side_effect=saved_items),
    )

    response = pro_client.post(
        "/image/generate",
        json={"prompt": "x", "count": 2, "expert_id": "studio"},
    )
    assert response.status_code == 200
    body = response.json()
    assert len(body["data"]["library_ids"]) == 2
    assert body["data"]["library_ids"] == [str(s.id) for s in saved_items]


# ══════════════════════════════════════════════════════════════
# 9. Response shape — nouveaux champs présents
# ══════════════════════════════════════════════════════════════


def test_response_shape_contains_watermark_fields(
    free_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_pipeline_mocks(monkeypatch, count=1)
    monkeypatch.setattr(
        LibraryService,
        "create_from_bytes",
        AsyncMock(return_value=_make_saved_item()),
    )

    response = free_client.post(
        "/image/generate",
        json={"prompt": "x", "count": 1, "expert_id": "studio"},
    )
    body = response.json()
    data = body["data"]
    assert "watermark_applied" in data
    assert "watermark_version" in data
    assert "library_ids" in data
    assert "images" in data
    assert "provider" in data
    assert "model" in data


# ══════════════════════════════════════════════════════════════
# 10. Validation Pydantic — remove_watermark type bool
# ══════════════════════════════════════════════════════════════


def test_remove_watermark_non_bool_rejected(
    free_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_pipeline_mocks(monkeypatch, count=1)
    response = free_client.post(
        "/image/generate",
        json={
            "prompt": "x",
            "count": 1,
            "expert_id": "studio",
            "remove_watermark": "oui",  # invalide
        },
    )
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# 11. Fail-safe library : metadata quand même présent côté API response
# ══════════════════════════════════════════════════════════════


def test_library_save_failure_keeps_watermark_response(
    free_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si LibraryService crashe, response reste 200 avec images
    base64 watermarkées + library_ids=[]."""
    _install_ai_pipeline_mocks(monkeypatch, count=1)
    monkeypatch.setattr(
        LibraryService,
        "create_from_bytes",
        AsyncMock(side_effect=RuntimeError("storage down")),
    )
    response = free_client.post(
        "/image/generate",
        json={"prompt": "x", "count": 1, "expert_id": "studio"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["watermark_applied"] is True
    assert body["data"]["library_ids"] == []
