"""
LibraryService — logique métier de la Bibliothèque utilisateur.

Pattern NEXYA : méthodes statiques, AsyncSession en paramètre, commit en
fin de chaque méthode publique, pas d'état injecté.

Points critiques :

- **Dédup par content-SHA256.** La `storage_key` intègre le hash SHA-256
  du contenu binaire : une même image générée deux fois par le même user
  partage la même clé. L'UNIQUE partiel `(user_id, storage_key) WHERE
  deleted_at IS NULL` côté DB + `INSERT ... ON CONFLICT DO NOTHING
  RETURNING` côté service permet un re-upload idempotent : on retourne
  l'entrée existante sans erreur. Économie storage + UX parfaite (le
  « Enregistrer dans ma biblio » peut être tapé 3 fois, pas de duplicate).

- **Upload avant INSERT DB.** Ordre critique : on upload vers MinIO avant
  le COMMIT DB. Si l'INSERT échoue (conflit rare, contrainte CHECK
  violée), on a un orphelin sur MinIO — on logge et on laisse le cron
  de cleanup Phase 12 le nettoyer. Alternative (INSERT avant upload)
  créerait un orphelin en DB si l'upload rate — pire, car visible côté
  user avec une URL cassée. Le compromis favorise la cohérence user-side
  (mieux un orphelin storage silencieux qu'une entrée DB visible et
  cassée).

- **Quota pré-flight en Python, pas CHECK DB** — même pattern que
  Projects. Le plan user (Free/Pro) change dynamiquement à la
  souscription, on ne peut pas figer le plafond au DDL.

- **Presigned URL générée à la demande.** Chaque `LibraryItemResponse`
  porte une URL fraîche. Pas de pré-génération au POST ni cache DB —
  l'URL expire, la re-génération est gratuite (HMAC local).

- **Soft-delete sans suppression MinIO synchrone.** `DELETE /library/{id}`
  pose `deleted_at=NOW()`, c'est tout. Un cron de Phase 12 purgera les
  objets MinIO `deleted_at < NOW() - 7 days` — on laisse 7 jours de
  fenêtre de restore. Ne pas supprimer sur MinIO dans le chemin user
  signifie qu'une erreur storage ne casse jamais un DELETE logique.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Final, Literal

import structlog
from sqlalchemy import func, select, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors.exceptions import (
    FileTooLargeException,
    LibraryQuotaExceededException,
    ResourceNotFoundException,
    ValidationException,
)
from app.core.storage import ObjectStore, get_object_store
from app.features.auth.models import User
from app.features.library.models import LibraryItem
from app.features.library.schemas import (
    LibraryItemCreate,
    LibraryItemType,
    LibrarySource,
)

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

_DEFAULT_LIMIT: Final[int] = 20
_MAX_LIMIT: Final[int] = 50

# Extensions dérivées du mime pour la storage_key (facilite le debug via
# l'arborescence MinIO — `user/library/image/ab/abcd...png` lisible).
_MIME_TO_EXT: Final[dict[str, str]] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/svg+xml": "svg",
    "video/mp4": "mp4",
    "video/webm": "webm",
    "video/quicktime": "mov",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/wav": "wav",
    "audio/ogg": "ogg",
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
    "text/plain": "txt",
    "text/markdown": "md",
}


# ══════════════════════════════════════════════════════════════
# DTO internes
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class LibraryPageOrm:
    """Page d'items ORM + curseur opaque."""

    items: list[LibraryItem]
    next_cursor: str | None


# ══════════════════════════════════════════════════════════════
# Helpers curseur (même format que chat / projects)
# ══════════════════════════════════════════════════════════════


def _encode_cursor(sort_ts: datetime, row_id: uuid.UUID) -> str:
    raw = f"{sort_ts.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
        iso, sep, row_id_str = raw.partition("|")
        if not sep or not iso or not row_id_str:
            raise ValueError("cursor missing fields")
        sort_ts = datetime.fromisoformat(iso)
        if sort_ts.tzinfo is None:
            sort_ts = sort_ts.replace(tzinfo=UTC)
        row_id = uuid.UUID(row_id_str)
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError) as exc:
        log.warning("library.cursor.invalid", cursor=cursor[:40], error=str(exc))
        raise ValidationException("Curseur de pagination invalide.") from exc
    return sort_ts, row_id


