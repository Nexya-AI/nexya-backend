"""Avatar profil — pipeline lean dédié `POST /user/avatar`.

Pourquoi un pipeline dédié plutôt que `FileUploadService` (E3) ?

`FileUploadService` fait du scan virus + extraction texte + dédup SHA +
INSERT `uploaded_files` + enqueue RAG + quota documents. Tout ça est
inutile (et coûteux) pour un avatar : c'est juste une petite image carrée
512² (~30-80 KB) déjà resizée côté client. On garde donc uniquement les
trois validations de sécurité qui comptent vraiment :

  1. MIME annoncé ∈ whitelist image (`avatar_allowed_mimes`).
  2. Taille ≤ `avatar_max_upload_bytes` (5 MB, marge anti image brute).
  3. **Magic-bytes** (`detect_mime_type`) cohérents avec le MIME annoncé
     — anti-smuggling (un `.exe` déguisé en `image/png`).

Clé de stockage **FIXE par user** : `users/{user_id}/avatar.{ext}`. Un
ré-upload écrase le blob précédent → **zéro orphelin**, aucun cron de
nettoyage. La presigned est régénérée à chaque lecture du profil
(`build_profile_response`), donc fraîche en permanence et bustée
naturellement côté cache client (signature + expiry changent à chaque GET).

**Module sans cycle** : `avatar.py` n'importe RIEN de `auth/service.py`.
C'est l'inverse — `service.py` importe `build_profile_response` +
`delete_avatar_blob_best_effort` d'ici. Sens unique, pas de circularité.
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog
from fastapi import UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors.exceptions import (
    FileContentMismatchException,
    FileTypeNotAllowedException,
    ImageTooLargeException,
    ResourceNotFoundException,
)
from app.core.storage import (
    ObjectStore,
    detect_mime_type,
    get_object_store,
    mimes_compatible,
)
from app.features.auth.models import User
from app.features.auth.schemas import UserProfile

log = structlog.get_logger()


_READ_CHUNK_SIZE: Final[int] = 8 * 1024  # 8 KB par chunk de lecture
_MAGIC_PROBE_BYTES: Final[int] = 4096  # inspecte les premiers 4 KB pour magic

# Extension de la clé MinIO dérivée du MIME **détecté** (source de vérité).
# Restreint aux 3 formats avatar acceptés.
_MIME_TO_EXT: Final[dict[str, str]] = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
}


def _ext_for_mime(mime: str) -> str:
    return _MIME_TO_EXT.get(mime.lower(), "jpg")


# Chemin de l'endpoint API qui stream l'avatar (proxy authentifié). Doit rester
# aligné sur le `@router.get(...)` correspondant dans `auth/router.py`.
AVATAR_ROUTE: Final[str] = "/user/avatar"

# Reverse map extension → MIME, pour servir le bon Content-Type au stream.
_EXT_TO_MIME: Final[dict[str, str]] = {
    "jpg": "image/jpeg",
    "png": "image/png",
    "webp": "image/webp",
}


def _mime_for_key(key: str) -> str:
    """MIME dérivé de l'extension de la clé MinIO (`users/{id}/avatar.{ext}`)."""
    ext = key.rsplit(".", 1)[-1].lower() if "." in key else "jpg"
    return _EXT_TO_MIME.get(ext, "application/octet-stream")


def build_avatar_url(user: User) -> str | None:
    """Chemin RELATIF authentifié vers l'avatar — **PAS** un presigned MinIO.

    En prod, MinIO n'a aucun port public (réseau Docker interne) : un presigned
    `http://minio:9000/...` est physiquement **injoignable depuis le téléphone**
    (même cause racine que le bug doc-download du 2026-06-10). On renvoie donc un
    chemin relatif vers `GET /user/avatar` — endpoint API authentifié qui stream
    le blob depuis MinIO interne (le backend, lui, est dans le réseau Docker).
    Le dio Flutter (`apiClientProvider`, baseUrl=api.nexyalabs.com + JWT Bearer)
    le résout et y attache le token.

    Cache-bust `?v={updated_at}` : un ré-upload bump `users.updated_at`
    (UUIDMixin `onupdate`) → l'URL change → le client re-télécharge la nouvelle
    photo. Sans ce token, Flutter ImageCache (clé = chemin) servirait l'ancienne
    image décodée (clé identique = cache HIT) malgré le changement de photo.
    """
    if not user.avatar_storage_key:
        return None
    version = int(user.updated_at.timestamp()) if user.updated_at else 0
    return f"{AVATAR_ROUTE}?v={version}"


