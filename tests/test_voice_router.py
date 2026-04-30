"""
Tests d'intégration — router `/voice/*` (E1 Pro-only).

Vérifications critiques :
- Un **Free** qui tape `/voice/transcribe` ou `/voice/speak` reçoit
  **403 `PLAN_REQUIRED`** avant tout coût (provider/budget non appelés).
- Un **Pro** passe le guard et le service est délégué correctement.
- Propagation des codes d'erreur service (402, 413, 415, 429, 503).
- Mode `save_to_library=False` → StreamingResponse audio direct.
"""

from __future__ import annotations

import uuid
from datetime import UTC
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.ai.voice.base import TTSResult
from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import (
    AudioTooLongException,
    FileTypeNotAllowedException,
    RateLimitAbuseException,
    TTSQuotaExceededException,
    VoiceQuotaExceededException,
    VoiceUnavailableException,
)
from app.features.auth.models import User
from app.features.voice.schemas import SpeakResponse
from app.features.voice.service import VoiceService
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_user(is_pro: bool) -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = is_pro
    return user


@pytest.fixture
def pro_client():
    """TestClient configuré avec un user Pro."""
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


@pytest.fixture
def free_client():
    """TestClient configuré avec un user Free — déclenche 403 sur /voice/*."""
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
# 1. Free → 403 PLAN_REQUIRED sur transcribe
# ══════════════════════════════════════════════════════════════


def test_free_user_gets_403_on_transcribe(free_client: TestClient) -> None:
    response = free_client.post(
        "/voice/transcribe",
        files={"audio": ("x.mp3", b"fake", "audio/mpeg")},
    )
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "PLAN_REQUIRED"


def test_free_user_gets_403_on_speak(free_client: TestClient) -> None:
    response = free_client.post("/voice/speak", json={"text": "hello"})
    assert response.status_code == 403
    body = response.json()
    assert body["code"] == "PLAN_REQUIRED"


# ══════════════════════════════════════════════════════════════
# 2. Pro → service appelé, 201 happy
# ══════════════════════════════════════════════════════════════


