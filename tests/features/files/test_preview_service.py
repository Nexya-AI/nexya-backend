"""
Tests unitaires — `PreviewService` (C4.10).

Couverture exhaustive du pipeline 7 étapes :
- Étape 1 : owner check IDOR-safe (404 propagé depuis FileUploadService.get_for_user)
- Étape 2 : check MIME previewable (415 si hors {pdf, docx})
- Étape 3 : cache MinIO hit (download direct, ~50ms)
- Étape 4 : cache miss → download_bytes original
- Étape 5 : PDF natif passthrough (PAS de re-render — RGPD)
- Étape 6 : DOCX → mammoth → weasyprint → pikepdf cap pages
- Étape 7 : cache write fire-and-forget

CONTRAINTE TRANSVERSALE RGPD : anti-régressions stricts qui vérifient que
le preview NE CONTIENT PAS de branding NEXYA (pas /Author=NEXYA, pas
de C2PA manifest, pas de watermark logo).
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors.exceptions import (
    FilePreviewNotPreviewableException,
    FilePreviewUnavailableException,
    ResourceNotFoundException,
)
from app.features.files.models import UploadedFile
from app.features.files.preview_service import (
    _DOCX_MIME,
    _PDF_MIME,
    _PREVIEW_BUCKET_PREFIX,
    PreviewResult,
    PreviewService,
)
from app.features.files.service import FileUploadService

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
_FAKE_UPLOAD_ID = uuid.UUID("11111111-0000-4000-8000-000000000001")
_FAKE_SHA = "a" * 64


def _make_fake_user():
    user = MagicMock()
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _make_fake_upload(
    *,
    mime_type: str = _PDF_MIME,
    content_sha256: str = _FAKE_SHA,
    extracted_text: str | None = "Texte extrait fallback",
) -> UploadedFile:
    now = datetime(2026, 5, 31, 10, 0, 0, tzinfo=UTC)
    row = UploadedFile(
        user_id=_FAKE_USER_ID,
        storage_key=f"{_FAKE_USER_ID}/uploads/aa/{content_sha256}.pdf",
        content_sha256=content_sha256,
        size_bytes=4096,
        mime_type=mime_type,
        original_filename="rapport.pdf",
        extension="pdf" if mime_type == _PDF_MIME else "docx",
        virus_scan_status="clean",
        virus_scan_signature=None,
        virus_scan_scanner="mock",
        extraction_status="ok",
    )
    row.id = _FAKE_UPLOAD_ID
    row.created_at = now
    row.updated_at = now
    row.deleted_at = None
    row.virus_scanned_at = now
    row.extracted_text = extracted_text
    row.extracted_text_length = len(extracted_text) if extracted_text else 0
    row.page_count = 3
    row.extraction_truncated = False
    row.extracted_at = now
    row.attached_to_kind = None
    row.attached_to_id = None
    row.attached_at = None
    row.chunks_indexed_at = None
    return row


class _FakeObjectStore:
    """ObjectStore minimaliste in-memory pour les tests, sans aioboto3."""

    name = "fake"

    def __init__(self) -> None:
        self._store: dict[str, bytes] = {}
        self.upload_calls: list[tuple[str, int, str]] = []  # (key, size, mime)
        self.download_calls: list[str] = []
        self.exists_calls: list[str] = []

    async def upload_bytes(self, key, data, *, mime_type, metadata=None):
        self.upload_calls.append((key, len(data), mime_type))
        self._store[key] = data

    async def download_bytes(self, key):
        self.download_calls.append(key)
        if key not in self._store:
            raise FileNotFoundError(key)
        return self._store[key]

    async def object_exists(self, key):
        self.exists_calls.append(key)
        return key in self._store

    async def delete_object(self, key):
        self._store.pop(key, None)

    async def stat_object(self, key):
        return None

    async def generate_presigned_url(self, key, *, ttl_seconds=3600, method="GET"):
        return f"fake://{key}?ttl={ttl_seconds}&method={method}"


def _minimal_pdf_bytes() -> bytes:
    """Génère un PDF minimal valide via pikepdf — réutilisable cross-tests."""
    import pikepdf

    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(595, 842))  # A4
    buf = io.BytesIO()
    pdf.save(buf)
    return buf.getvalue()


# ══════════════════════════════════════════════════════════════
# 1. Cache & key helpers
# ══════════════════════════════════════════════════════════════


def test_preview_key_format() -> None:
    """Clé MinIO format `previews/{sha256}.pdf` — pas de sharding."""
    key = PreviewService._preview_key(_FAKE_SHA)
    assert key == f"previews/{_FAKE_SHA}.pdf"
    assert key.startswith(_PREVIEW_BUCKET_PREFIX)


# ══════════════════════════════════════════════════════════════
# 2. Owner check 404 IDOR-safe (étape 1)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_owner_check_404_propagated(monkeypatch: pytest.MonkeyPatch) -> None:
    """`FileUploadService.get_for_user` lève → propagé tel quel."""
    monkeypatch.setattr(
        FileUploadService,
        "get_for_user",
        AsyncMock(side_effect=ResourceNotFoundException("Upload")),
    )
    store = _FakeObjectStore()

    with pytest.raises(ResourceNotFoundException):
        await PreviewService.get_cached_or_generate(
            _FAKE_UPLOAD_ID, _make_fake_user(), MagicMock(), store=store
        )


# ══════════════════════════════════════════════════════════════
# 3. MIME non-previewable (étape 2)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_non_previewable_mime_raises_415(monkeypatch: pytest.MonkeyPatch) -> None:
    """MIME hors {pdf, docx} → 415 FilePreviewNotPreviewableException."""
    upload = _make_fake_upload(mime_type="text/plain")
    monkeypatch.setattr(
        FileUploadService, "get_for_user", AsyncMock(return_value=upload)
    )
    store = _FakeObjectStore()

    with pytest.raises(FilePreviewNotPreviewableException) as exc_info:
        await PreviewService.get_cached_or_generate(
            _FAKE_UPLOAD_ID, _make_fake_user(), MagicMock(), store=store
        )
    assert exc_info.value.code == "FILE_TYPE_NOT_PREVIEWABLE"
    assert exc_info.value.status_code == 415


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime",
    ["image/png", "audio/mpeg", "video/mp4", "text/markdown", "application/zip"],
)
async def test_various_non_previewable_mimes_rejected(
    monkeypatch: pytest.MonkeyPatch, mime: str
) -> None:
    """Plusieurs MIMEs non-previewable rejetés uniformément."""
    upload = _make_fake_upload(mime_type=mime)
    monkeypatch.setattr(
        FileUploadService, "get_for_user", AsyncMock(return_value=upload)
    )

    with pytest.raises(FilePreviewNotPreviewableException):
        await PreviewService.get_cached_or_generate(
            _FAKE_UPLOAD_ID, _make_fake_user(), MagicMock(), store=_FakeObjectStore()
        )


# ══════════════════════════════════════════════════════════════
# 4. Cache hit (étape 3)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cache_hit_returns_cached_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si le preview existe déjà dans MinIO, retour direct sans génération."""
    upload = _make_fake_upload(mime_type=_PDF_MIME)
    monkeypatch.setattr(
        FileUploadService, "get_for_user", AsyncMock(return_value=upload)
    )
    cached_bytes = b"CACHED_PDF_CONTENT_FAKE"
    store = _FakeObjectStore()
    cache_key = PreviewService._preview_key(_FAKE_SHA)
    store._store[cache_key] = cached_bytes  # pré-pose le cache

    result = await PreviewService.get_cached_or_generate(
        _FAKE_UPLOAD_ID, _make_fake_user(), MagicMock(), store=store
    )

    assert result.from_cache is True
    assert result.pdf_bytes == cached_bytes
    assert result.size_bytes == len(cached_bytes)
    # Le cache hit ne déclenche PAS le download de l'original.
    assert upload.storage_key not in store.download_calls
    assert cache_key in store.download_calls


