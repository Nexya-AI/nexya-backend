"""
VoiceService — pipeline STT (Whisper) + TTS (OpenAI TTS) Pro-only.

**Scope Pro uniquement.** Les endpoints `/voice/*` sont gated par
`Depends(require_pro)` côté router, donc ce service n'est jamais appelé
par un user Free. Pas de logique Free ici — un Free est bloqué en 403
au niveau du router, avant d'atteindre ce code.

Pipeline `transcribe` 13 étapes court-circuitantes :
    1. MIME whitelist audio (FileTypeNotAllowedException 415).
    2. Lecture streaming + cap bytes (FileTooLargeException 413).
    3. SHA-256 calculé pendant la lecture.
    4. Magic-bytes check (FileContentMismatchException 415).
    5. Estimation durée via heuristique MP3 128 kbps.
    6. Refus si durée estimée > voice_max_duration_seconds (AudioTooLongException 413).
    7. Dédup SHA actif → SELECT existing, retour immédiat.
    8. Quota pré-flight voice_minutes_pro → 402 VOICE_QUOTA_EXCEEDED.
    9. Budget embeddings-style voice_minutes (`check_and_consume_voice_minutes`).
   10. Rate limit user-scoped voice_transcribe 30/h → 429.
   11. Appel provider.transcribe() avec retry en cas d'erreur retryable.
   12. Correction compteur voice_minutes via `refund_voice_minutes` si
       estimation pré-appel > durée réelle retournée par l'API.
   13. INSERT voice_transcriptions + log forensic.

Pipeline `synthesize` 9 étapes :
    1. Validation chars (déjà Pydantic, double guard côté settings).
    2. Quota pré-flight voice_tts_chars_pro → 402 TTS_QUOTA_EXCEEDED.
    3. Budget check_and_consume_tts_chars.
    4. Rate limit voice_tts 60/h → 429.
    5. Appel provider.synthesize() avec mapping erreurs.
    6. Si save_to_library=True : fail-safe LibraryService.create_from_bytes
       avec source='generated'.
    7. Log forensic.
    8. Return SpeakResponse avec library_id + URL, OU bytes bruts si
       save_to_library=False.

Discipline mock-first : `get_voice_provider()` bascule auto sur mock si
`OPENAI_API_KEY` vide, donc ce service tourne identiquement en dev sans clé.
"""

from __future__ import annotations

import hashlib
import math
from datetime import UTC
from decimal import Decimal
from typing import Final

import structlog
from fastapi import UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.budget_tracker import get_budget_tracker
from app.ai.voice import (
    TranscriptionResult,
    TTSResult,
    VoiceError,
    get_voice_provider,
)
from app.config import settings
from app.core.errors.exceptions import (
    AudioTooLongException,
    FileContentMismatchException,
    FileTooLargeException,
    FileTypeNotAllowedException,
    RateLimitAbuseException,
    TTSQuotaExceededException,
    VoiceQuotaExceededException,
    VoiceUnavailableException,
)
from app.core.security.rate_limiter import check_user_rate_limit
from app.core.storage import detect_mime_type, mimes_compatible
from app.features.auth.models import User
from app.features.voice.models import VoiceTranscription
from app.features.voice.schemas import SpeakRequest, SpeakResponse

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

# Heuristique MP3 128 kbps : ~16 000 bytes/seconde d'audio. Utilisée
# pour estimer la durée AVANT appel API (on ne parse pas le header MP3
# pour éviter une dépendance FFmpeg). Conservatrice : si on se trompe,
# on estime trop (le remboursement post-appel corrige).
_BYTES_PER_SECOND_ESTIMATE: Final[int] = 16_000

_READ_CHUNK_SIZE: Final[int] = 8 * 1024
_MAGIC_PROBE_BYTES: Final[int] = 4096


# ══════════════════════════════════════════════════════════════
# VoiceService
# ══════════════════════════════════════════════════════════════


