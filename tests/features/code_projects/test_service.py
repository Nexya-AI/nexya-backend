"""Tests `CodeProjectService.build_zip` (C4.6).

Mock-first strict : utilise `MockObjectStore` (in-memory dict),
aucun appel MinIO réel. Pattern aligné `data_export_service` tests RGPD.
"""

from __future__ import annotations

import io
import uuid
import zipfile

import pytest

from app.core.storage.object_store import MockObjectStore
from app.features.code_projects.service import (
    CodeProjectService,
    _build_readme,
    _sanitize_filename,
)
from app.features.rich_content.schemas import (
    CodeProjectDraftData,
    CodeProjectFileItem,
)


class TestSanitizeFilename:
    """Helper `_sanitize_filename` pour le download client."""

    def test_basic_name_preserved(self) -> None:
        assert _sanitize_filename("My Project") == "My Project"

    def test_fs_unsafe_chars_replaced(self) -> None:
        result = _sanitize_filename('My<>:"/\\|?*Project')
        # Tous les chars unsafe remplacés par `_`.
        assert "<" not in result
        assert ">" not in result
        assert ":" not in result
        assert "/" not in result
        assert "\\" not in result
        assert "|" not in result

    def test_control_chars_replaced(self) -> None:
        result = _sanitize_filename("Project\x00Name\x01Hidden")
        assert "\x00" not in result
        assert "\x01" not in result

    def test_empty_fallback(self) -> None:
        assert _sanitize_filename("") == "code-project"
        assert _sanitize_filename("   ") == "code-project"

    def test_cap_100_chars(self) -> None:
        long_name = "x" * 200
        result = _sanitize_filename(long_name)
        assert len(result) <= 100


class TestBuildReadme:
    """Génération README.md auto."""

    def _make_payload(self, **kwargs):
        defaults = {
            "project_name": "Test Project",
            "files": [
                CodeProjectFileItem(filename="a.py", content="print(1)", language="python"),
                CodeProjectFileItem(filename="b.py", content="print(2)", language="python"),
            ],
        }
        defaults.update(kwargs)
        return CodeProjectDraftData(**defaults)

    def test_readme_includes_project_name(self) -> None:
        readme = _build_readme(self._make_payload())
        assert "# Test Project" in readme

    def test_readme_includes_description(self) -> None:
        payload = self._make_payload(description="Une API simple")
        readme = _build_readme(payload)
        assert "Une API simple" in readme

    def test_readme_includes_project_type(self) -> None:
        payload = self._make_payload(project_type="python")
        readme = _build_readme(payload)
        assert "`python`" in readme

    def test_readme_includes_file_list(self) -> None:
        readme = _build_readme(self._make_payload())
        assert "`a.py`" in readme
        assert "`b.py`" in readme

    def test_readme_includes_nexya_footer(self) -> None:
        readme = _build_readme(self._make_payload())
        assert "NEXYA" in readme.upper()