def test_pro_user_transcribe_201_with_envelope(
    pro_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from datetime import datetime
    from decimal import Decimal

    from app.features.voice.models import VoiceTranscription

    row = VoiceTranscription(
        user_id=_FAKE_USER_ID,
        source_file_id=None,
        content_sha256="a" * 64,
        transcribed_text="Bonjour le monde",
        language="fr",
        duration_seconds=Decimal("3.500"),
        model="whisper-1",
        provider="openai",
        cost_usd=Decimal("0.000350"),
    )
    row.id = uuid.uuid4()
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    row.deleted_at = None
    row.metadata_json = None

    monkeypatch.setattr(VoiceService, "transcribe", AsyncMock(return_value=row))

    response = pro_client.post(
        "/voice/transcribe",
        files={"audio": ("test.mp3", b"fake-audio", "audio/mpeg")},
    )
    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["data"]["transcribed_text"] == "Bonjour le monde"
    assert body["data"]["language"] == "fr"


# ══════════════════════════════════════════════════════════════
# 3. Pro → speak 200 save_to_library=True
# ══════════════════════════════════════════════════════════════


def test_pro_user_speak_200_with_library_id(
    pro_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from decimal import Decimal

    response_data = SpeakResponse(
        library_id=uuid.uuid4(),
        url="mock://bucket/key?expires=123",
        chars=11,
        voice="alloy",
        model="tts-1",
        provider="openai",
        cost_usd=Decimal("0.000165"),
        mime_type="audio/mpeg",
    )
    monkeypatch.setattr(VoiceService, "synthesize", AsyncMock(return_value=response_data))
    r = pro_client.post("/voice/speak", json={"text": "hello world"})
    assert r.status_code == 200
    body = r.json()
    assert body["success"] is True
    assert body["data"]["library_id"] is not None
    assert body["data"]["url"].startswith("mock://")


# ══════════════════════════════════════════════════════════════
# 4. Pro → speak streaming mode (save_to_library=False)
# ══════════════════════════════════════════════════════════════


def test_pro_user_speak_streaming_audio_response(
    pro_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Mode save_to_library=False → StreamingResponse audio direct."""
    tts = TTSResult(
        audio_bytes=b"\xff\xfb\x90\x00fake-mp3",
        mime_type="audio/mpeg",
        voice="alloy",
        model="tts-1",
        provider="openai",
        chars=5,
        cost_usd=0.000075,
    )
    monkeypatch.setattr(VoiceService, "synthesize", AsyncMock(return_value=tts))
    r = pro_client.post("/voice/speak", json={"text": "hello", "save_to_library": False})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert b"fake-mp3" in r.content
    assert r.headers["x-voice-model"] == "tts-1"


# ══════════════════════════════════════════════════════════════
# 5-8. Propagation codes d'erreur service
# ══════════════════════════════════════════════════════════════


def test_transcribe_415_on_mime_not_allowed(
    pro_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        VoiceService,
        "transcribe",
        AsyncMock(side_effect=FileTypeNotAllowedException(mime_type="image/png")),
    )
    r = pro_client.post(
        "/voice/transcribe",
        files={"audio": ("x.png", b"fake", "image/png")},
    )
    assert r.status_code == 415


def test_transcribe_413_on_audio_too_long(
    pro_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        VoiceService,
        "transcribe",
        AsyncMock(side_effect=AudioTooLongException(duration_seconds=1000, max_seconds=600)),
    )
    r = pro_client.post(
        "/voice/transcribe",
        files={"audio": ("x.mp3", b"big", "audio/mpeg")},
    )
    assert r.status_code == 413
    assert r.json()["code"] == "AUDIO_TOO_LONG"


def test_transcribe_402_on_voice_quota_exceeded(
    pro_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        VoiceService,
        "transcribe",
        AsyncMock(side_effect=VoiceQuotaExceededException(current=120, maximum=120, plan="pro")),
    )
    r = pro_client.post(
        "/voice/transcribe",
        files={"audio": ("x.mp3", b"audio", "audio/mpeg")},
    )
    assert r.status_code == 402
    assert r.json()["code"] == "VOICE_QUOTA_EXCEEDED"
    assert r.json()["data"]["plan"] == "pro"


def test_speak_402_on_tts_quota_exceeded(
    pro_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        VoiceService,
        "synthesize",
        AsyncMock(side_effect=TTSQuotaExceededException(current=50000, maximum=50000, plan="pro")),
    )
    r = pro_client.post("/voice/speak", json={"text": "hi"})
    assert r.status_code == 402
    assert r.json()["code"] == "TTS_QUOTA_EXCEEDED"


def test_transcribe_429_on_rate_limit(
    pro_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        VoiceService,
        "transcribe",
        AsyncMock(side_effect=RateLimitAbuseException(retry_after=3600)),
    )
    r = pro_client.post(
        "/voice/transcribe",
        files={"audio": ("x.mp3", b"audio", "audio/mpeg")},
    )
    assert r.status_code == 429


def test_speak_503_on_voice_unavailable(
    pro_client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        VoiceService,
        "synthesize",
        AsyncMock(side_effect=VoiceUnavailableException(provider="openai", reason="api down")),
    )
    r = pro_client.post("/voice/speak", json={"text": "hi"})
    assert r.status_code == 503
    assert r.json()["code"] == "VOICE_UNAVAILABLE"


# ══════════════════════════════════════════════════════════════
# 9. Validators Pydantic
# ══════════════════════════════════════════════════════════════


def test_speak_422_on_text_over_4096_chars(pro_client: TestClient) -> None:
    r = pro_client.post("/voice/speak", json={"text": "x" * 4097})
    assert r.status_code == 422


def test_speak_422_on_empty_text(pro_client: TestClient) -> None:
    r = pro_client.post("/voice/speak", json={"text": ""})
    assert r.status_code == 422