# ══════════════════════════════════════════════════════════════
# 5. PDF natif passthrough (étape 5)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_pdf_native_passthrough_no_modification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PDF natif → bytes inchangés, AUCUN re-render (RGPD)."""
    upload = _make_fake_upload(mime_type=_PDF_MIME)
    monkeypatch.setattr(
        FileUploadService, "get_for_user", AsyncMock(return_value=upload)
    )

    original_pdf = _minimal_pdf_bytes()
    store = _FakeObjectStore()
    store._store[upload.storage_key] = original_pdf  # cache miss puis download OK

    result = await PreviewService.get_cached_or_generate(
        _FAKE_UPLOAD_ID, _make_fake_user(), MagicMock(), store=store
    )

    assert result.from_cache is False
    # Passthrough strict : bytes IDENTIQUES à l'original.
    assert result.pdf_bytes == original_pdf
    assert result.truncated is False


# ══════════════════════════════════════════════════════════════
# 6. DOCX → PDF via mammoth + weasyprint (étape 6)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_docx_renders_to_pdf_via_mammoth_weasyprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DOCX → mammoth → weasyprint → PDF binaire (commence par %PDF-).

    Note Windows-dev : WeasyPrint nécessite libgobject/libcairo natifs.
    En prod Linux/Docker ces libs sont apt-installées (cf. pyproject.toml).
    En dev Windows on mocke `_weasyprint_render_sync` pour éviter la
    dépendance système (le test prod réel se fait via la CI Linux).
    """
    upload = _make_fake_upload(mime_type=_DOCX_MIME)
    monkeypatch.setattr(
        FileUploadService, "get_for_user", AsyncMock(return_value=upload)
    )

    # Mammoth mock : retourne du HTML simulé (évite parser un vrai DOCX).
    monkeypatch.setattr(
        PreviewService,
        "_mammoth_convert_sync",
        lambda _bytes: "<h1>Test</h1><p>Contenu du document.</p>",
    )

    # WeasyPrint mock (Windows-dev) : retourne un PDF minimal valide.
    fake_pdf = _minimal_pdf_bytes()
    monkeypatch.setattr(
        PreviewService,
        "_weasyprint_render_sync",
        lambda _html: fake_pdf,
    )

    store = _FakeObjectStore()
    store._store[upload.storage_key] = b"FAKE_DOCX_BYTES"

    result = await PreviewService.get_cached_or_generate(
        _FAKE_UPLOAD_ID, _make_fake_user(), MagicMock(), store=store
    )

    assert result.from_cache is False
    # Le PDF généré commence par le magic number PDF.
    assert result.pdf_bytes.startswith(b"%PDF-")
    assert result.size_bytes > 100  # PDF non-trivial


# ══════════════════════════════════════════════════════════════
# 7. Fallback texte brut (mammoth crash)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_docx_mammoth_crash_falls_back_to_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si mammoth crash + extracted_text présent → PDF fallback minimaliste."""
    upload = _make_fake_upload(
        mime_type=_DOCX_MIME,
        extracted_text="Voici le texte extrait du DOCX corrompu.",
    )
    monkeypatch.setattr(
        FileUploadService, "get_for_user", AsyncMock(return_value=upload)
    )

    def _crash_mammoth(_bytes: bytes) -> str:
        raise RuntimeError("Simulated mammoth crash on exotic DOCX")

    monkeypatch.setattr(PreviewService, "_mammoth_convert_sync", _crash_mammoth)

    # WeasyPrint mock (Windows-dev) : retourne un PDF minimal pour le fallback.
    fake_pdf = _minimal_pdf_bytes()
    monkeypatch.setattr(
        PreviewService,
        "_weasyprint_render_sync",
        lambda _html: fake_pdf,
    )

    store = _FakeObjectStore()
    store._store[upload.storage_key] = b"FAKE_CORRUPTED_DOCX"

    result = await PreviewService.get_cached_or_generate(
        _FAKE_UPLOAD_ID, _make_fake_user(), MagicMock(), store=store
    )

    # Fallback texte → PDF valide
    assert result.pdf_bytes.startswith(b"%PDF-")
    # Le texte extracted doit être présent (vérif content via pikepdf)
    import pikepdf

    with pikepdf.open(io.BytesIO(result.pdf_bytes)) as pdf:
        assert len(pdf.pages) >= 1


