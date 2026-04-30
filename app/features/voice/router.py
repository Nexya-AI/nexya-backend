"""
Router Voice — endpoints `/voice/*` Pro only (E1).

Stratégie asymétrique :
- **Free plan** : STT/TTS natif Flutter (`speech_to_text` + `flutter_tts`).
  Ne passe JAMAIS par ces endpoints → $0 de coût backend.
- **Pro plan** : ces endpoints gated `Depends(require_pro)` appellent
  Whisper / OpenAI TTS pour qualité premium.

Un Free qui tape `/voice/transcribe` ou `/voice/speak` reçoit
**403 `PLAN_REQUIRED`** immédiat, avant toute lecture de bytes ou tout
appel API. Le Flutter interprète ce code comme signal paywall → affiche
la modal upgrade Pro.

Discipline :
- `Depends(require_pro)` remplace `Depends(get_current_user)` sur les 2
  endpoints. C'est la seule différence de câblage — le reste du pipeline
  est identique à Files E3 / Memory D5 / RAG D5.
- Aucune logique métier — délégation stricte à `VoiceService`.
- `/voice/speak` avec `save_to_library=False` renvoie un
  `StreamingResponse(audio/mpeg)` direct au lieu du JSON enveloppe.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, Response, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.voice import TTSResult
from app.core.auth.guards import get_current_user, require_pro
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.voice.schemas import (
    SpeakRequest,
    SpeakResponse,
    TranscribeResponse,
    VoiceCatalogueResponse,
)
from app.features.voice.service import VoiceService
from app.features.voice.voices_catalogue import get_voice_catalogue
from app.shared.schemas import NexyaResponse

router = APIRouter(prefix="/voice", tags=["voice"])


# ══════════════════════════════════════════════════════════════
# POST /voice/transcribe — Whisper STT (Pro only)
# ══════════════════════════════════════════════════════════════


@router.post(
    "/transcribe",
    status_code=status.HTTP_201_CREATED,
    response_model=NexyaResponse[TranscribeResponse],
)
async def transcribe(
    audio: UploadFile = File(..., description="Fichier audio (≤ 20 MB, ≤ 10 min)"),
    language: str | None = Form(
        default=None,
        max_length=8,
        description="Code langue ISO-639-1 (ex: 'fr'). None = auto-détection.",
    ),
    current_user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[TranscribeResponse]:
    """Transcrit un audio via Whisper. **Pro only** (403 sinon).

    Pipeline :
    - MIME whitelist audio (7 formats).
    - Cap 20 MB + estimation durée ≤ 10 min.
    - Dédup SHA-256 cross-upload.
    - Quota `voice_minutes_pro_per_day` → 402 `VOICE_QUOTA_EXCEEDED`.
    - Rate limit 30 req/h/user → 429 `RATE_LIMIT_ABUSE`.
    - INSERT `voice_transcriptions` avec `model` + `cost_usd` tracés
      (benchmark portabilité providers).
    """
    row = await VoiceService.transcribe(current_user, db, upload_file=audio, language=language)
    return NexyaResponse(success=True, data=TranscribeResponse.model_validate(row))


# ══════════════════════════════════════════════════════════════
# POST /voice/speak — OpenAI TTS (Pro only)
# ══════════════════════════════════════════════════════════════


@router.post("/speak", response_model=NexyaResponse[SpeakResponse])
async def speak(
    body: SpeakRequest,
    current_user: User = Depends(require_pro),
    db: AsyncSession = Depends(get_db),
):
    """Génère un audio TTS. **Pro only** (403 sinon).

    - `save_to_library=True` (défaut) : MP3 sauvé dans Library C3 avec
      `source='generated'`, retourne `library_id` + presigned URL.
    - `save_to_library=False` : retourne directement un StreamingResponse
      audio (pour les cas où le Flutter veut lire l'audio sans passer
      par MinIO, ex: lecture à voix haute d'une réponse courte).
    """
    result = await VoiceService.synthesize(current_user, db, body=body)

    if isinstance(result, TTSResult):
        # Mode save_to_library=False → retourne le MP3 direct.
        return StreamingResponse(
            content=_bytes_iter(result.audio_bytes),
            media_type=result.mime_type,
            headers={
                "X-Voice-Model": result.model,
                "X-Voice-Provider": result.provider,
                "X-Voice-Chars": str(result.chars),
                "Cache-Control": "no-cache",
            },
        )

    return NexyaResponse(success=True, data=result)


async def _bytes_iter(audio: bytes, chunk_size: int = 8192):
    """Itère les bytes en chunks pour StreamingResponse."""
    for i in range(0, len(audio), chunk_size):
        yield audio[i : i + chunk_size]


# ══════════════════════════════════════════════════════════════════
# Session N1 — GET /voice/list (catalogue NEXYA branded, NOT Pro-only)
# ══════════════════════════════════════════════════════════════════


@router.get(
    "/list",
    response_model=NexyaResponse[VoiceCatalogueResponse],
)
async def list_voices(
    response: Response,
    current_user: User = Depends(get_current_user),
) -> NexyaResponse[VoiceCatalogueResponse]:
    """Catalogue 6 voix NEXYA branded — auth requise mais PAS Pro-only.

    Le picker s'affiche AVANT l'upgrade Pro pour que l'user voie ce
    qu'il débloquera (transparence catalogue). Les voix Pro-only V2
    seront filtrées via flag `is_pro_only` à ce moment-là.

    `Cache-Control: public, max-age=3600` — catalogue identique pour
    tous les users, CDN-cacheable pour les futures requêtes Africa
    2G/3G (gain latence).
    """
    response.headers["Cache-Control"] = "public, max-age=3600"
    voices = get_voice_catalogue()
    return NexyaResponse(
        success=True,
        data=VoiceCatalogueResponse(voices=voices),
    )
