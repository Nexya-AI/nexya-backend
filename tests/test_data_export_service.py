"""J1 — DataExportService unit tests (~13 tests)."""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.features.rgpd.data_export_service import (
    DataExportService,
    _anonymize_ip,
    _mask_device_token,
    _row_to_dict,
)

# ── Helpers de fixture ───────────────────────────────────────────


class _ScalarResult:
    def __init__(self, *, all_rows=None):
        self._all = all_rows or []

    def scalars(self):
        return self

    def all(self):
        return self._all


def _mk_db(scalar_lists: list[list]) -> MagicMock:
    """`scalar_lists` = liste de listes de rows à yielder pour chaque
    SELECT exécuté."""
    db = MagicMock()
    results = [_ScalarResult(all_rows=rows) for rows in scalar_lists]
    db.execute = AsyncMock(side_effect=results)
    return db


def _mk_user() -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    user.__table__ = MagicMock()
    user.__table__.columns = []
    return user


def _mk_object_store() -> MagicMock:
    store = MagicMock()
    store.generate_presigned_url = AsyncMock(
        side_effect=lambda key, ttl_seconds=None, method="GET": (
            f"mock://bucket/{key}?expires={ttl_seconds}"
        )
    )
    return store


def _mk_columns(*names: str) -> list:
    """Crée une liste de mock columns avec `.name` réellement string.

    Nécessaire car `MagicMock(name="id")` interprète `name` comme nom
    de l'instance, pas comme attribut .name (piège classique).
    """
    cols = []
    for n in names:
        col = MagicMock()
        col.name = n
        cols.append(col)
    return cols


def _mk_row(**fields):
    """Crée un mock row ORM avec `__table__.columns` et attributs."""
    row = MagicMock()
    row.__table__ = MagicMock()
    row.__table__.columns = _mk_columns(*fields.keys())
    for k, v in fields.items():
        setattr(row, k, v)
    return row


# ── Helpers tests ────────────────────────────────────────────────


def test_anonymize_ip_ipv4():
    assert _anonymize_ip("192.168.1.42") == "192.168.1.0/24"


def test_anonymize_ip_ipv6():
    assert _anonymize_ip("2001:db8::abcd") == "2001:db8::/48"


def test_anonymize_ip_invalid_returns_none():
    assert _anonymize_ip(None) is None
    assert _anonymize_ip("") is None
    assert _anonymize_ip("not-an-ip") is None


def test_mask_device_token_short():
    assert _mask_device_token("abc") == "***"


def test_mask_device_token_long():
    masked = _mask_device_token("abcdefgh12345678")
    assert masked == "***12345678"


# ── Tests build_export ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_export_empty_user_produces_valid_zip(monkeypatch):
    """Un user sans données doit produire un ZIP valide minimal."""
    user = MagicMock()
    user.id = uuid.uuid4()
    user.email = "test@nexya.ai"
    user.username = "test"
    user.password_hash = "secret"
    user.deleted_at = None
    user.created_at = datetime.now(UTC)
    user.is_active = True

    # Mock __table__.columns pour _row_to_dict
    columns = []
    for name in ("id", "email", "username", "password_hash", "is_active"):
        col = MagicMock()
        col.name = name
        columns.append(col)
    user.__table__ = MagicMock()
    user.__table__.columns = columns

    # Patch ConsentService et 17 SELECT vides (chaque table user-scope)
    monkeypatch.setattr(
        "app.features.rgpd.data_export_service.ConsentService.list_history_for_user",
        AsyncMock(return_value=[]),
    )
    db = _mk_db([[]] * 17)

    service = DataExportService(object_store=_mk_object_store())
    result = await service.build_export(user, db)

    assert result.zip_bytes
    # ZIP valide ?
    with zipfile.ZipFile(io.BytesIO(result.zip_bytes), mode="r") as zf:
        names = set(zf.namelist())
        assert "manifest.json" in names
        assert "README.txt" in names
        assert "users.json" in names
        assert "consents.json" in names
        # users.json ne contient pas password_hash
        users_data = json.loads(zf.read("users.json").decode("utf-8"))
        assert "password_hash" not in users_data
        assert users_data["email"] == "test@nexya.ai"