def _clamp_limit(limit: int | None) -> int:
    if limit is None or limit <= 0:
        return _DEFAULT_LIMIT
    return min(limit, _MAX_LIMIT)


def _library_quota(user: User) -> tuple[int, str]:
    """Retourne `(max_items, plan_label)` selon le plan."""
    if user.is_pro:
        return settings.library_max_pro, "pro"
    return settings.library_max_free, "free"


def _guess_extension(mime_type: str) -> str:
    """Devine l'extension de fichier à partir du mime. Fallback `bin`."""
    return _MIME_TO_EXT.get(mime_type.lower(), "bin")


def _build_storage_key(
    user_id: uuid.UUID,
    type_: LibraryItemType,
    content_sha256: str,
    mime_type: str,
) -> str:
    """Clé MinIO canonique avec sharding 2-char du SHA.

    Forme : `{user}/library/{type}/{sha[:2]}/{sha}.{ext}`
    Exemple : `c4a2.../library/image/ab/abcd1234...ffee.png`

    Sharding : évite un bucket flat avec 10k+ objets dans le même
    « dossier » MinIO (certaines UIs admin rament au-delà). 256 shards
    par type × user = largement suffisant.
    """
    ext = _guess_extension(mime_type)
    shard = content_sha256[:2]
    return f"{user_id}/library/{type_}/{shard}/{content_sha256}.{ext}"


# ══════════════════════════════════════════════════════════════
# LibraryService
# ══════════════════════════════════════════════════════════════


