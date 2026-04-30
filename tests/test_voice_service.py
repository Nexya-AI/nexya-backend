"""
Tests d'intégration — `VoiceService` (E1 Pro-only).

Monkey-patch `get_voice_provider` + `get_budget_tracker` +
`check_user_rate_limit` pour isoler le pipeline sans clé ni Redis.
Vérifie les 13 étapes du pipeline transcribe + 8 étapes synthesize.
"""

from __future__ import annotations

import io
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import UploadFile

from app.ai.voice.base import (
    TranscriptionResult,
    TTSResult,
    VoiceUnavailableError,
)
from app.core.errors.exceptions import (
    AudioTooLongException,
    FileTooLargeException,
    FileTypeNotAllowedException,
    TTSQuotaExceededException,
    VoiceQuotaExceededException,
    VoiceUnavailableException,
)
from app.features.voice import service as voice_service_module
from app.features.voice.schemas import SpeakRequest
from app.features.voice.service import VoiceService

# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════


_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_user(is_pro: bool = True) -> Any:
    user = MagicMock()
    user.id = _USER_ID
    user.is_pro = is_pro
    return user


def _make_upload(
    data: bytes,
    *,
    filename: str = "test.mp3",
    content_type: str = "audio/mpeg",
) -> UploadFile:
    # FastAPI UploadFile wraps a SpooledTemporaryFile-like with read/close.
    return UploadFile(
        file=io.BytesIO(data),
        filename=filename,
        headers={"content-type": content_type},
    )


class _FakeProvider:
    name = "mock"

    def __init__(self, *, transcribe_result=None, raise_error=False) -> None:
        self._tr = transcribe_result
        self._raise = raise_error
        self.tr_calls: list[tuple] = []

    async def transcribe(self, audio, *, filename, mime_type, language=None):
        self.tr_calls.append((len(audio), filename, language))
        if self._raise:
            raise VoiceUnavailableError("down", provider="mock")
        return self._tr or TranscriptionResult(
            text="transcription fake",
            language="fr",
            duration_seconds=5.0,
            model="mock-whisper",
            provider="mock",
            cost_usd=0.0,
        )

    async def synthesize(self, text, *, voice="alloy", speed=1.0, model="tts-1", fmt="mp3"):
        if self._raise:
            raise VoiceUnavailableError("down", provider="mock")
        return TTSResult(
            audio_bytes=b"\xff\xfb\x90\x00" * 10,
            mime_type="audio/mpeg",
            voice=voice,
            model=model,
            provider="mock",
            chars=len(text),
            cost_usd=0.0,
        )


class _NoBudget:
    user_voice_minutes_per_day = 120
    user_tts_chars_per_day = 50_000

    def __init__(self) -> None:
        self.consume_calls: list[tuple[str, int]] = []
        self.refund_calls: list[int] = []

    async def check_and_consume_voice_minutes(self, uid, *, minutes):
        self.consume_calls.append(("voice", minutes))
        return minutes

    async def check_and_consume_tts_chars(self, uid, *, chars):
        self.consume_calls.append(("tts", chars))
        return chars

    async def refund_voice_minutes(self, uid, *, minutes):
        self.refund_calls.append(minutes)


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

    def add(self, obj) -> None:
        if not getattr(obj, "id", None):
            obj.id = uuid.uuid4()
        obj.created_at = datetime.now(UTC)
        obj.updated_at = datetime.now(UTC)
        self.added.append(obj)


@pytest.fixture(autouse=True)
def _patch_common(monkeypatch: pytest.MonkeyPatch):
    """Bypass rate-limit + install fake budget+provider par défaut."""
    monkeypatch.setattr(
        voice_service_module,
        "check_user_rate_limit",
        AsyncMock(return_value=None),
    )


# ══════════════════════════════════════════════════════════════
# 1. transcribe — rejet MIME
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_transcribe_rejects_non_audio_mime(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_service_module, "get_voice_provider", lambda: _FakeProvider())
    monkeypatch.setattr(voice_service_module, "get_budget_tracker", lambda: _NoBudget())
    user = _make_user()
    db = _FakeDB()
    upload = _make_upload(b"fake", filename="x.png", content_type="image/png")
    with pytest.raises(FileTypeNotAllowedException):
        await VoiceService.transcribe(user, db, upload_file=upload)