@pytest.mark.asyncio
async def test_build_export_zip_contains_required_paths(monkeypatch):
    user = _mk_row(id=uuid.uuid4(), password_hash="x")

    monkeypatch.setattr(
        "app.features.rgpd.data_export_service.ConsentService.list_history_for_user",
        AsyncMock(return_value=[]),
    )
    db = _mk_db([[]] * 17)
    service = DataExportService(object_store=_mk_object_store())
    result = await service.build_export(user, db)

    with zipfile.ZipFile(io.BytesIO(result.zip_bytes), mode="r") as zf:
        names = set(zf.namelist())

    expected_paths = {
        "manifest.json",
        "README.txt",
        "users.json",
        "consents.json",
        "deletion_requests.json",
        "auth_events.json",
        "device_tokens.json",
        "chat/conversations.json",
        "chat/messages.json",
        "chat/abuse_reports.json",
        "projects/projects.json",
        "projects/files.json",
        "library/items.json",
        "library/blob_urls.json",
        "memory/memories.json",
        "notifications/notifications.json",
        "notifications/preferences.json",
        "planner/tasks.json",
        "planner/results.json",
        "files/uploaded.json",
        "files/chunks.json",
        "files/blob_urls.json",
        "voice/transcriptions.json",
        "vision/analyses.json",
        "ai_calls/ai_calls.json",
    }
    missing = expected_paths - names
    assert not missing, f"Missing paths in export ZIP: {missing}"


@pytest.mark.asyncio
async def test_build_export_manifest_record_counts(monkeypatch):
    user = _mk_row(id=uuid.uuid4(), password_hash="x")

    monkeypatch.setattr(
        "app.features.rgpd.data_export_service.ConsentService.list_history_for_user",
        AsyncMock(return_value=[]),
    )
    db = _mk_db([[]] * 17)
    service = DataExportService(object_store=_mk_object_store())
    result = await service.build_export(user, db)

    with zipfile.ZipFile(io.BytesIO(result.zip_bytes), mode="r") as zf:
        manifest = json.loads(zf.read("manifest.json").decode("utf-8"))

    assert manifest["user_id"] == str(user.id)
    assert "exported_at" in manifest
    assert manifest["schema_version"] == "1.0"
    assert manifest["record_counts"]["users"] == 1
    assert manifest["record_counts"]["consents"] == 0
    assert manifest["truncated"] is False


@pytest.mark.asyncio
async def test_build_export_readme_french(monkeypatch):
    user = _mk_row(id=uuid.uuid4(), password_hash="x")

    monkeypatch.setattr(
        "app.features.rgpd.data_export_service.ConsentService.list_history_for_user",
        AsyncMock(return_value=[]),
    )
    db = _mk_db([[]] * 17)
    service = DataExportService(object_store=_mk_object_store())
    result = await service.build_export(user, db)

    with zipfile.ZipFile(io.BytesIO(result.zip_bytes), mode="r") as zf:
        readme = zf.read("README.txt").decode("utf-8")

    assert "RGPD" in readme
    assert "Article" in readme
    assert "données personnelles" in readme.lower() or "donnees" in readme.lower()


@pytest.mark.asyncio
async def test_build_export_never_leaks_password_hash(monkeypatch):
    user = _mk_row(
        id=uuid.uuid4(),
        email="leak@test.ai",
        password_hash="BCRYPT_HASH_SECRET_VALUE",
    )

    monkeypatch.setattr(
        "app.features.rgpd.data_export_service.ConsentService.list_history_for_user",
        AsyncMock(return_value=[]),
    )
    db = _mk_db([[]] * 17)
    service = DataExportService(object_store=_mk_object_store())
    result = await service.build_export(user, db)

    # password_hash ne doit apparaître nulle part dans le ZIP
    assert b"BCRYPT_HASH_SECRET_VALUE" not in result.zip_bytes


@pytest.mark.asyncio
async def test_build_export_empty_ai_calls_file_present(monkeypatch):
    """Même 0 ai_calls → fichier présent (pas crash)."""
    user = _mk_row(id=uuid.uuid4(), password_hash="x")

    monkeypatch.setattr(
        "app.features.rgpd.data_export_service.ConsentService.list_history_for_user",
        AsyncMock(return_value=[]),
    )
    db = _mk_db([[]] * 17)
    service = DataExportService(object_store=_mk_object_store())
    result = await service.build_export(user, db)

    with zipfile.ZipFile(io.BytesIO(result.zip_bytes), mode="r") as zf:
        ai_calls = json.loads(zf.read("ai_calls/ai_calls.json").decode("utf-8"))
    assert ai_calls == []


@pytest.mark.asyncio
async def test_build_export_truncated_flag_when_over_cap(monkeypatch):
    """Si la taille dépasse le cap, manifest porte truncated=True."""
    from app.config import settings as global_settings

    user = _mk_row(id=uuid.uuid4(), password_hash="x")

    monkeypatch.setattr(
        "app.features.rgpd.data_export_service.ConsentService.list_history_for_user",
        AsyncMock(return_value=[]),
    )
    # On force un cap ridicule (1 byte) pour déclencher le flag
    monkeypatch.setattr(global_settings, "rgpd_export_max_size_bytes", 1)
    db = _mk_db([[]] * 17)
    service = DataExportService(object_store=_mk_object_store())
    result = await service.build_export(user, db)
    assert result.truncated is True
    assert result.truncated_reason is not None