class LibraryService:
    """CRUD + upload/presign pour la bibliothèque utilisateur."""

    # ── Owner check 404 IDOR-safe ────────────────────────────────
    @staticmethod
    async def _get_owned_item(
        item_id: uuid.UUID,
        user_id: uuid.UUID,
        db: AsyncSession,
    ) -> LibraryItem:
        result = await db.execute(
            select(LibraryItem).where(
                LibraryItem.id == item_id,
                LibraryItem.user_id == user_id,
                LibraryItem.deleted_at.is_(None),
            )
        )
        item = result.scalar_one_or_none()
        if item is None:
            raise ResourceNotFoundException("Média")
        return item

    # ── Count actif scope user ─────────────────────────────────
    @staticmethod
    async def _count_active(user_id: uuid.UUID, db: AsyncSession) -> int:
        stmt = select(func.count(LibraryItem.id)).where(
            LibraryItem.user_id == user_id,
            LibraryItem.deleted_at.is_(None),
        )
        raw = (await db.execute(stmt)).scalar_one() or 0
        return int(raw)

    # ── CREATE — depuis bytes bruts (utilisé par /image/generate autosave) ─
    @staticmethod
    async def create_from_bytes(
        user: User,
        db: AsyncSession,
        *,
        type_: LibraryItemType,
        title: str,
        data: bytes,
        mime_type: str,
        source: LibrarySource = "uploaded",
        file_type: str | None = None,
        description: str | None = None,
        tags: list[str] | None = None,
        provider: str | None = None,
        model: str | None = None,
        prompt: str | None = None,
        source_conversation_id: uuid.UUID | None = None,
        source_message_id: uuid.UUID | None = None,
        width_px: int | None = None,
        height_px: int | None = None,
        duration_ms: int | None = None,
        aspect_ratio: Any = None,
        metadata_json: dict[str, Any] | None = None,
        store: ObjectStore | None = None,
    ) -> LibraryItem:
        """Upload `data` vers MinIO + INSERT métadonnée en DB.

        Dédup par `(user_id, storage_key)` UNIQUE partiel côté DB. Si le
        même contenu est « sauvé » deux fois par le même user, on
        retourne l'entrée existante sans erreur ni double-upload.

        Lève :
        - `FileTooLargeException` (413) si `len(data) > s3_max_upload_bytes`.
        - `LibraryQuotaExceededException` (402) si le user est au plafond.
        - `ObjectStoreUnavailableException` (503) si MinIO down.
        """
        # 1. Cap taille — avant même de hasher, pour couper vite.
        if len(data) > settings.s3_max_upload_bytes:
            raise FileTooLargeException(max_mb=settings.s3_max_upload_bytes // (1024 * 1024))

        # 2. Quota pré-flight.
        max_items, plan_label = _library_quota(user)
        active = await LibraryService._count_active(user.id, db)
        if active >= max_items:
            raise LibraryQuotaExceededException(current=active, maximum=max_items, plan=plan_label)

        # 3. SHA-256 + storage_key.
        content_sha256 = hashlib.sha256(data).hexdigest()
        storage_key = _build_storage_key(user.id, type_, content_sha256, mime_type)

        # 4. Upload MinIO. Avant INSERT pour que l'entrée DB ait toujours
        # un binaire accessible. Un orphelin storage (INSERT ko après
        # upload ok) est plus tolérable qu'un orphelin DB (URL cassée
        # côté user).
        object_store = store if store is not None else get_object_store()
        await object_store.upload_bytes(
            storage_key,
            data,
            mime_type=mime_type,
            metadata={
                "user_id": str(user.id),
                "type": type_,
                "source": source,
            },
        )

        # 5. INSERT ON CONFLICT DO NOTHING RETURNING.
        # `pg_insert` dialect-specific pour exploiter l'UNIQUE partiel
        # `(user_id, storage_key) WHERE deleted_at IS NULL`.
        insert_stmt = (
            pg_insert(LibraryItem)
            .values(
                user_id=user.id,
                type=type_,
                file_type=file_type,
                title=title,
                description=description,
                storage_key=storage_key,
                mime_type=mime_type,
                size_bytes=len(data),
                content_sha256=content_sha256,
                width_px=width_px,
                height_px=height_px,
                duration_ms=duration_ms,
                aspect_ratio=aspect_ratio,
                source=source,
                provider=provider,
                model=model,
                prompt=prompt,
                source_conversation_id=source_conversation_id,
                source_message_id=source_message_id,
                tags=tags,
                metadata_json=metadata_json,
            )
            # `index_elements=[user_id, storage_key]` cible l'UNIQUE
            # partiel. `index_where` IMPORTANT : l'index partiel ne
            # matche que les rows actives, il faut le déclarer côté
            # ON CONFLICT sinon Postgres ne trouve pas la contrainte.
            .on_conflict_do_nothing(
                index_elements=["user_id", "storage_key"],
                index_where=LibraryItem.deleted_at.is_(None),
            )
            .returning(LibraryItem)
        )
        result = await db.execute(insert_stmt)
        item = result.scalar_one_or_none()

        if item is None:
            # Dédup déclenchée — on récupère l'existant.
            existing = await db.execute(
                select(LibraryItem).where(
                    LibraryItem.user_id == user.id,
                    LibraryItem.storage_key == storage_key,
                    LibraryItem.deleted_at.is_(None),
                )
            )
            item = existing.scalar_one_or_none()
            if item is None:
                # Ne devrait jamais arriver (ON CONFLICT + WHERE actifs).
                # Garde-fou pour un bug d'index.
                raise ValidationException("Conflit inattendu sur la sauvegarde du média.")
            log.info(
                "library.create.dedup_hit",
                user_id=str(user.id),
                item_id=str(item.id),
                sha=content_sha256[:16],
            )
        else:
            await db.commit()
            await db.refresh(item)
            log.info(
                "library.created",
                user_id=str(user.id),
                item_id=str(item.id),
                type=type_,
                size_bytes=len(data),
                source=source,
                provider=provider,
                sha=content_sha256[:16],
            )

        return item

    # ── CREATE — wrapper base64 pour `POST /library` ──────────────
    @staticmethod
    async def create_from_base64(
        user: User,
        db: AsyncSession,
        body: LibraryItemCreate,
        *,
        store: ObjectStore | None = None,
    ) -> LibraryItem:
        """Décode `body.content_base64` puis délègue à `create_from_bytes`."""
        try:
            data = base64.b64decode(body.content_base64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValidationException(
                "Le champ content_base64 n'est pas un base64 valide."
            ) from exc

        return await LibraryService.create_from_bytes(
            user,
            db,
            type_=body.type,
            title=body.title,
            data=data,
            mime_type=body.mime_type,
            source=body.source,
            file_type=body.file_type,
            description=body.description,
            tags=body.tags,
            provider=body.provider,
            model=body.model,
            prompt=body.prompt,
            source_conversation_id=body.source_conversation_id,
            source_message_id=body.source_message_id,
            width_px=body.width_px,
            height_px=body.height_px,
            duration_ms=body.duration_ms,
            aspect_ratio=body.aspect_ratio,
            metadata_json=body.metadata_json,
            store=store,
        )

    # ── LIST paginée cursor-based + filtres combinables ──────────
    @staticmethod
    async def list_for_user(
        user: User,
        db: AsyncSession,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        type_: LibraryItemType | None = None,
        source: LibrarySource | None = None,
        conversation_id: uuid.UUID | None = None,
        q: str | None = None,
    ) -> LibraryPageOrm:
        """Liste paginée des items actifs — tri `(created_at, id) DESC`.

        Filtres combinables :
        - `type_` — exploite `idx_library_user_type` quand seul.
        - `source` — pas d'index dédié (cardinalité basse, filtre post).
        - `conversation_id` — exploite `idx_library_user_conversation`.
        - `q` — ILIKE `%q%` sur `title`, accéléré par le GIN trigram
          `idx_library_title_trgm`. Whitespace-only traité comme None.
        """
        effective_limit = _clamp_limit(limit)

        conditions: list = [
            LibraryItem.user_id == user.id,
            LibraryItem.deleted_at.is_(None),
        ]
        if type_ is not None:
            conditions.append(LibraryItem.type == type_)
        if source is not None:
            conditions.append(LibraryItem.source == source)
        if conversation_id is not None:
            conditions.append(LibraryItem.source_conversation_id == conversation_id)
        if q is not None:
            q_stripped = q.strip()
            if q_stripped:
                conditions.append(LibraryItem.title.ilike(f"%{q_stripped}%"))

        if cursor:
            cursor_ts, cursor_id = _decode_cursor(cursor)
            conditions.append(
                tuple_(LibraryItem.created_at, LibraryItem.id) < tuple_(cursor_ts, cursor_id)
            )

        stmt = (
            select(LibraryItem)
            .where(*conditions)
            .order_by(LibraryItem.created_at.desc(), LibraryItem.id.desc())
            .limit(effective_limit + 1)
        )
        result = await db.execute(stmt)
        rows = list(result.scalars().all())

        has_next = len(rows) > effective_limit
        items = rows[:effective_limit]
        next_cursor: str | None = None
        if has_next and items:
            last = items[-1]
            next_cursor = _encode_cursor(last.created_at, last.id)

        return LibraryPageOrm(items=items, next_cursor=next_cursor)

    # ── GET par id ──────────────────────────────────────────────
    @staticmethod
    async def get(
        item_id: uuid.UUID,
        user: User,
        db: AsyncSession,
    ) -> LibraryItem:
        return await LibraryService._get_owned_item(item_id, user.id, db)

    # ── SOFT DELETE — aucune suppression MinIO synchrone ─────────
    @staticmethod
    async def soft_delete(
        item_id: uuid.UUID,
        user: User,
        db: AsyncSession,
    ) -> None:
        item = await LibraryService._get_owned_item(item_id, user.id, db)
        now = datetime.now(UTC)
        item.deleted_at = now
        item.updated_at = now
        await db.commit()
        log.info(
            "library.soft_deleted",
            user_id=str(user.id),
            item_id=str(item.id),
            storage_key_tail=item.storage_key[-32:],
        )
        # Pas de delete MinIO ici. Un cron de Phase 12 nettoiera les
        # objets dont `deleted_at < NOW() - 7 days` — marge de sécurité
        # pour un éventuel restore.

    # ── Helper : presigned URL pour un item chargé ──────────────
    @staticmethod
    async def presigned_url_for(
        item: LibraryItem,
        *,
        ttl_seconds: int | None = None,
        method: Literal["GET", "PUT"] = "GET",
        store: ObjectStore | None = None,
    ) -> str:
        """Génère une presigned URL pour un item déjà chargé (ou pseudo).

        Utilisé par le router pour enrichir `LibraryItemResponse.url` sans
        toucher la DB. Lève `ObjectStoreUnavailableException` si MinIO
        est down (bien qu'en principe un presign = HMAC local).
        """
        object_store = store if store is not None else get_object_store()
        ttl = ttl_seconds or settings.s3_presigned_ttl_seconds
        return await object_store.generate_presigned_url(
            item.storage_key, ttl_seconds=ttl, method=method
        )