# ══════════════════════════════════════════════════════════════
# 2. transcribe — rejet taille
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_transcribe_rejects_file_too_large(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "voice_max_upload_bytes", 1024, raising=False)
    monkeypatch.setattr(voice_service_module, "get_voice_provider", lambda: _FakeProvider())
    monkeypatch.setattr(voice_service_module, "get_budget_tracker", lambda: _NoBudget())
    user = _make_user()
    db = _FakeDB()
    upload = _make_upload(b"A" * 5000, content_type="audio/mpeg")
    with pytest.raises(FileTooLargeException):
        await VoiceService.transcribe(user, db, upload_file=upload)


# ══════════════════════════════════════════════════════════════
# 3. transcribe — rejet durée > max
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_transcribe_rejects_audio_too_long(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "voice_max_duration_seconds", 60, raising=False)
    monkeypatch.setattr(voice_service_module, "get_voice_provider", lambda: _FakeProvider())
    monkeypatch.setattr(voice_service_module, "get_budget_tracker", lambda: _NoBudget())
    user = _make_user()
    db = _FakeDB()
    # ~16k bytes/s × 100 s = 1.6 MB estimé > 60 s max.
    upload = _make_upload(b"A" * 1_600_000, content_type="audio/mpeg")
    with pytest.raises(AudioTooLongException):
        await VoiceService.transcribe(user, db, upload_file=upload)


# ══════════════════════════════════════════════════════════════
# 4. transcribe — dédup SHA retourne existing sans API call
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_transcribe_dedup_returns_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = MagicMock()
    existing.id = uuid.uuid4()
    provider = _FakeProvider()
    monkeypatch.setattr(voice_service_module, "get_voice_provider", lambda: provider)
    monkeypatch.setattr(voice_service_module, "get_budget_tracker", lambda: _NoBudget())
    user = _make_user()
    db = _FakeDB(existing=existing)
    upload = _make_upload(b"audio-content", content_type="audio/mpeg")
    result = await VoiceService.transcribe(user, db, upload_file=upload)
    assert result is existing
    # Provider non-appelé sur dédup.
    assert provider.tr_calls == []


# ══════════════════════════════════════════════════════════════
# 5. transcribe — happy path
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_transcribe_happy_path_inserts_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_service_module, "get_voice_provider", lambda: _FakeProvider())
    monkeypatch.setattr(voice_service_module, "get_budget_tracker", lambda: _NoBudget())
    user = _make_user()
    db = _FakeDB()
    upload = _make_upload(b"hello audio", content_type="audio/mpeg")
    row = await VoiceService.transcribe(user, db, upload_file=upload, language="fr")
    assert row.transcribed_text == "transcription fake"
    assert row.language == "fr"
    assert row.model == "mock-whisper"
    assert row.provider == "mock"
    assert len(db.added) == 1


# ══════════════════════════════════════════════════════════════
# 6. transcribe — remboursement si estimation > durée réelle
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_transcribe_refunds_excess_estimate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Provider retourne 5s (1 min après ceil) pour 32k bytes → estimation 2 min.
    provider = _FakeProvider(
        transcribe_result=TranscriptionResult(
            text="t",
            language="fr",
            duration_seconds=5.0,
            model="mock-whisper",
            provider="mock",
            cost_usd=0.0,
        )
    )
    budget = _NoBudget()
    monkeypatch.setattr(voice_service_module, "get_voice_provider", lambda: provider)
    monkeypatch.setattr(voice_service_module, "get_budget_tracker", lambda: budget)
    user = _make_user()
    db = _FakeDB()
    # 32k bytes / 16k/s = 2s estimé → 1 min. Durée réelle 5s → 1 min.
    # Estimation == réelle, donc refund = 0.
    # Test avec 64k bytes = 4s estimé → 1 min. Réel 5s = 1 min. No refund.
    # Test vrai refund : 320k bytes = 20s estimé = 1 min. Réel 5s = 1 min. No refund (ceil = 1 dans les deux cas).
    # Faisons 2000k bytes = 125s estimé = 3 min. Réel 5s = 1 min. Refund = 2.
    upload = _make_upload(b"A" * 2_000_000, content_type="audio/mpeg")
    await VoiceService.transcribe(user, db, upload_file=upload)
    # refund_voice_minutes appelé avec 2 minutes.
    assert budget.refund_calls == [2]