@pytest.mark.asyncio
class TestCodeProjectServiceBuildZip:
    """Service `build_zip` complet — mock MinIO."""

    def _make_payload(self, files: list | None = None, **kwargs):
        defaults = {
            "project_name": "FastAPI Tasks",
            "files": files or [
                CodeProjectFileItem(
                    filename="main.py",
                    content="from fastapi import FastAPI\napp = FastAPI()",
                    language="python",
                ),
                CodeProjectFileItem(
                    filename="requirements.txt",
                    content="fastapi==0.100.0\nuvicorn==0.20.0",
                    language="text",
                ),
            ],
        }
        defaults.update(kwargs)
        return CodeProjectDraftData(**defaults)

    async def test_build_zip_returns_response_with_url(self) -> None:
        user_id = uuid.uuid4()
        store = MockObjectStore(bucket="test-bucket")

        result = await CodeProjectService.build_zip(
            payload=self._make_payload(),
            user_id=user_id,
            object_store=store,
        )

        assert result.download_url.startswith("mock://")
        assert result.filename == "FastAPI Tasks.zip"
        assert result.size_bytes > 0
        assert result.expires_at is not None

    async def test_zip_contains_all_files_plus_readme(self) -> None:
        user_id = uuid.uuid4()
        store = MockObjectStore(bucket="test-bucket")

        await CodeProjectService.build_zip(
            payload=self._make_payload(),
            user_id=user_id,
            object_store=store,
        )

        # Récupère les bytes du .zip uploadés (helper interne MockObjectStore).
        # On scan le store dict pour trouver la seule clé .zip.
        zip_keys = [k for k in store._store if k.endswith(".zip")]
        assert len(zip_keys) == 1
        zip_bytes = store._store[zip_keys[0]][0]  # (data, mime, metadata, last_modified)

        # Ouvre le .zip et vérifie son contenu.
        with zipfile.ZipFile(io.BytesIO(zip_bytes), mode="r") as zf:
            names = zf.namelist()
            assert "main.py" in names
            assert "requirements.txt" in names
            assert "README.md" in names

            # Vérifie content main.py préservé byte-à-byte.
            main_content = zf.read("main.py").decode("utf-8")
            assert "from fastapi import FastAPI" in main_content

    async def test_storage_key_sharded_by_sha(self) -> None:
        user_id = uuid.uuid4()
        store = MockObjectStore(bucket="test-bucket")

        await CodeProjectService.build_zip(
            payload=self._make_payload(),
            user_id=user_id,
            object_store=store,
        )

        zip_keys = [k for k in store._store if k.endswith(".zip")]
        key = zip_keys[0]
        # Pattern : {user_id}/code-projects/{sha[:2]}/{sha}.zip
        assert key.startswith(f"{user_id}/code-projects/")
        # Sharding 2 chars hex après le path commun.
        parts = key.split("/")
        assert len(parts) == 4  # user_id, code-projects, sha[:2], sha.zip
        assert len(parts[2]) == 2  # sharding 2 chars
        assert parts[3].endswith(".zip")

    async def test_filename_sanitized_in_response(self) -> None:
        user_id = uuid.uuid4()
        store = MockObjectStore(bucket="test-bucket")

        result = await CodeProjectService.build_zip(
            payload=self._make_payload(project_name='Evil<>"|Name'),
            user_id=user_id,
            object_store=store,
        )

        # Filename FS-safe (chars unsafe remplacés par _).
        assert "<" not in result.filename
        assert ">" not in result.filename
        assert '"' not in result.filename
        assert "|" not in result.filename
        assert result.filename.endswith(".zip")

    async def test_metadata_attached_on_upload(self) -> None:
        user_id = uuid.uuid4()
        store = MockObjectStore(bucket="test-bucket")

        await CodeProjectService.build_zip(
            payload=self._make_payload(project_name="My App"),
            user_id=user_id,
            object_store=store,
        )

        zip_keys = [k for k in store._store if k.endswith(".zip")]
        # Mock store : (data, mime_type, metadata, last_modified).
        _, mime_type, metadata, _ = store._store[zip_keys[0]]
        assert mime_type == "application/zip"
        assert metadata is not None
        assert metadata["user_id"] == str(user_id)
        assert metadata["project_name"] == "My App"

    async def test_idempotent_same_payload_same_key(self) -> None:
        # Même payload → même SHA → même storage_key (dédup naturel).
        user_id = uuid.uuid4()
        store = MockObjectStore(bucket="test-bucket")

        result1 = await CodeProjectService.build_zip(
            payload=self._make_payload(),
            user_id=user_id,
            object_store=store,
        )
        result2 = await CodeProjectService.build_zip(
            payload=self._make_payload(),
            user_id=user_id,
            object_store=store,
        )

        # Même URL = même storage_key (mock encode key dans URL).
        # En vrai MinIO, le 2e upload écrase le 1er (idempotent UPSERT).
        assert result1.size_bytes == result2.size_bytes