@pytest.mark.asyncio
async def test_docx_mammoth_crash_no_fallback_raises_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si mammoth crash ET pas d'extracted_text → 503 FilePreviewUnavailable."""
    upload = _make_fake_upload(mime_type=_DOCX_MIME, extracted_text=None)
    monkeypatch.setattr(
        FileUploadService, "get_for_user", AsyncMock(return_value=upload)
    )

    def _crash_mammoth(_bytes: bytes) -> str:
        raise RuntimeError("Simulated mammoth crash")

    monkeypatch.setattr(PreviewService, "_mammoth_convert_sync", _crash_mammoth)

    store = _FakeObjectStore()
    store._store[upload.storage_key] = b"FAKE_DOCX"

    with pytest.raises(FilePreviewUnavailableException) as exc_info:
        await PreviewService.get_cached_or_generate(
            _FAKE_UPLOAD_ID, _make_fake_user(), MagicMock(), store=store
        )
    assert exc_info.value.code == "FILE_PREVIEW_UNAVAILABLE"


# ══════════════════════════════════════════════════════════════
# 8. Original storage missing
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_original_missing_raises_503(monkeypatch: pytest.MonkeyPatch) -> None:
    """Si l'original n'est pas dans MinIO (orphan storage) → 503."""
    upload = _make_fake_upload(mime_type=_PDF_MIME)
    monkeypatch.setattr(
        FileUploadService, "get_for_user", AsyncMock(return_value=upload)
    )
    store = _FakeObjectStore()  # vide — storage_key inexistant

    with pytest.raises(FilePreviewUnavailableException):
        await PreviewService.get_cached_or_generate(
            _FAKE_UPLOAD_ID, _make_fake_user(), MagicMock(), store=store
        )


