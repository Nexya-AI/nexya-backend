"""
Schémas Pydantic — endpoints publics Voice Pro-only (E1).

Contrat API :
- `POST /voice/transcribe` : multipart form → `TranscribeResponse` (201).
  Pas de body Pydantic ici — multipart géré par FastAPI `UploadFile`.
- `POST /voice/speak` : `SpeakRequest` → `SpeakResponse`.

Discipline :
- Pas d'exposition du vecteur embedding, du content_sha256 brut ou du
  storage_key côté client — même politique E3/D5.
- `TTSVoice` et `TTSFormat` Literal 1:1 alignés sur l'ABC Voice.
- Validator strip sur les textes libres.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# ══════════════════════════════════════════════════════════════
# Types partagés
# ══════════════════════════════════════════════════════════════

TTSVoice = Literal["alloy", "echo", "fable", "onyx", "nova", "shimmer"]
TTSFormat = Literal["mp3", "opus", "aac", "flac"]
TTSModel = Literal["tts-1", "tts-1-hd"]

# Langues STT acceptées côté API. Whisper supporte 99 langues ; on
# whiteliste les 8 les plus utiles pour NEXYA + `None` pour l'auto-détection.
STTLanguage = Literal["fr", "en", "ar", "es", "pt", "de", "it", "sw"]


# ══════════════════════════════════════════════════════════════
# Réponses
# ══════════════════════════════════════════════════════════════


class TranscribeResponse(BaseModel):
    """Résultat d'une transcription Whisper.

    Mappé depuis `VoiceTranscription` ORM via `from_attributes=True`.
    N'expose PAS `content_sha256` (interne à la dédup) ni `metadata_json`
    (réservé admin).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    transcribed_text: str
    language: str | None = None
    duration_seconds: Decimal
    model: str
    provider: str
    source_file_id: uuid.UUID | None = None
    created_at: datetime


class SpeakRequest(BaseModel):
    """Body de `POST /voice/speak` — TTS.

    - `text` 1-4096 chars (cap OpenAI TTS API).
    - `voice` par défaut `alloy` (voix neutre, bonne qualité FR).
    - `speed` 0.25-4.0 (cap OpenAI).
    - `model` par défaut `tts-1` ($15/1M chars). `tts-1-hd` ($30/1M)
      pour qualité supérieure.
    - `fmt` par défaut `mp3`.
    - `save_to_library` : si True (défaut), sauve le MP3 dans la Library
      C3 (source='generated') et retourne `library_id` + URL signée.
      Si False, le backend retourne un StreamingResponse audio direct.
    """

    text: str = Field(min_length=1, max_length=4096)
    voice: TTSVoice = "alloy"
    speed: float = Field(default=1.0, ge=0.25, le=4.0)
    model: TTSModel = "tts-1"
    fmt: TTSFormat = "mp3"
    save_to_library: bool = True

    @field_validator("text", mode="before")
    @classmethod
    def _strip_text(cls, v):
        return v.strip() if isinstance(v, str) else v


class SpeakResponse(BaseModel):
    """Réponse TTS en mode `save_to_library=True` (défaut).

    Le client récupère la presigned URL MinIO pour streamer l'audio
    sans passer par le backend. En mode `save_to_library=False`, le
    router retourne directement un `StreamingResponse(audio/mpeg)`.
    """

    library_id: uuid.UUID | None = None
    url: str | None = None
    chars: int
    voice: str
    model: str
    provider: str
    cost_usd: Decimal = Decimal("0")
    mime_type: str = "audio/mpeg"


# ══════════════════════════════════════════════════════════════════
# Session N1 — GET /voice/list (catalogue NEXYA branded)
# ══════════════════════════════════════════════════════════════════

VoiceTone = Literal["deep", "medium", "high"]
VoiceLanguage = Literal["fr", "en", "both"]


class VoiceCatalogueItem(BaseModel):
    """Une voix NEXYA branded — métadonnées textuelles uniquement.

    Les couleurs UI (orb, background dark/light) restent côté Flutter
    en V1 — le backend ne gère que les métadonnées textuelles. V2 si
    Ivan veut centraliser les couleurs côté backend pour modifier le
    branding sans déploiement Flutter.
    """

    id: str
    name: str
    personality: str
    tone: VoiceTone
    language: VoiceLanguage = "fr"


class VoiceCatalogueResponse(BaseModel):
    """Réponse GET /voice/list — catalogue complet."""

    voices: list[VoiceCatalogueItem]