# ══════════════════════════════════════════════════════════════
# 7. transcribe — quota dépassé → 402
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_transcribe_raises_voice_quota_exceeded_on_budget_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.errors.exceptions import RateLimitExceededException

    class _CappedBudget:
        user_voice_minutes_per_day = 1
        user_tts_chars_per_day = 1000

        async def check_and_consume_voice_minutes(self, uid, *, minutes):
            raise RateLimitExceededException(reset_at=None)

        async def refund_voice_minutes(self, uid, *, minutes):
            pass

        async def check_and_consume_tts_chars(self, uid, *, chars):
            return chars

    monkeypatch.setattr(voice_service_module, "get_voice_provider", lambda: _FakeProvider())
    monkeypatch.setattr(voice_service_module, "get_budget_tracker", lambda: _CappedBudget())
    user = _make_user()
    db = _FakeDB()
    upload = _make_upload(b"A" * 100_000, content_type="audio/mpeg")
    with pytest.raises(VoiceQuotaExceededException):
        await VoiceService.transcribe(user, db, upload_file=upload)


# ══════════════════════════════════════════════════════════════
# 8. transcribe — provider down → 503 + refund
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_transcribe_maps_provider_error_to_503_and_refunds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider = _FakeProvider(raise_error=True)
    budget = _NoBudget()
    monkeypatch.setattr(voice_service_module, "get_voice_provider", lambda: provider)
    monkeypatch.setattr(voice_service_module, "get_budget_tracker", lambda: budget)
    user = _make_user()
    db = _FakeDB()
    upload = _make_upload(b"A" * 50_000, content_type="audio/mpeg")
    with pytest.raises(VoiceUnavailableException):
        await VoiceService.transcribe(user, db, upload_file=upload)
    # Rembourse l'estimation entière.
    assert len(budget.refund_calls) == 1


# ══════════════════════════════════════════════════════════════
# 9. synthesize — happy path avec save_to_library=False
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_synthesize_no_library_returns_tts_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(voice_service_module, "get_voice_provider", lambda: _FakeProvider())
    monkeypatch.setattr(voice_service_module, "get_budget_tracker", lambda: _NoBudget())
    user = _make_user()
    db = _FakeDB()
    body = SpeakRequest(text="Hello world", save_to_library=False)
    result = await VoiceService.synthesize(user, db, body=body)
    # Retour TTSResult direct.
    assert hasattr(result, "audio_bytes")
    assert result.mime_type == "audio/mpeg"


# ══════════════════════════════════════════════════════════════
# 10. synthesize — quota TTS chars → 402
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_synthesize_raises_tts_quota_exceeded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.errors.exceptions import RateLimitExceededException

    class _CappedBudget:
        user_voice_minutes_per_day = 120
        user_tts_chars_per_day = 10

        async def check_and_consume_voice_minutes(self, uid, *, minutes):
            return minutes

        async def check_and_consume_tts_chars(self, uid, *, chars):
            raise RateLimitExceededException(reset_at=None)

        async def refund_voice_minutes(self, uid, *, minutes):
            pass

    monkeypatch.setattr(voice_service_module, "get_voice_provider", lambda: _FakeProvider())
    monkeypatch.setattr(voice_service_module, "get_budget_tracker", lambda: _CappedBudget())
    user = _make_user()
    db = _FakeDB()
    body = SpeakRequest(text="Hello world", save_to_library=False)
    with pytest.raises(TTSQuotaExceededException):
        await VoiceService.synthesize(user, db, body=body)


# ══════════════════════════════════════════════════════════════
# 11. synthesize — fail-safe Library
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_synthesize_library_failsafe_returns_response_without_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si LibraryService.create_from_bytes raise, on retourne quand même
    un `SpeakResponse` avec `library_id=None`."""
    from app.features.library.service import LibraryService

    monkeypatch.setattr(voice_service_module, "get_voice_provider", lambda: _FakeProvider())
    monkeypatch.setattr(voice_service_module, "get_budget_tracker", lambda: _NoBudget())
    monkeypatch.setattr(
        LibraryService,
        "create_from_bytes",
        AsyncMock(side_effect=RuntimeError("storage down")),
    )
    user = _make_user()
    db = _FakeDB()
    body = SpeakRequest(text="Hello world", save_to_library=True)
    result = await VoiceService.synthesize(user, db, body=body)
    assert result.library_id is None
    assert result.url is None
    # Mais on a bien les autres champs.
    assert result.chars == len("Hello world")
    assert result.mime_type == "audio/mpeg"


# ══════════════════════════════════════════════════════════════
# 12. synthesize — provider down → 503
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_synthesize_maps_provider_error_to_503(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        voice_service_module,
        "get_voice_provider",
        lambda: _FakeProvider(raise_error=True),
    )
    monkeypatch.setattr(voice_service_module, "get_budget_tracker", lambda: _NoBudget())
    user = _make_user()
    db = _FakeDB()
    body = SpeakRequest(text="Hello", save_to_library=False)
    with pytest.raises(VoiceUnavailableException):
        await VoiceService.synthesize(user, db, body=body)