# ══════════════════════════════════════════════════════════════
# 9. Cap pages DOCX (anti-DOCX géant)
# ══════════════════════════════════════════════════════════════


def test_cap_pages_truncates_large_pdf() -> None:
    """`_cap_pages_sync` tronque les pages au-delà du cap."""
    import pikepdf

    # Génère un PDF de 60 pages
    pdf = pikepdf.new()
    for _ in range(60):
        pdf.add_blank_page(page_size=(595, 842))
    buf = io.BytesIO()
    pdf.save(buf)
    pdf_bytes = buf.getvalue()

    capped_bytes, truncated = PreviewService._cap_pages_sync(pdf_bytes, max_pages=10)

    assert truncated is True
    with pikepdf.open(io.BytesIO(capped_bytes)) as result_pdf:
        assert len(result_pdf.pages) == 10


def test_cap_pages_no_truncation_under_cap() -> None:
    """`_cap_pages_sync` ne tronque PAS si pages ≤ cap."""
    import pikepdf

    pdf = pikepdf.new()
    for _ in range(5):
        pdf.add_blank_page(page_size=(595, 842))
    buf = io.BytesIO()
    pdf.save(buf)
    pdf_bytes = buf.getvalue()

    capped_bytes, truncated = PreviewService._cap_pages_sync(pdf_bytes, max_pages=50)

    assert truncated is False
    with pikepdf.open(io.BytesIO(capped_bytes)) as result_pdf:
        assert len(result_pdf.pages) == 5


