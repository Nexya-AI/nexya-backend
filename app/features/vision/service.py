"""
VisionService — pipeline analyse image Free + Pro (E2).

Endpoint accepté pour Free (tier flash imposé) et Pro (tier au choix).
Différenciation par **qualité/tier**, pas par `require_pro` gate.

Pipeline `analyze` 14 étapes strictement court-circuitantes :

    1. Validation tier Pro si `model_tier='pro'` et user Free → 403 PLAN_REQUIRED.
    2. Résolution source image (3 modes) → bytes + mime + source_*_id.
       - upload_id → FileUploadService.get_for_user + ObjectStore.download.
       - library_id → LibraryService.get + ObjectStore.download.
       - image_base64 → decode + validate data URL.
    3. MIME whitelist image → 415 FILE_TYPE_NOT_ALLOWED.
    4. Cap taille 10 MB → 413 IMAGE_TOO_LARGE.
    5. Magic-bytes check (réutilise E3) → 415 FILE_CONTENT_MISMATCH.
    6. Resize Pillow si dims > max_dimension → économie tokens.
    7. Résolution additional_images (max 3) → itération 2-6.
    8. Estimation tokens image totaux → 402 LLM_QUOTA_EXCEEDED si > cap.
    9. SHA-256 image_combined + prompt.
   10. Dédup actif → retourne existing sans appel provider.
   11. Quota pré-flight vision_images_{free,pro}_per_day → 402 VISION_QUOTA_EXCEEDED.
   12. BudgetTracker.check_and_consume_vision_images.
   13. Rate limit user-scoped vision_analyze 30/h → 429.
   14. provider.analyze_images() avec VISION_SYSTEM_INSTRUCTION + INSERT + log forensic.

Fail-safe provider down → refund compteur + 503 VISION_UNAVAILABLE.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import uuid
from decimal import Decimal
from typing import Final

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.budget_tracker import get_budget_tracker
from app.ai.vision import (
    ImageInput,
    VisionContentFilteredError,
    VisionError,
    VisionResult,
    get_vision_provider,
)
from app.config import settings
from app.core.errors.exceptions import (
    FileContentMismatchException,
    FileTypeNotAllowedException,
    ImageTooLargeException,
    LlmQuotaExceededException,
    PlanRequiredException,
    RateLimitAbuseException,
    ValidationException,
    VisionContentFilteredException,
    VisionQuotaExceededException,
    VisionUnavailableException,
)
from app.core.security.rate_limiter import check_user_rate_limit
from app.core.storage import detect_mime_type, get_object_store, mimes_compatible
from app.features.auth.models import User
from app.features.files.service import FileUploadService
from app.features.library.service import LibraryService
from app.features.vision.image_utils import (
    ResizedImage,
    estimate_gemini_image_tokens,
    resize_image_if_needed,
)
from app.features.vision.models import VisionAnalysis
from app.features.vision.prompt_safety import VISION_SYSTEM_INSTRUCTION
from app.features.vision.schemas import VisionAnalyzeRequest

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

_DATA_URL_PREFIX: Final[str] = "data:"
_MAX_IMAGES_TOTAL: Final[int] = 4  # 1 primary + 3 additional


# ══════════════════════════════════════════════════════════════
# VisionService
# ══════════════════════════════════════════════════════════════


class VisionService:
    """Pipeline analyse image Free + Pro."""

    # ── Résolution source image → bytes + mime ──────────────────
    @staticmethod
    async def _resolve_image_source(
        user: User,
        db: AsyncSession,
        *,
        body: VisionAnalyzeRequest,
    ) -> tuple[bytes, str, uuid.UUID | None, uuid.UUID | None]:
        """Retourne `(bytes, mime_type, source_file_id, source_library_id)`
        selon le mode d'entrée choisi."""
        if body.image_source == "upload_id":
            assert body.upload_id is not None
            upload = await FileUploadService.get_for_user(body.upload_id, user, db)
            store = get_object_store()
            data = await store.download_bytes(upload.storage_key)
            return data, upload.mime_type, upload.id, None

        if body.image_source == "library_id":
            assert body.library_id is not None
            item = await LibraryService.get(body.library_id, user, db)
            if item.type != "image":
                raise ValidationException("L'item Library fourni n'est pas une image.")
            store = get_object_store()
            data = await store.download_bytes(item.storage_key)
            return data, item.mime_type, None, item.id

        # Mode base64.
        assert body.image_base64 is not None
        data, mime = VisionService._decode_data_url(body.image_base64)
        return data, mime, None, None

    @staticmethod
    def _decode_data_url(data_url: str) -> tuple[bytes, str]:
        """Decode une data URL `data:image/png;base64,...` ou du base64 brut.

        Accepte les deux formes pour robustesse :
        - `data:image/png;base64,iVBORw0KGgo...` (forme canonique)
        - `iVBORw0KGgo...` (base64 brut, on devine le MIME via magic plus tard)
        """
        stripped = data_url.strip()
        if stripped.startswith(_DATA_URL_PREFIX):
            # Format data:<mime>;base64,<payload>
            try:
                header, payload = stripped.split(",", 1)
                mime_part = header[len(_DATA_URL_PREFIX) :].split(";", 1)[0]
                mime_type = mime_part or "image/png"
            except ValueError as exc:
                raise ValidationException("Data URL image invalide.") from exc
            raw = payload
        else:
            # Base64 brut — on devinera le MIME via magic-bytes ensuite.
            mime_type = "image/png"  # par défaut, corrigé plus tard
            raw = stripped

        try:
            data = base64.b64decode(raw, validate=True)
        except Exception as exc:  # noqa: BLE001
            raise ValidationException(f"Décodage base64 image invalide : {exc}") from exc
        return data, mime_type.lower()

    # ── Validation image individuelle ───────────────────────────
    @staticmethod
    def _validate_image_bytes(data: bytes, announced_mime: str) -> str:
        """Cap taille + MIME whitelist + magic-bytes. Retourne le MIME
        effectif (après réconciliation magic-bytes si besoin)."""
        # Cap taille.
        if len(data) > settings.vision_max_image_bytes:
            raise ImageTooLargeException(
                size_bytes=len(data),
                max_bytes=settings.vision_max_image_bytes,
            )

        # MIME whitelist.
        announced = announced_mime.lower()
        if announced not in {m.lower() for m in settings.vision_allowed_mimes}:
            raise FileTypeNotAllowedException(mime_type=announced)

        # Magic-bytes check (réutilise E3).
        detected = detect_mime_type(data[:4096])
        if detected is None:
            # Tolérance : on garde le MIME annoncé, certains formats
            # rares ne matchent pas nos signatures. Le provider LLM
            # aura sa propre validation.
            return announced
        if not mimes_compatible(announced, detected):
            raise FileContentMismatchException(announced=announced, detected=detected)
        return detected or announced

    # ── Pipeline analyze ────────────────────────────────────────
    @staticmethod
    async def analyze(
        user: User,
        db: AsyncSession,
        *,
        body: VisionAnalyzeRequest,
    ) -> VisionAnalysis:
        """Pipeline 14 étapes strict."""
        user_id_str = str(user.id)

        # 1. Tier Pro → require is_pro.
        if body.model_tier == "pro" and not getattr(user, "is_pro", False):
            raise PlanRequiredException()

        # 2. Résolution source principale.
        (
            data_main,
            mime_main,
            source_file_id,
            source_library_id,
        ) = await VisionService._resolve_image_source(user, db, body=body)

        # 3-5. Validation image principale.
        mime_main = VisionService._validate_image_bytes(data_main, mime_main)

        # 6. Resize Pillow (CPU-bound en thread).
        resized_main: ResizedImage = await asyncio.to_thread(
            resize_image_if_needed,
            data_main,
            mime_main,
            max_dimension=settings.vision_max_dimension,
        )

        # 7. Résolution additional_images (max 3).
        image_inputs: list[ImageInput] = [
            ImageInput(
                data=resized_main.data,
                mime_type=resized_main.mime_type,
                width=resized_main.width or None,
                height=resized_main.height or None,
            )
        ]
        if body.additional_images:
            if len(body.additional_images) + 1 > _MAX_IMAGES_TOTAL:
                raise ValidationException(f"Maximum {_MAX_IMAGES_TOTAL} images par requête.")
            store = get_object_store()
            for add_id in body.additional_images:
                add_upload = await FileUploadService.get_for_user(add_id, user, db)
                add_data = await store.download_bytes(add_upload.storage_key)
                add_mime = VisionService._validate_image_bytes(add_data, add_upload.mime_type)
                add_resized = await asyncio.to_thread(
                    resize_image_if_needed,
                    add_data,
                    add_mime,
                    max_dimension=settings.vision_max_dimension,
                )
                image_inputs.append(
                    ImageInput(
                        data=add_resized.data,
                        mime_type=add_resized.mime_type,
                        width=add_resized.width or None,
                        height=add_resized.height or None,
                    )
                )

        # 8. Estimation tokens image totaux + cap.
        total_image_tokens = sum(
            estimate_gemini_image_tokens(img.width or 0, img.height or 0) for img in image_inputs
        )
        if total_image_tokens > settings.vision_max_input_tokens_per_request:
            log.info(
                "vision.analyze.tokens_cap_exceeded",
                estimated=total_image_tokens,
                cap=settings.vision_max_input_tokens_per_request,
            )
            raise LlmQuotaExceededException()

        # 9. SHA-256 images combinées + prompt.
        img_hasher = hashlib.sha256()
        for img in image_inputs:
            img_hasher.update(img.data)
        image_sha = img_hasher.hexdigest()
        prompt_sha = hashlib.sha256(body.prompt.encode("utf-8")).hexdigest()

        # 10. Dédup actif.
        existing_q = await db.execute(
            select(VisionAnalysis).where(
                VisionAnalysis.user_id == user.id,
                VisionAnalysis.image_sha256 == image_sha,
                VisionAnalysis.prompt_sha256 == prompt_sha,
                VisionAnalysis.deleted_at.is_(None),
            )
        )
        existing = existing_q.scalar_one_or_none()
        if existing is not None:
            log.info(
                "vision.analyze.dedup_hit",
                user_id=user_id_str,
                row_id=str(existing.id),
                sha=image_sha[:16],
            )
            return existing

        # 11+12. Quota + BudgetTracker.
        plan_is_pro = bool(getattr(user, "is_pro", False))
        max_images = (
            settings.vision_images_pro_per_day
            if plan_is_pro
            else settings.vision_images_free_per_day
        )
        plan_label = "pro" if plan_is_pro else "free"

        tracker = get_budget_tracker()
        tracker.user_vision_images_per_day = max_images
        try:
            await tracker.check_and_consume_vision_images(user_id_str, images=1)
        except Exception as exc:
            if exc.__class__.__name__ == "RateLimitExceededException":
                raise VisionQuotaExceededException(
                    current=max_images,
                    maximum=max_images,
                    plan=plan_label,
                ) from exc
            raise

        # 13. Rate limit user-scoped.
        await check_user_rate_limit(
            user.id,
            action="vision_analyze",
            max_requests=settings.vision_rate_limit_per_hour,
            window_seconds=3600,
            on_exceeded=RateLimitAbuseException,
        )

        # 14. Appel provider.
        provider = get_vision_provider(body.model_tier)
        try:
            result: VisionResult = await provider.analyze_images(
                image_inputs,
                body.prompt,
                tier=body.model_tier,
                system_prompt=VISION_SYSTEM_INSTRUCTION,
                max_output_tokens=body.max_output_tokens,
            )
        except VisionContentFilteredError as exc:
            log.info(
                "vision.analyze.content_filtered",
                user_id=user_id_str,
                provider=exc.provider,
            )
            # Rembourse l'image (on n'a pas pu la traiter).
            await tracker.refund_vision_images(user_id_str, images=1)
            raise VisionContentFilteredException(provider=exc.provider) from exc
        except VisionError as exc:
            log.warning(
                "vision.analyze.provider_error",
                user_id=user_id_str,
                provider=exc.provider,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            await tracker.refund_vision_images(user_id_str, images=1)
            raise VisionUnavailableException(provider=exc.provider, reason=str(exc)) from exc

        # INSERT VisionAnalysis + log forensic.
        row = VisionAnalysis(
            user_id=user.id,
            source_file_id=source_file_id,
            source_library_id=source_library_id,
            image_sha256=image_sha,
            prompt_sha256=prompt_sha,
            prompt=body.prompt,
            analysis_text=result.text,
            model=result.model,
            provider=result.provider,
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            cost_usd=Decimal(str(result.cost_usd)),
            image_width=image_inputs[0].width,
            image_height=image_inputs[0].height,
            metadata_json={
                "tier": body.model_tier,
                "n_images": len(image_inputs),
                "max_output_tokens": body.max_output_tokens,
            },
        )
        db.add(row)
        await db.commit()
        await db.refresh(row)

        log.info(
            "vision.analyze.completed",
            user_id=user_id_str,
            row_id=str(row.id),
            model=result.model,
            provider=result.provider,
            tier=body.model_tier,
            n_images=len(image_inputs),
            tokens_input=result.tokens_input,
            tokens_output=result.tokens_output,
            cost_usd=result.cost_usd,
            plan=plan_label,
        )
        return row