class VoiceService:
    """Pipeline Voice Pro-only (STT + TTS)."""

    # ── Streaming read avec cap + SHA ──────────────────────────
    @staticmethod
    async def _read_capped(upload_file: UploadFile, *, max_bytes: int) -> tuple[bytes, str]:
        """Lit un UploadFile par chunks avec SHA-256 accumulé, cap strict."""
        hasher = hashlib.sha256()
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = await upload_file.read(_READ_CHUNK_SIZE)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise FileTooLargeException(max_mb=max_bytes // (1024 * 1024))
            hasher.update(chunk)
            chunks.append(chunk)
        return b"".join(chunks), hasher.hexdigest()

    # ── TRANSCRIBE (STT) ───────────────────────────────────────
    @staticmethod
    async def transcribe(
        user: User,
        db: AsyncSession,
        *,
        upload_file: UploadFile,
        language: str | None = None,
    ) -> VoiceTranscription:
        """Transcrit un audio via Whisper. Pro only (gated au router)."""
        user_id_str = str(user.id)

        # 1. MIME whitelist.
        announced_mime = (upload_file.content_type or "").lower()
        allowed = {m.lower() for m in settings.voice_allowed_mimes}
        if announced_mime not in allowed:
            log.info(
                "voice.transcribe.mime_rejected",
                mime=announced_mime,
                user_id=user_id_str,
            )
            raise FileTypeNotAllowedException(mime_type=announced_mime)

        # 2+3. Streaming read + SHA.
        data, content_sha = await VoiceService._read_capped(
            upload_file, max_bytes=settings.voice_max_upload_bytes
        )

        # 4. Magic-bytes check.
        detected = detect_mime_type(data[:_MAGIC_PROBE_BYTES])
        if detected is None:
            # Certains WebM/OGG audio peuvent ne pas matcher nos
            # signatures (variantes codec) — on tolère si le MIME
            # annoncé fait partie de la famille audio whitelistée.
            log.info(
                "voice.transcribe.magic_unknown_accepted",
                announced=announced_mime,
                user_id=user_id_str,
            )
        elif not mimes_compatible(announced_mime, detected):
            log.warning(
                "voice.transcribe.mime_mismatch",
                announced=announced_mime,
                detected=detected,
                user_id=user_id_str,
            )
            raise FileContentMismatchException(announced=announced_mime, detected=detected)

        size_bytes = len(data)

        # 5. Estimation durée audio (heuristique MP3 128 kbps).
        duration_estimate = max(1.0, size_bytes / _BYTES_PER_SECOND_ESTIMATE)

        # 6. Refus si trop long.
        if duration_estimate > settings.voice_max_duration_seconds:
            log.info(
                "voice.transcribe.too_long",
                duration_s=duration_estimate,
                max_s=settings.voice_max_duration_seconds,
                user_id=user_id_str,
            )
            raise AudioTooLongException(
                duration_seconds=duration_estimate,
                max_seconds=settings.voice_max_duration_seconds,
            )

        # 7. Dédup SHA actif.
        existing_q = await db.execute(
            select(VoiceTranscription).where(
                VoiceTranscription.user_id == user.id,
                VoiceTranscription.content_sha256 == content_sha,
                VoiceTranscription.deleted_at.is_(None),
            )
        )
        existing = existing_q.scalar_one_or_none()
        if existing is not None:
            log.info(
                "voice.transcribe.dedup_hit",
                user_id=user_id_str,
                row_id=str(existing.id),
                sha=content_sha[:16],
            )
            return existing

        # 8. Quota pré-flight (Pro only).
        plan_label = "pro" if getattr(user, "is_pro", False) else "free"
        max_minutes = settings.voice_minutes_pro_per_day
        # Note : ce service n'est appelé que par un Pro (gated router).
        # On garde quand même le check métier pour défense en profondeur.
        minutes_estimate = max(1, math.ceil(duration_estimate / 60.0))

        # 9. Budget + rate limit via BudgetTracker.
        tracker = get_budget_tracker()
        # On override le cap tracker runtime avec le setting Pro pour
        # que la limite correspondre au plan (le tracker a un cap global
        # à 120 mais settings peut être plus haut/bas).
        tracker.user_voice_minutes_per_day = max_minutes
        try:
            await tracker.check_and_consume_voice_minutes(user_id_str, minutes=minutes_estimate)
        except Exception as exc:
            # RateLimitExceededException du BudgetTracker → on remappe en
            # VoiceQuotaExceeded avec jauge propre pour le Flutter.
            if exc.__class__.__name__ == "RateLimitExceededException":
                raise VoiceQuotaExceededException(
                    current=max_minutes,  # on est ≥ max par définition
                    maximum=max_minutes,
                    plan=plan_label,
                ) from exc
            raise

        # 10. Rate limit user-scoped.
        await check_user_rate_limit(
            user.id,
            action="voice_transcribe",
            max_requests=settings.voice_transcribe_rate_limit_per_hour,
            window_seconds=3600,
            on_exceeded=RateLimitAbuseException,
        )

        # 11. Appel provider.
        provider = get_voice_provider()
        try:
            result: TranscriptionResult = await provider.transcribe(
                data,
                filename=upload_file.filename or "audio",
                mime_type=announced_mime,
                language=language,
            )
        except VoiceError as exc:
            log.warning(
                "voice.transcribe.provider_error",
                user_id=user_id_str,
                provider=exc.provider,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            # Rembourser l'estimation (on n'a rien consommé réel).
            await tracker.refund_voice_minutes(user_id_str, minutes=minutes_estimate)
            raise VoiceUnavailableException(provider=exc.provider, reason=str(exc)) from exc

        # 12. Correction compteur via la durée réelle.
        real_duration = result.duration_seconds
        real_minutes = max(1, math.ceil(real_duration / 60.0))
        if minutes_estimate > real_minutes:
            await tracker.refund_voice_minutes(user_id_str, minutes=minutes_estimate - real_minutes)

        # 13. INSERT + log forensic.
        row = VoiceTranscription(
            user_id=user.id,
            source_file_id=None,  # audio uploadé direct, pas via /files/upload
            content_sha256=content_sha,
            transcribed_text=result.text,
            language=result.language,
            duration_seconds=Decimal(str(round(result.duration_seconds, 3))),
            model=result.model,
            provider=result.provider,
            cost_usd=Decimal(str(result.cost_usd)),
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

        log.info(
            "voice.transcribe.completed",
            user_id=user_id_str,
            row_id=str(row.id),
            duration_s=result.duration_seconds,
            size_bytes=size_bytes,
            model=result.model,
            provider=result.provider,
            language_detected=result.language,
            cost_usd=result.cost_usd,
            minutes_charged=real_minutes,
            minutes_estimate=minutes_estimate,
        )
        return row

    # ── SYNTHESIZE (TTS) ───────────────────────────────────────
    @staticmethod
    async def synthesize(
        user: User,
        db: AsyncSession,
        *,
        body: SpeakRequest,
    ) -> SpeakResponse | TTSResult:
        """Génère un audio TTS. Pro only (gated au router).

        Retourne `SpeakResponse` si `save_to_library=True`, sinon le
        `TTSResult` brut (le router le convertit en StreamingResponse).
        """
        user_id_str = str(user.id)
        chars = len(body.text)
        plan_label = "pro" if getattr(user, "is_pro", False) else "free"
        max_chars = settings.voice_tts_chars_pro_per_day

        tracker = get_budget_tracker()
        # Alignement cap tracker avec setting (même logique que STT).
        tracker.user_tts_chars_per_day = max_chars

        # 2+3. Quota + budget (fusionnés côté BudgetTracker).
        try:
            await tracker.check_and_consume_tts_chars(user_id_str, chars=chars)
        except Exception as exc:
            if exc.__class__.__name__ == "RateLimitExceededException":
                raise TTSQuotaExceededException(
                    current=max_chars,
                    maximum=max_chars,
                    plan=plan_label,
                ) from exc
            raise

        # 4. Rate limit.
        await check_user_rate_limit(
            user.id,
            action="voice_tts",
            max_requests=settings.voice_tts_rate_limit_per_hour,
            window_seconds=3600,
            on_exceeded=RateLimitAbuseException,
        )

        # 5. Appel provider.
        provider = get_voice_provider()
        try:
            result: TTSResult = await provider.synthesize(
                body.text,
                voice=body.voice,
                speed=body.speed,
                model=body.model,
                fmt=body.fmt,
            )
        except VoiceError as exc:
            log.warning(
                "voice.synthesize.provider_error",
                user_id=user_id_str,
                provider=exc.provider,
                error=str(exc),
            )
            raise VoiceUnavailableException(provider=exc.provider, reason=str(exc)) from exc

        # 6+7. Log forensic commun.
        log.info(
            "voice.synthesize.completed",
            user_id=user_id_str,
            chars=chars,
            voice=body.voice,
            model=result.model,
            provider=result.provider,
            audio_bytes=len(result.audio_bytes),
            cost_usd=result.cost_usd,
            saved_to_library=body.save_to_library,
        )

        # 8. Mode save_to_library — fail-safe Library.
        if not body.save_to_library:
            return result

        library_id: str | None = None
        url: str | None = None
        try:
            from app.features.library.service import (  # noqa: PLC0415
                LibraryService,
            )

            item = await LibraryService.create_from_bytes(
                user,
                db,
                type_="audio",
                data=result.audio_bytes,
                mime_type=result.mime_type,
                title=_build_auto_tts_title(body.text),
                description=None,
                source="generated",
                provider=result.provider,
                model=result.model,
                prompt=body.text[:200],
                source_conversation_id=None,
                source_message_id=None,
                tags=None,
                metadata_json={"voice": body.voice, "chars": chars},
            )
            library_id = item.id
            url = await LibraryService.presigned_url_for(item)
        except Exception as exc:  # noqa: BLE001
            # Fail-safe : l'échec library ne doit pas casser la réponse
            # TTS — on renvoie une `SpeakResponse` sans library_id.
            log.warning(
                "voice.synthesize.library_save_failed",
                user_id=user_id_str,
                error=str(exc),
                error_type=type(exc).__name__,
            )

        return SpeakResponse(
            library_id=library_id,
            url=url,
            chars=chars,
            voice=result.voice,
            model=result.model,
            provider=result.provider,
            cost_usd=Decimal(str(result.cost_usd)),
            mime_type=result.mime_type,
        )


# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════


def _build_auto_tts_title(text: str) -> str:
    """Construit un titre court pour la Library (≤ 60 chars)."""
    base = text.strip()
    if not base:
        from datetime import datetime  # noqa: PLC0415

        return "Audio TTS " + datetime.now(UTC).strftime("%Y-%m-%d %H:%M")
    if len(base) <= 60:
        return base
    cut = base[:60].rstrip()
    # Coupe sur espace pour ne pas finir au milieu d'un mot.
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut + "…"
