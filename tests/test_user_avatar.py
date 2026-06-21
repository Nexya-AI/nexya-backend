"""Tests du pipeline avatar (`POST/DELETE /user/avatar`) — Avatar lot.

Couverture :
  - helpers `_ext_for_mime` + `_build_avatar_storage_key`
  - `AvatarService.upload_avatar` : happy PNG/JPEG/WebP, 415 MIME, 413 size,
    415 magic mismatch / unknown, ré-upload extension différente → delete old
  - `AvatarService.delete_avatar` : happy + idempotent
  - `build_profile_response` : avec clé (presigned), sans clé (None), fail-safe
  - `delete_avatar_blob_best_effort` : None + fail-safe

Tout est mock-store (zéro MinIO réel) — on instancie `MockObjectStore` et on
le passe explicitement via `store=` pour éviter le singleton process-wide.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors.exceptions import (
    FileContentMismatchException,
    FileTypeNotAllowedException,
    ImageTooLargeException,
)
from app.core.storage.object_store import MockObjectStore
from app.features.auth.avatar import (
    AvatarService,
    _build_avatar_storage_key,
    _ext_for_mime,
    build_profile_response,
    delete_avatar_blob_best_effort,
)
from app.features.auth.models import User

# ── Payloads images minimalistes (magic-bytes valides) ───────────────────────
# `detect_mime_type` ne décode pas l'image — il inspecte la signature. Les
# bytes de tête suffisent. Le crop carré 512² est fait côté Flutter.
_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
_JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
_WEBP = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 64
_NOT_IMAGE = b"this is plainly not an image payload at all" + b"\x00" * 16


def _make_user(*, avatar_storage_key: str | None = None) -> User:
    """User ORM en mémoire — UUIDMixin pose id/created_at au flush DB, donc
    on les renseigne explicitement pour un objet non persisté."""
    user = User(
        email="avatar@nexya.ai",
        username="avataruser",
        password_hash="x",
        display_name="Avatar User",
        locale="fr",
        timezone="UTC",
        plan="free",
        data_collection_enabled=False,
    )
    user.id = uuid.uuid4()
    user.avatar_url = None
    user.avatar_storage_key = avatar_storage_key
    user.bio = None
    user.voice_id = None
    user.plan_expires_at = None
    user.created_at = datetime.now(UTC)
    user.updated_at = datetime.now(UTC)
    return user


class _FakeUploadFile:
    """Minimal `fastapi.UploadFile` — expose `content_type` + `read(size)`."""

    def __init__(self, data: bytes, content_type: str, filename: str = "avatar.img") -> None:
        self.content_type = content_type
        self.filename = filename
        self._buf = io.BytesIO(data)

    async def read(self, size: int = -1) -> bytes:
        return self._buf.read(size)


def _make_db() -> MagicMock:
    db = MagicMock()
    db.flush = AsyncMock()
    return db


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════


def test_ext_for_mime_maps_three_formats() -> None:
    assert _ext_for_mime("image/jpeg") == "jpg"
    assert _ext_for_mime("image/jpg") == "jpg"
    assert _ext_for_mime("image/png") == "png"
    assert _ext_for_mime("image/webp") == "webp"
    # Fallback défensif (ne devrait jamais arriver post-whitelist).
    assert _ext_for_mime("image/unknown") == "jpg"


def test_build_storage_key_is_fixed_per_user() -> None:
    uid = uuid.uuid4()
    assert _build_avatar_storage_key(uid, "image/png") == f"users/{uid}/avatar.png"
    assert _build_avatar_storage_key(uid, "image/jpeg") == f"users/{uid}/avatar.jpg"
    assert _build_avatar_storage_key(uid, "image/webp") == f"users/{uid}/avatar.webp"


# ══════════════════════════════════════════════════════════════
# upload_avatar — happy paths
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_upload_avatar_happy_png_sets_key_and_uploads_blob() -> None:
    user = _make_user()
    db = _make_db()
    store = MockObjectStore()
    up = _FakeUploadFile(_PNG, "image/png", "me.png")

    profile = await AvatarService.upload_avatar(user, db, upload_file=up, store=store)

    expected_key = f"users/{user.id}/avatar.png"
    assert user.avatar_storage_key == expected_key
    assert user.avatar_url is None  # colonne legacy jamais écrite
    assert store._fetch_raw(expected_key) == _PNG
    assert profile.avatar_url is not None
    assert profile.avatar_url.startswith("mock://")
    db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_upload_avatar_jpeg_uses_jpg_extension() -> None:
    user = _make_user()
    store = MockObjectStore()
    up = _FakeUploadFile(_JPEG, "image/jpeg")
    await AvatarService.upload_avatar(user, _make_db(), upload_file=up, store=store)
    assert user.avatar_storage_key == f"users/{user.id}/avatar.jpg"


@pytest.mark.asyncio
async def test_upload_avatar_webp_uses_webp_extension() -> None:
    user = _make_user()
    store = MockObjectStore()
    up = _FakeUploadFile(_WEBP, "image/webp")
    await AvatarService.upload_avatar(user, _make_db(), upload_file=up, store=store)
    assert user.avatar_storage_key == f"users/{user.id}/avatar.webp"


# ══════════════════════════════════════════════════════════════
# upload_avatar — rejets
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_upload_avatar_rejects_non_image_mime() -> None:
    user = _make_user()
    store = MockObjectStore()
    up = _FakeUploadFile(_PNG, "application/pdf")
    with pytest.raises(FileTypeNotAllowedException):
        await AvatarService.upload_avatar(user, _make_db(), upload_file=up, store=store)


@pytest.mark.asyncio
async def test_upload_avatar_rejects_gif_not_in_whitelist() -> None:
    # GIF est une image valide mais HORS whitelist avatar (pas de GIF animé).
    user = _make_user()
    store = MockObjectStore()
    up = _FakeUploadFile(b"GIF89a" + b"\x00" * 32, "image/gif")
    with pytest.raises(FileTypeNotAllowedException):
        await AvatarService.upload_avatar(user, _make_db(), upload_file=up, store=store)


@pytest.mark.asyncio
async def test_upload_avatar_rejects_too_large(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "avatar_max_upload_bytes", 16)
    user = _make_user()
    store = MockObjectStore()
    up = _FakeUploadFile(_JPEG, "image/jpeg")  # 68 bytes > 16
    with pytest.raises(ImageTooLargeException):
        await AvatarService.upload_avatar(user, _make_db(), upload_file=up, store=store)


@pytest.mark.asyncio
async def test_upload_avatar_rejects_magic_mismatch() -> None:
    # Annonce JPEG mais le contenu est un PNG → anti-smuggling.
    user = _make_user()
    store = MockObjectStore()
    up = _FakeUploadFile(_PNG, "image/jpeg")
    with pytest.raises(FileContentMismatchException):
        await AvatarService.upload_avatar(user, _make_db(), upload_file=up, store=store)
    # Aucun blob ne doit avoir été uploadé sur un rejet.
    assert store._fetch_raw(f"users/{user.id}/avatar.jpg") is None


@pytest.mark.asyncio
async def test_upload_avatar_rejects_magic_unknown() -> None:
    user = _make_user()
    store = MockObjectStore()
    up = _FakeUploadFile(_NOT_IMAGE, "image/png")
    with pytest.raises(FileContentMismatchException):
        await AvatarService.upload_avatar(user, _make_db(), upload_file=up, store=store)


# ══════════════════════════════════════════════════════════════
# upload_avatar — ré-upload (overwrite + cleanup orphelin)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_reupload_different_extension_deletes_old_blob() -> None:
    user = _make_user()
    store = MockObjectStore()
    # 1er upload PNG.
    await AvatarService.upload_avatar(
        user, _make_db(), upload_file=_FakeUploadFile(_PNG, "image/png"), store=store
    )
    old_key = f"users/{user.id}/avatar.png"
    assert store._fetch_raw(old_key) == _PNG

    # 2e upload JPEG → nouvelle clé .jpg, l'ancienne .png doit être supprimée.
    await AvatarService.upload_avatar(
        user, _make_db(), upload_file=_FakeUploadFile(_JPEG, "image/jpeg"), store=store
    )
    new_key = f"users/{user.id}/avatar.jpg"
    assert user.avatar_storage_key == new_key
    assert store._fetch_raw(new_key) == _JPEG
    assert store._fetch_raw(old_key) is None  # orphelin nettoyé


@pytest.mark.asyncio
async def test_reupload_same_extension_overwrites_no_delete() -> None:
    user = _make_user()
    store = MockObjectStore()
    await AvatarService.upload_avatar(
        user, _make_db(), upload_file=_FakeUploadFile(_PNG, "image/png"), store=store
    )
    key = f"users/{user.id}/avatar.png"
    # Ré-upload PNG (contenu différent) → même clé, overwrite.
    new_png = b"\x89PNG\r\n\x1a\n" + b"\xff" * 64
    await AvatarService.upload_avatar(
        user, _make_db(), upload_file=_FakeUploadFile(new_png, "image/png"), store=store
    )
    assert user.avatar_storage_key == key
    assert store._fetch_raw(key) == new_png


# ══════════════════════════════════════════════════════════════
# delete_avatar
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_delete_avatar_removes_blob_and_clears_key() -> None:
    user = _make_user()
    store = MockObjectStore()
    await AvatarService.upload_avatar(
        user, _make_db(), upload_file=_FakeUploadFile(_PNG, "image/png"), store=store
    )
    key = user.avatar_storage_key
    assert key is not None

    db = _make_db()
    profile = await AvatarService.delete_avatar(user, db, store=store)
    assert user.avatar_storage_key is None
    assert store._fetch_raw(key) is None
    assert profile.avatar_url is None
    db.flush.assert_awaited()


@pytest.mark.asyncio
async def test_delete_avatar_idempotent_when_no_avatar() -> None:
    user = _make_user(avatar_storage_key=None)
    store = MockObjectStore()
    db = _make_db()
    profile = await AvatarService.delete_avatar(user, db, store=store)
    assert user.avatar_storage_key is None
    assert profile.avatar_url is None
    db.flush.assert_not_awaited()  # rien à flush


# ══════════════════════════════════════════════════════════════
# build_profile_response
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_build_profile_response_with_key_regenerates_presigned() -> None:
    key = f"users/{uuid.uuid4()}/avatar.jpg"
    user = _make_user(avatar_storage_key=key)
    store = MockObjectStore()
    profile = await build_profile_response(user, store=store)
    assert profile.avatar_url is not None
    assert profile.avatar_url.startswith("mock://")
    assert key in profile.avatar_url


@pytest.mark.asyncio
async def test_build_profile_response_without_key_returns_none() -> None:
    user = _make_user(avatar_storage_key=None)
    store = MagicMock()
    store.generate_presigned_url = AsyncMock()
    profile = await build_profile_response(user, store=store)
    assert profile.avatar_url is None
    store.generate_presigned_url.assert_not_awaited()  # pas d'appel inutile


@pytest.mark.asyncio
async def test_build_profile_response_failsafe_on_presign_error() -> None:
    user = _make_user(avatar_storage_key="users/x/avatar.jpg")
    store = MagicMock()
    store.generate_presigned_url = AsyncMock(side_effect=RuntimeError("minio down"))
    # Ne lève PAS — dégrade en avatar_url None.
    profile = await build_profile_response(user, store=store)
    assert profile.avatar_url is None


# ══════════════════════════════════════════════════════════════
# delete_avatar_blob_best_effort
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_blob_delete_best_effort_noop_on_none() -> None:
    store = MagicMock()
    store.delete_object = AsyncMock()
    await delete_avatar_blob_best_effort(None, store=store)
    store.delete_object.assert_not_awaited()


@pytest.mark.asyncio
async def test_blob_delete_best_effort_failsafe_on_error() -> None:
    store = MagicMock()
    store.delete_object = AsyncMock(side_effect=RuntimeError("minio down"))
    # Ne lève PAS.
    await delete_avatar_blob_best_effort("users/x/avatar.jpg", store=store)
    store.delete_object.assert_awaited_once()
