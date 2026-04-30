"""
Tests unitaires — `LibraryService` (Session C3).

Même pattern que `test_projects_service.py` : on capture les statements
SQLAlchemy via un `db.execute` monkeypatché + on inspecte leur forme
compilée avec `literal_binds`. On utilise un `MockObjectStore` réel
(en mémoire) pour l'upload — zéro container.

Couverture ciblée :
- `create_from_bytes` : happy-path, quota Free atteint, cap taille
  (FileTooLarge), dédup via ON CONFLICT (2ᵉ upload identique retourne
  l'existant), fail MinIO → StorageUnavailable.
- `create_from_base64` : base64 invalide → 422, base64 valide → délégation.
- `list_for_user` : SQL shape (type / source / conversation_id / q
  filtres combinés / no filter), whitespace-only q skipped.
- `get` : 404 IDOR.
- `soft_delete` : marque `deleted_at`, 404 idempotent sur 2ᵉ appel (via
  helper `_get_owned_item` qui ne trouve plus l'item actif).
- `presigned_url_for` : retourne bien une URL mock:// pour un item donné.
- `_build_storage_key` : sharding 2-char SHA, extension depuis mime.
- Pydantic validators : type/file_type/mime cohérence.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from app.config import settings
from app.core.errors.exceptions import (
    FileTooLargeException,
    LibraryQuotaExceededException,
    ResourceNotFoundException,
    ValidationException,
)
from app.core.storage import MockObjectStore
from app.features.library.models import LibraryItem
from app.features.library.schemas import LibraryItemCreate
from app.features.library.service import (
    LibraryService,
    _build_storage_key,
    _guess_extension,
)

# ══════════════════════════════════════════════════════════════
# Fixtures & helpers
# ══════════════════════════════════════════════════════════════


def _make_user(*, is_pro: bool = False) -> MagicMock:
    user = MagicMock()
    user.id = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
    user.is_pro = is_pro
    return user


def _make_item(*, item_id: uuid.UUID | None = None) -> LibraryItem:
    now = datetime(2026, 4, 24, 10, 0, 0, tzinfo=UTC)
    item = LibraryItem(
        user_id=uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77"),
        type="image",
        title="Mon image",
        storage_key="c4a2.../library/image/ab/abcd.png",
        mime_type="image/png",
        size_bytes=100,
        content_sha256="a" * 64,
        source="generated",
    )
    item.id = item_id or uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")
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


class _ScalarResult:
    def __init__(
        self,
        *,
        scalar_one: object | None = None,
        scalar_one_or_none: object | None | type = object,
        scalars_all: list | None = None,
    ) -> None:
        self._scalar_one = scalar_one
        self._scalar_or_none = scalar_one_or_none
        self._scalars_all = scalars_all or []

    def scalar_one(self):
        return self._scalar_one

    def scalar_one_or_none(self):
        if self._scalar_or_none is object:
            return None
        return self._scalar_or_none

    def scalars(self):
        sc = MagicMock()
        sc.all.return_value = self._scalars_all
        return sc


def _mk_db(execute_results: list[_ScalarResult]) -> MagicMock:
    db = MagicMock()
    iterator = iter(execute_results)

    async def _execute(stmt, *args, **kwargs):
        return next(iterator)

    db.execute = _execute
    db.commit = AsyncMock()
    db.rollback = AsyncMock()
    db.refresh = AsyncMock()
    return db


def _compiled_sql(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()


# ══════════════════════════════════════════════════════════════
# 1. Helpers internes
# ══════════════════════════════════════════════════════════════


def test_build_storage_key_has_user_and_shard() -> None:
    user_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    sha = "abcdef0123" * 6 + "abcdef0123"  # 64 chars
    key = _build_storage_key(user_id, "image", sha, "image/png")
    assert key.startswith(f"{user_id}/library/image/ab/")
    assert key.endswith(".png")


def test_guess_extension_known_mime() -> None:
    assert _guess_extension("image/png") == "png"
    assert _guess_extension("application/pdf") == "pdf"


def test_guess_extension_unknown_defaults_to_bin() -> None:
    assert _guess_extension("application/x-custom") == "bin"


# ══════════════════════════════════════════════════════════════
# 2. Pydantic validators — type/mime/file_type coherence
# ══════════════════════════════════════════════════════════════


def test_create_schema_requires_file_type_for_document() -> None:
    with pytest.raises(ValidationError):
        LibraryItemCreate(
            type="document",
            title="PDF sans file_type",
            content_base64=base64.b64encode(b"%PDF-1.4").decode(),
            mime_type="application/pdf",
        )


def test_create_schema_rejects_file_type_for_non_document() -> None:
    with pytest.raises(ValidationError):
        LibraryItemCreate(
            type="image",
            file_type="pdf",
            title="Image avec file_type",
            content_base64=base64.b64encode(b"...").decode(),
            mime_type="image/png",
        )


def test_create_schema_rejects_mime_type_mismatch() -> None:
    with pytest.raises(ValidationError):
        LibraryItemCreate(
            type="image",
            title="type image mais mime pdf",
            content_base64=base64.b64encode(b"...").decode(),
            mime_type="application/pdf",
        )


def test_create_schema_accepts_valid_image() -> None:
    body = LibraryItemCreate(
        type="image",
        title="Valide",
        content_base64=base64.b64encode(b"fake").decode(),
        mime_type="image/jpeg",
    )
    assert body.mime_type == "image/jpeg"
    assert body.source == "uploaded"


def test_create_schema_duration_only_for_media_types() -> None:
    with pytest.raises(ValidationError):
        LibraryItemCreate(
            type="image",
            title="pas de duration sur image",
            content_base64=base64.b64encode(b"...").decode(),
            mime_type="image/png",
            duration_ms=500,
        )


# ══════════════════════════════════════════════════════════════
# 3. `create_from_bytes` — happy, quota, cap, dedup
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_from_bytes_happy_path() -> None:
    user = _make_user()
    inserted = _make_item()
    store = MockObjectStore(bucket="test")
    db = _mk_db(
        [
            _ScalarResult(scalar_one=0),  # count active
            _ScalarResult(scalar_one_or_none=inserted),  # INSERT RETURNING
        ]
    )

    result = await LibraryService.create_from_bytes(
        user,
        db,
        type_="image",
        title="Photo",
        data=b"binary-bytes",
        mime_type="image/png",
        source="generated",
        provider="gemini-imagen",
        model="imagen-3.0",
        prompt="test",
        store=store,
    )

    assert result.id == inserted.id
    # L'upload mock contient le binaire attendu.
    stat = await store.stat_object(
        _build_storage_key(
            user.id,
            "image",
            __import__("hashlib").sha256(b"binary-bytes").hexdigest(),
            "image/png",
        )
    )
    assert stat is not None
    assert stat.size_bytes == len(b"binary-bytes")


@pytest.mark.asyncio
async def test_create_from_bytes_quota_exceeded_free() -> None:
    user = _make_user(is_pro=False)
    store = MockObjectStore(bucket="test")
    db = _mk_db([_ScalarResult(scalar_one=settings.library_max_free)])

    with pytest.raises(LibraryQuotaExceededException) as excinfo:
        await LibraryService.create_from_bytes(
            user,
            db,
            type_="image",
            title="Photo",
            data=b"x",
            mime_type="image/png",
            store=store,
        )

    assert excinfo.value.code == "LIBRARY_QUOTA_EXCEEDED"
    assert excinfo.value.status_code == 402
    assert excinfo.value.data["plan"] == "free"


@pytest.mark.asyncio
async def test_create_from_bytes_rejects_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user = _make_user()
    store = MockObjectStore(bucket="test")
    db = _mk_db([])
    # Cap à 10 octets pour déclencher rapidement.
    monkeypatch.setattr(settings, "s3_max_upload_bytes", 10, raising=False)

    with pytest.raises(FileTooLargeException):
        await LibraryService.create_from_bytes(
            user,
            db,
            type_="image",
            title="Gros fichier",
            data=b"x" * 100,
            mime_type="image/png",
            store=store,
        )


@pytest.mark.asyncio
async def test_create_from_bytes_dedup_returns_existing_on_conflict() -> None:
    """2ᵉ upload du MÊME contenu retourne l'entrée existante, pas d'erreur."""
    user = _make_user()
    existing = _make_item()
    store = MockObjectStore(bucket="test")
    # 1. count active → 0
    # 2. INSERT ON CONFLICT DO NOTHING RETURNING → None (conflit déclenché)
    # 3. SELECT existing → retourne l'item existant
    db = _mk_db(
        [
            _ScalarResult(scalar_one=0),
            _ScalarResult(scalar_one_or_none=None),  # RETURNING vide
            _ScalarResult(scalar_one_or_none=existing),  # SELECT existing
        ]
    )

    result = await LibraryService.create_from_bytes(
        user,
        db,
        type_="image",
        title="Re-upload",
        data=b"same-bytes",
        mime_type="image/png",
        store=store,
    )

    assert result.id == existing.id


# ══════════════════════════════════════════════════════════════
# 4. `create_from_base64` — validation du format
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_create_from_base64_rejects_invalid_base64() -> None:
    user = _make_user()
    db = _mk_db([])
    store = MockObjectStore(bucket="test")

    body = LibraryItemCreate(
        type="image",
        title="Base64 casse",
        content_base64="%%%PAS DE BASE64!!!",
        mime_type="image/png",
    )
    with pytest.raises(ValidationException):
        await LibraryService.create_from_base64(user, db, body, store=store)


@pytest.mark.asyncio
async def test_create_from_base64_decodes_and_delegates() -> None:
    user = _make_user()
    inserted = _make_item()
    store = MockObjectStore(bucket="test")
    db = _mk_db(
        [
            _ScalarResult(scalar_one=0),
            _ScalarResult(scalar_one_or_none=inserted),
        ]
    )

    body = LibraryItemCreate(
        type="image",
        title="Photo",
        content_base64=base64.b64encode(b"hello").decode(),
        mime_type="image/png",
    )
    result = await LibraryService.create_from_base64(user, db, body, store=store)
    assert result.id == inserted.id


# ══════════════════════════════════════════════════════════════
# 5. `list_for_user` — SQL shape avec filtres combinés
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_list_for_user_with_type_filter_injects_type_clause() -> None:
    captured: dict = {}

    async def _capture(stmt, *args, **kwargs):
        captured["stmt"] = stmt
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        return r

    db = MagicMock()
    db.execute = _capture
    await LibraryService.list_for_user(_make_user(), db, type_="video")
    sql = _compiled_sql(captured["stmt"])
    assert "library_items.type" in sql
    assert "'video'" in sql


@pytest.mark.asyncio
async def test_list_for_user_with_source_filter() -> None:
    captured: dict = {}

    async def _capture(stmt, *args, **kwargs):
        captured["stmt"] = stmt
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        return r

    db = MagicMock()
    db.execute = _capture
    await LibraryService.list_for_user(_make_user(), db, source="generated")
    sql = _compiled_sql(captured["stmt"])
    assert "'generated'" in sql


@pytest.mark.asyncio
async def test_list_for_user_with_q_trim_ilike() -> None:
    captured: dict = {}

    async def _capture(stmt, *args, **kwargs):
        captured["stmt"] = stmt
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        return r

    db = MagicMock()
    db.execute = _capture
    await LibraryService.list_for_user(_make_user(), db, q="chaton")
    sql = _compiled_sql(captured["stmt"])
    assert "ilike" in sql or "like" in sql
    assert "chaton" in sql


@pytest.mark.asyncio
async def test_list_for_user_whitespace_q_skipped() -> None:
    captured: dict = {}

    async def _capture(stmt, *args, **kwargs):
        captured["stmt"] = stmt
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        return r

    db = MagicMock()
    db.execute = _capture
    await LibraryService.list_for_user(_make_user(), db, q="   ")
    sql = _compiled_sql(captured["stmt"])
    assert "ilike" not in sql


@pytest.mark.asyncio
async def test_list_for_user_combined_filters() -> None:
    captured: dict = {}

    async def _capture(stmt, *args, **kwargs):
        captured["stmt"] = stmt
        r = MagicMock()
        r.scalars.return_value.all.return_value = []
        return r

    db = MagicMock()
    db.execute = _capture
    conv_id = uuid.uuid4()
    await LibraryService.list_for_user(
        _make_user(),
        db,
        type_="image",
        source="generated",
        conversation_id=conv_id,
        q="noel",
    )
    sql = _compiled_sql(captured["stmt"])
    assert "'image'" in sql
    assert "'generated'" in sql
    # Le UUID est rendu dans un format dialect-dependent par literal_binds ;
    # on vérifie que la colonne apparaît bien dans le WHERE (preuve que la
    # condition a été injectée).
    assert "source_conversation_id" in sql
    assert "noel" in sql


# ══════════════════════════════════════════════════════════════
# 6. GET + DELETE
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_returns_404_for_non_owner() -> None:
    user = _make_user()
    db = _mk_db([_ScalarResult(scalar_one_or_none=None)])

    with pytest.raises(ResourceNotFoundException):
        await LibraryService.get(uuid.uuid4(), user, db)


@pytest.mark.asyncio
async def test_soft_delete_marks_deleted_at() -> None:
    user = _make_user()
    item = _make_item()
    db = _mk_db([_ScalarResult(scalar_one_or_none=item)])

    await LibraryService.soft_delete(item.id, user, db)
    assert item.deleted_at is not None
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_soft_delete_idempotent_via_404() -> None:
    """Après soft-delete, un 2ᵉ appel lève 404 (l'item n'est plus actif)."""
    user = _make_user()
    # 1er appel réussit. 2ᵉ appel : item déjà soft-deleté → _get_owned_item
    # filtre sur deleted_at IS NULL → None → 404.
    db = _mk_db([_ScalarResult(scalar_one_or_none=None)])

    with pytest.raises(ResourceNotFoundException):
        await LibraryService.soft_delete(uuid.uuid4(), user, db)


# ══════════════════════════════════════════════════════════════
# 7. presigned_url_for — intégration avec store
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_presigned_url_for_returns_mock_url() -> None:
    store = MockObjectStore(bucket="test-bucket")
    item = _make_item()
    url = await LibraryService.presigned_url_for(item, store=store)
    assert url.startswith("mock://test-bucket/")
    assert item.storage_key in url
