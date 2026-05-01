"""
Tests d'intégration — auto-save des images générées dans la Library
(Session C3).

Après la génération, `/image/generate` appelle
`LibraryService.create_from_bytes(source='generated', ...)` pour chaque
image retournée et enrichit la réponse avec `library_ids`. En cas
d'échec d'upload ou d'INSERT, le user reçoit quand même les images
base64 (fail-safe : on ne pénalise pas pour une erreur stockage).
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.ai.providers.base import GeneratedImage
from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.library.models import LibraryItem
from app.features.library.service import LibraryService
from app.main import _build_auto_library_title, app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _make_saved_item(*, idx: int = 0) -> LibraryItem:
    now = datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)
    item = LibraryItem(
        user_id=_FAKE_USER_ID,
        type="image",
        title=f"Chat roux ({idx + 1})",
        storage_key=f"c4a2/library/image/ab/fake{idx}.jpg",
        mime_type="image/jpeg",
        size_bytes=100,
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
    item.prompt = "Un chaton roux"
    item.source_conversation_id = None
    item.source_message_id = None
    item.tags = None
    item.metadata_json = None
    return item


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    fake_user = _make_fake_user()
    fake_db = MagicMock()

    async def _user_override() -> User:
        return fake_user

    async def _db_override():
        yield fake_db

    # Budget + modération : on bypass pour se concentrer sur l'auto-save.
    # IMPORTANT : `app.main` fait `from X import Y` au top du fichier, donc
    # les symboles `get_budget_tracker`, `get_moderation_service`,
    # `get_ai_router` sont bindés dans `app.main` au moment de l'import.
    # Patcher uniquement `app.ai.budget_tracker.get_budget_tracker` ne
    # suffit PAS — il faut patcher `app.main.get_budget_tracker` directement
    # (règle « monkeypatch le namespace qui consomme, pas celui qui définit »
    # documentée dans CLAUDE.md §15 E4 décision (i)).
    from app.ai import budget_tracker, moderation
    from app.ai import runtime
    import app.main as main_module

    bt = MagicMock()
    bt.check_and_consume_image = AsyncMock(return_value=None)
    monkeypatch.setattr(budget_tracker, "get_budget_tracker", lambda: bt)
    monkeypatch.setattr(main_module, "get_budget_tracker", lambda: bt)

    mod = MagicMock()
    decision = MagicMock()
    decision.allowed = True
    mod.check = AsyncMock(return_value=decision)
    monkeypatch.setattr(moderation, "get_moderation_service", lambda: mod)
    monkeypatch.setattr(main_module, "get_moderation_service", lambda: mod)

    # Router IA résout vers un provider factice avec generate_images mocké.
    provider = MagicMock()
    provider.name = "gemini-imagen"
    provider.generate_images = AsyncMock(
        return_value=[
            GeneratedImage(
                base64_data=base64.b64encode(b"fake-image-1").decode(),
                mime_type="image/jpeg",
            ),
            GeneratedImage(
                base64_data=base64.b64encode(b"fake-image-2").decode(),
                mime_type="image/jpeg",
            ),
        ]
    )

    ai_router = MagicMock()
    resolution = MagicMock()
    resolution.provider = provider
    resolution.model = "imagen-3.0-generate-002"
    resolution.config.expert_id = "studio"
    ai_router.resolve_image = MagicMock(return_value=resolution)
    monkeypatch.setattr(runtime, "get_ai_router", lambda: ai_router)
    monkeypatch.setattr(main_module, "get_ai_router", lambda: ai_router)

    app.dependency_overrides[get_current_user] = _user_override
    app.dependency_overrides[get_db] = _db_override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


# ══════════════════════════════════════════════════════════════
# 1. Happy path — library_ids peuplés
# ══════════════════════════════════════════════════════════════


def test_image_generate_saves_to_library_and_returns_ids(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = [_make_saved_item(idx=0), _make_saved_item(idx=1)]
    mock_create = AsyncMock(side_effect=saved)
    monkeypatch.setattr(LibraryService, "create_from_bytes", mock_create)

    response = client.post(
        "/image/generate",
        json={"prompt": "Un chaton roux", "count": 2, "expert_id": "studio"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert len(body["data"]["images"]) == 2
    assert body["data"]["library_ids"] == [str(s.id) for s in saved]
    assert mock_create.await_count == 2

    # Vérifier les kwargs du 1er appel : source=generated + provider/model/prompt.
    kwargs = mock_create.await_args_list[0].kwargs
    assert kwargs["source"] == "generated"
    assert kwargs["provider"] == "gemini-imagen"
    assert kwargs["model"] == "imagen-3.0-generate-002"
    assert kwargs["prompt"] == "Un chaton roux"
    assert kwargs["type_"] == "image"


# ══════════════════════════════════════════════════════════════
# 2. Fail-safe — erreur library ne casse pas /image/generate
# ══════════════════════════════════════════════════════════════


def test_image_generate_failsafe_on_library_exception(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si `create_from_bytes` explose, l'endpoint doit quand même
    retourner 200 avec les images base64 — library_ids vide."""
    mock_create = AsyncMock(side_effect=RuntimeError("storage down"))
    monkeypatch.setattr(LibraryService, "create_from_bytes", mock_create)

    response = client.post(
        "/image/generate",
        json={"prompt": "anything", "count": 2, "expert_id": "studio"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert len(body["data"]["images"]) == 2
    assert body["data"]["library_ids"] == []


def test_image_generate_failsafe_partial(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1ère sauvegarde OK, 2ᵉ échoue → library_ids contient juste la 1ère."""
    saved = _make_saved_item(idx=0)
    mock_create = AsyncMock(side_effect=[saved, RuntimeError("storage down on #2")])
    monkeypatch.setattr(LibraryService, "create_from_bytes", mock_create)

    response = client.post(
        "/image/generate",
        json={"prompt": "test", "count": 2, "expert_id": "studio"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["library_ids"] == [str(saved.id)]


# ══════════════════════════════════════════════════════════════
# 3. Helper _build_auto_library_title
# ══════════════════════════════════════════════════════════════


def test_auto_title_single_image_uses_prompt_prefix() -> None:
    title = _build_auto_library_title("Un chaton roux qui joue dans le jardin", 0, 1)
    assert title == "Un chaton roux qui joue dans le jardin"


def test_auto_title_multi_image_appends_index() -> None:
    title = _build_auto_library_title("Paysage d'automne", 2, 4)
    assert title == "Paysage d'automne (3)"


def test_auto_title_long_prompt_truncates_nicely() -> None:
    long = (
        "Un très très long prompt qui dépasse soixante caractères pour tester la troncature propre"
    )
    title = _build_auto_library_title(long, 0, 1)
    assert len(title) <= 60
    # Ne coupe pas au milieu d'un mot (finit sur un mot complet).
    assert not title.endswith(" ")


def test_auto_title_empty_prompt_uses_timestamp_fallback() -> None:
    title = _build_auto_library_title("", 0, 1)
    assert title.startswith("Image générée ")