@pytest.mark.asyncio
async def test_build_export_anonymizes_ip_in_auth_events(monkeypatch):
    user = _mk_row(id=uuid.uuid4(), password_hash="x")

    event = _mk_row(
        id=uuid.uuid4(),
        user_id=user.id,
        event_type="login_success",
        ip="192.168.1.42",
    )

    monkeypatch.setattr(
        "app.features.rgpd.data_export_service.ConsentService.list_history_for_user",
        AsyncMock(return_value=[]),
    )
    # Ordre des SELECTs dans build_export : deletion_requests, auth_events, ...
    db = _mk_db(
        [
            [],  # deletion_requests
            [event],  # auth_events
            *([[]] * 15),
        ]
    )
    service = DataExportService(object_store=_mk_object_store())
    result = await service.build_export(user, db)

    with zipfile.ZipFile(io.BytesIO(result.zip_bytes), mode="r") as zf:
        events = json.loads(zf.read("auth_events.json").decode("utf-8"))

    assert len(events) == 1
    assert events[0]["ip"] == "192.168.1.0/24"
    # Vérifie que l'IP brute n'apparaît plus dans le ZIP entier
    assert b"192.168.1.42" not in result.zip_bytes


@pytest.mark.asyncio
async def test_build_export_record_counts_match_actual(monkeypatch):
    """record_counts manifest doivent être cohérents avec les listes."""
    user = _mk_row(id=uuid.uuid4(), password_hash="x")

    # 3 fake consents
    consents = [_mk_row(id=uuid.uuid4()) for _ in range(3)]

    monkeypatch.setattr(
        "app.features.rgpd.data_export_service.ConsentService.list_history_for_user",
        AsyncMock(return_value=consents),
    )
    db = _mk_db([[]] * 17)
    service = DataExportService(object_store=_mk_object_store())
    result = await service.build_export(user, db)

    assert result.record_counts["consents"] == 3
    assert result.record_counts["users"] == 1
    assert result.record_counts["messages"] == 0


@pytest.mark.asyncio
async def test_row_to_dict_excludes_redacted_fields():
    row = _mk_row(
        id=uuid.uuid4(),
        email="a@b.c",
        password_hash="BCRYPT_SECRET",
    )
    d = _row_to_dict(row, redact={"password_hash"})
    assert "email" in d
    assert "password_hash" not in d


@pytest.mark.asyncio
async def test_build_export_presigned_urls_use_settings_ttl(monkeypatch):
    user = _mk_row(id=uuid.uuid4(), password_hash="x")

    monkeypatch.setattr(
        "app.features.rgpd.data_export_service.ConsentService.list_history_for_user",
        AsyncMock(return_value=[]),
    )
    # 1 library_item avec storage_key
    li = _mk_row(id=uuid.uuid4(), storage_key="user/library/img.png")

    # Ordre : del_req, auth_events, device_tokens, conversations, messages,
    # abuse_reports, projects, project_files, library_items, ...
    db = _mk_db(
        [
            [],  # deletion_requests
            [],  # auth_events
            [],  # device_tokens
            [],  # conversations
            [],  # abuse_reports
            [],  # projects
            [li],  # library_items
            *([[]] * 10),
        ]
    )
    store = _mk_object_store()
    service = DataExportService(object_store=store)
    await service.build_export(user, db)

    # presigned URL appelée avec ttl_seconds = settings.rgpd_blob_presigned_ttl_seconds
    from app.config import settings as global_settings

    store.generate_presigned_url.assert_any_call(
        "user/library/img.png",
        ttl_seconds=global_settings.rgpd_blob_presigned_ttl_seconds,
    )


@pytest.mark.asyncio
async def test_build_export_presign_failure_continues_gracefully(monkeypatch):
    """Si presign échoue → URL=None mais l'export ne crash pas."""
    user = _mk_row(id=uuid.uuid4(), password_hash="x")

    monkeypatch.setattr(
        "app.features.rgpd.data_export_service.ConsentService.list_history_for_user",
        AsyncMock(return_value=[]),
    )
    li = _mk_row(id=uuid.uuid4(), storage_key="broken/key")

    db = _mk_db(
        [
            [],
            [],
            [],
            [],
            [],
            [],
            [li],  # library
            *([[]] * 10),
        ]
    )
    store = MagicMock()
    store.generate_presigned_url = AsyncMock(side_effect=RuntimeError("boom"))
    service = DataExportService(object_store=store)
    result = await service.build_export(user, db)

    with zipfile.ZipFile(io.BytesIO(result.zip_bytes), mode="r") as zf:
        urls = json.loads(zf.read("library/blob_urls.json").decode("utf-8"))
    assert len(urls) == 1
    assert urls[0]["url"] is None