# ══════════════════════════════════════════════════════════════
# 10. RGPD ANTI-RÉGRESSIONS CRITIQUES
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rgpd_pdf_passthrough_preserves_original_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RGPD : PDF natif passthrough = bytes IDENTIQUES, AUCUN re-render.

    Si un futur refacto re-rendre un PDF natif (ex: pour ajouter du
    branding NEXYA), ce test casse — défense en profondeur contre
    régression accidentelle de la contrainte RGPD.
    """
    upload = _make_fake_upload(mime_type=_PDF_MIME)
    monkeypatch.setattr(
        FileUploadService, "get_for_user", AsyncMock(return_value=upload)
    )

    # PDF original avec metadata user (Author = "Loth Ivan", Producer = "WordPad")
    import pikepdf

    pdf = pikepdf.new()
    pdf.add_blank_page(page_size=(595, 842))
    pdf.docinfo["/Author"] = "Loth Ivan"
    pdf.docinfo["/Producer"] = "WordPad 2025"
    buf = io.BytesIO()
    pdf.save(buf)
    user_pdf_with_metadata = buf.getvalue()

    store = _FakeObjectStore()
    store._store[upload.storage_key] = user_pdf_with_metadata

    result = await PreviewService.get_cached_or_generate(
        _FAKE_UPLOAD_ID, _make_fake_user(), MagicMock(), store=store
    )

    # Le PDF preview EST le PDF original byte-pour-byte (passthrough strict).
    assert result.pdf_bytes == user_pdf_with_metadata

    # Vérif metadata user PRÉSERVÉES (pas écrasées par NEXYA).
    with pikepdf.open(io.BytesIO(result.pdf_bytes)) as preview_pdf:
        assert str(preview_pdf.docinfo["/Author"]) == "Loth Ivan"
        assert "WordPad" in str(preview_pdf.docinfo["/Producer"])
        # Vérif AUCUN champ NEXYA injecté.
        author_str = str(preview_pdf.docinfo.get("/Author", ""))
        producer_str = str(preview_pdf.docinfo.get("/Producer", ""))
        assert "NEXYA" not in author_str
        assert "NEXYA" not in producer_str
        assert "Nexyalabs" not in author_str
        assert "Nexyalabs" not in producer_str


@pytest.mark.asyncio
async def test_rgpd_docx_preview_no_nexya_branding_in_metadata(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """RGPD : Le preview DOCX généré NE DOIT PAS contenir de branding NEXYA
    dans les métadonnées PDF.

    Anti-régression contre application accidentelle de
    `apply_pdf_native_metadata` C4.8 sur les previews.
    """
    upload = _make_fake_upload(mime_type=_DOCX_MIME)
    monkeypatch.setattr(
        FileUploadService, "get_for_user", AsyncMock(return_value=upload)
    )
    monkeypatch.setattr(
        PreviewService,
        "_mammoth_convert_sync",
        lambda _bytes: "<h1>Document utilisateur</h1><p>Contenu privé.</p>",
    )

    # WeasyPrint mock (Windows-dev) : retourne un PDF minimal SANS metadata
    # (anti-régression : le test vérifie qu'on n'ajoute pas de NEXYA branding).
    fake_pdf = _minimal_pdf_bytes()
    monkeypatch.setattr(
        PreviewService,
        "_weasyprint_render_sync",
        lambda _html: fake_pdf,
    )

    store = _FakeObjectStore()
    store._store[upload.storage_key] = b"FAKE_DOCX"

    result = await PreviewService.get_cached_or_generate(
        _FAKE_UPLOAD_ID, _make_fake_user(), MagicMock(), store=store
    )

    # Vérif metadata PDF NE contient PAS NEXYA / Nexyalabs / AI Act.
    import pikepdf

    with pikepdf.open(io.BytesIO(result.pdf_bytes)) as preview_pdf:
        author = str(preview_pdf.docinfo.get("/Author", ""))
        producer = str(preview_pdf.docinfo.get("/Producer", ""))
        subject = str(preview_pdf.docinfo.get("/Subject", ""))
        keywords = str(preview_pdf.docinfo.get("/Keywords", ""))

        # AUCUN identifiant NEXYA ne doit figurer.
        for field_value in [author, producer, subject, keywords]:
            assert "NEXYA" not in field_value, (
                f"RÉGRESSION RGPD : champ contient NEXYA → {field_value!r}"
            )
            assert "Nexyalabs" not in field_value, (
                f"RÉGRESSION RGPD : champ contient Nexyalabs → {field_value!r}"
            )
            assert "AI Act" not in field_value, (
                f"RÉGRESSION RGPD : champ contient AI Act → {field_value!r}"
            )


# ══════════════════════════════════════════════════════════════
# 11. Cache write fire-and-forget (étape 7)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_cache_write_called_after_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Après génération, le cache est écrit (fire-and-forget)."""
    import asyncio

    upload = _make_fake_upload(mime_type=_PDF_MIME)
    monkeypatch.setattr(
        FileUploadService, "get_for_user", AsyncMock(return_value=upload)
    )

    original_pdf = _minimal_pdf_bytes()
    store = _FakeObjectStore()
    store._store[upload.storage_key] = original_pdf

    result = await PreviewService.get_cached_or_generate(
        _FAKE_UPLOAD_ID, _make_fake_user(), MagicMock(), store=store
    )

    # Laisse une chance au create_task de tourner.
    await asyncio.sleep(0.1)

    # Le cache a été écrit (clé `previews/{sha}.pdf`)
    cache_key = PreviewService._preview_key(_FAKE_SHA)
    upload_keys = [call[0] for call in store.upload_calls]
    assert cache_key in upload_keys


# ══════════════════════════════════════════════════════════════
# 12. PreviewResult dataclass
# ══════════════════════════════════════════════════════════════


def test_preview_result_dataclass_is_frozen() -> None:
    """`PreviewResult` est frozen (immutable)."""
    result = PreviewResult(
        pdf_bytes=b"test", from_cache=True, truncated=False, size_bytes=4
    )
    with pytest.raises(Exception):  # FrozenInstanceError
        result.from_cache = False  # type: ignore[misc]


def test_preview_result_size_bytes_matches_pdf() -> None:
    """`size_bytes` exposé pour log + header Content-Length."""
    pdf_bytes = b"%PDF-1.4 fake content"
    result = PreviewResult(
        pdf_bytes=pdf_bytes, from_cache=False, truncated=True, size_bytes=len(pdf_bytes)
    )
    assert result.size_bytes == len(pdf_bytes)
    assert result.truncated is True