def _build_avatar_storage_key(user_id: uuid.UUID, mime: str) -> str:
    """Clé MinIO FIXE par user — un ré-upload écrase le blob précédent."""
    return f"users/{user_id}/avatar.{_ext_for_mime(mime)}"


async def _read_capped(upload_file: UploadFile, *, max_bytes: int) -> bytes:
    """Lit le `UploadFile` par chunks, stoppe dès que `max_bytes` est franchi.

    Lève `ImageTooLargeException` (413) sans chercher à tout lire
    (interruption précoce — un attaquant qui poste 500 MB ne fait lire
    que `max_bytes + 1` chunk au serveur).
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await upload_file.read(_READ_CHUNK_SIZE)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise ImageTooLargeException(size_bytes=total, max_bytes=max_bytes)
        chunks.append(chunk)
    return b"".join(chunks)


async def build_profile_response(user: User) -> UserProfile:
    """Construit le `UserProfile` avec l'URL avatar relative authentifiée.

    Source de vérité = `user.avatar_storage_key`. Si présente → chemin relatif
    vers le proxy API `GET /user/avatar` (cf. `build_avatar_url`). Sinon →
    `avatar_url = None`. La colonne legacy `avatar_url` (désormais toujours NULL)
    est ignorée.

    Reste `async` pour préserver la signature de tous les call-sites (`await …`),
    même si plus aucun I/O n'est requis : l'URL n'est plus une presigned MinIO
    (injoignable depuis le device) mais un simple chemin relatif. Conséquence :
    plus aucune dépendance au store ici, et un `GET /user/profile` ne peut plus
    jamais échouer sur une presign ratée.
    """
    profile = UserProfile.model_validate(user)
    return profile.model_copy(update={"avatar_url": build_avatar_url(user)})


async def delete_avatar_blob_best_effort(
    storage_key: str | None,
    *,
    store: ObjectStore | None = None,
) -> None:
    """Supprime le blob avatar de MinIO en best-effort (idempotent).

    Réutilisé par les deux chemins d'anonymisation RGPD
    (`auth.delete_account` legacy + `rgpd.DeletionRequestService.create_request`).
    Ne lève JAMAIS : un avatar qui survit 1 jour de plus n'est pas un
    incident, alors qu'une exception bloquerait la suppression de compte.
    """
    if not storage_key:
        return
    try:
        st = store if store is not None else get_object_store()
        await st.delete_object(storage_key)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "avatar.blob_delete_failed",
            storage_key=storage_key,
            error=str(exc),
        )


class AvatarService:
    """Upload / suppression de l'avatar de l'utilisateur courant."""

    @staticmethod
    async def upload_avatar(
        user: User,
        db: AsyncSession,
        *,
        upload_file: UploadFile,
        store: ObjectStore | None = None,
    ) -> UserProfile:
        """Pipeline lean : valide, upload MinIO (clé fixe), persiste la clé.

        Lève :
        - `FileTypeNotAllowedException` (415) si MIME annoncé hors whitelist.
        - `ImageTooLargeException` (413) si > `avatar_max_upload_bytes`.
        - `FileContentMismatchException` (415) si magic-bytes ≠ MIME annoncé.
        - `ObjectStoreUnavailableException` (503) si MinIO down (propagée
          depuis `ObjectStore.upload_bytes`).
        """
        st = store if store is not None else get_object_store()

        # 1. MIME annoncé dans la whitelist avatar (image-only strict).
        announced = (upload_file.content_type or "").lower()
        if announced not in {m.lower() for m in settings.avatar_allowed_mimes}:
            log.info(
                "avatar.upload.mime_rejected",
                mime=announced,
                user_id=str(user.id),
            )
            raise FileTypeNotAllowedException(mime_type=announced)

        # 2. Lecture cappée (interruption précoce si trop gros).
        data = await _read_capped(upload_file, max_bytes=settings.avatar_max_upload_bytes)

        # 3. Magic-bytes cohérents avec le MIME annoncé (anti-smuggling).
        detected = detect_mime_type(data[:_MAGIC_PROBE_BYTES])
        if detected is None:
            log.info("avatar.upload.magic_unknown", announced=announced, user_id=str(user.id))
            raise FileContentMismatchException(announced=announced, detected="")
        if not mimes_compatible(announced, detected):
            log.warning(
                "avatar.upload.mime_mismatch",
                announced=announced,
                detected=detected,
                user_id=str(user.id),
            )
            raise FileContentMismatchException(announced=announced, detected=detected)

        # 4. Clé FIXE par user — un ré-upload écrase le blob précédent.
        new_key = _build_avatar_storage_key(user.id, detected)

        # 5. Si l'ancien avatar avait une extension différente (jpg → png),
        #    la clé change → on supprime l'ancien blob best-effort pour ne
        #    pas laisser d'orphelin. (Même extension → overwrite, pas de delete.)
        old_key = user.avatar_storage_key
        if old_key and old_key != new_key:
            await delete_avatar_blob_best_effort(old_key, store=st)

        # 6. Upload MinIO.
        await st.upload_bytes(
            new_key,
            data,
            mime_type=detected,
            metadata={
                "user_id": str(user.id),
                "kind": "avatar",
                "mime_detected": detected,
            },
        )

        # 7. Persiste la clé. La colonne legacy `avatar_url` reste NULL
        #    (l'URL est régénérée à la lecture).
        user.avatar_storage_key = new_key
        user.avatar_url = None
        await db.flush()

        log.info(
            "avatar.upload.completed",
            user_id=str(user.id),
            mime=detected,
            size_bytes=len(data),
            storage_key=new_key,
        )
        return await build_profile_response(user)

    @staticmethod
    async def delete_avatar(
        user: User,
        db: AsyncSession,
        *,
        store: ObjectStore | None = None,
    ) -> UserProfile:
        """Supprime l'avatar : blob MinIO best-effort + clé nulle.

        Idempotent : pas d'avatar → no-op, retourne le profil tel quel.
        """
        st = store if store is not None else get_object_store()
        key = user.avatar_storage_key
        if key:
            await delete_avatar_blob_best_effort(key, store=st)
            user.avatar_storage_key = None
            user.avatar_url = None
            await db.flush()
            log.info("avatar.delete.completed", user_id=str(user.id), storage_key=key)
        return await build_profile_response(user)

    @staticmethod
    async def fetch_avatar_blob(
        user: User,
        *,
        store: ObjectStore | None = None,
    ) -> tuple[bytes, str]:
        """Télécharge le blob avatar depuis MinIO pour le streamer via l'API.

        C'est le cœur du proxy `GET /user/avatar` : le backend (dans le réseau
        Docker) joint MinIO, l'API (TLS + Caddy) est joignable depuis le
        téléphone. Résout le presigned `minio:9000` injoignable.

        Retourne `(bytes, content_type)`. Lève :
        - `ResourceNotFoundException` (404) si pas d'avatar OU blob introuvable
          côté MinIO (anti-énumération : un user sans avatar et un blob purgé
          renvoient le même 404).
        - `ObjectStoreUnavailableException` (503) si MinIO est down (propagée
          depuis `ObjectStore.download_bytes`).
        """
        key = user.avatar_storage_key
        if not key:
            raise ResourceNotFoundException("Avatar")
        st = store if store is not None else get_object_store()
        try:
            data = await st.download_bytes(key)
        except FileNotFoundError as exc:
            raise ResourceNotFoundException("Avatar") from exc
        return data, _mime_for_key(key)
