"""
Schémas Pydantic Library — Request/Response pour `/library`.

Conventions NEXYA :
- `Literal[...]` pour les enums (miroir CHECK SQL).
- Validators stricts pour faire échouer tôt (422) les combos invalides
  type ↔ mime_type ↔ file_type.
- `model_config = {"from_attributes": True}` pour les DTOs nourris d'ORM.
- `storage_key` / `content_sha256` JAMAIS exposés côté client — fuites
  potentielles (devinabilité de clés, collisions volontaires).

Pourquoi la validation serrée :

- Un client qui poste `type='image'` avec `mime_type='application/pdf'`
  est soit buggé, soit hostile. L'un comme l'autre mérite 422, pas une
  corruption silencieuse de la biblio.

- `type='document'` **requiert** `file_type` (sinon comment afficher le
  badge PDF/DOCX côté Flutter ?). À l'inverse, `type='image'` **refuse**
  `file_type` (non applicable). Le validator dédié garantit la cohérence.

- `content_base64` plafonné à 28 MB base64 ≈ 20 MB binaire — protège le
  backend d'un POST de 500 MB qui saturerait la RAM avant même d'arriver
  au service. Le cap dur final vit côté service (`s3_max_upload_bytes`).
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ══════════════════════════════════════════════════════════════
# Types communs — miroirs des CHECK SQL
# ══════════════════════════════════════════════════════════════

LibraryItemType = Literal[
    "image", "video", "gif", "audio", "document", "text", "code"
]
LibraryFileType = Literal["pdf", "docx", "xlsx", "pptx", "other", "zip"]
LibrarySource = Literal["generated", "uploaded", "imported", "shared"]


# ══════════════════════════════════════════════════════════════
# Bornes applicatives
# ══════════════════════════════════════════════════════════════

_TITLE_MAX = 200
_DESCRIPTION_MAX = 2000
_PROMPT_MAX = 4000
_TAG_MAX_CHARS = 32
_TAGS_MAX_COUNT = 10
# 28 MB base64 → ≈ 20.5 MB binaire (base64 overhead ~33 %). Le cap binaire
# dur vit côté service (settings.s3_max_upload_bytes) après décodage.
_CONTENT_BASE64_MAX = 28 * 1024 * 1024
_MIME_REGEX = re.compile(r"^[a-z0-9]+/[a-z0-9.+-]+$")


# ══════════════════════════════════════════════════════════════
# Helpers validation — couplage type ↔ mime ↔ file_type
# ══════════════════════════════════════════════════════════════


def _expected_mime_prefix(type_: LibraryItemType) -> str | None:
    """Retourne le préfixe MIME attendu pour un type donné, ou None si
    pas de contrainte stricte (document / text peuvent être variés)."""
    return {
        "image": "image/",
        "gif": "image/",  # les GIFs sont servis en image/gif
        "video": "video/",
        "audio": "audio/",
    }.get(type_)


# ══════════════════════════════════════════════════════════════
# CREATE
# ══════════════════════════════════════════════════════════════


class LibraryItemCreate(BaseModel):
    """Création d'un item de biblio avec le binaire en base64.

    Session C3 livre l'upload base64 (suffit pour images générées,
    screenshots, petits fichiers). L'upload multipart streaming arrive
    en E3 (`POST /files/upload`) — on gardera la sémantique `type` +
    `file_type` identique.
    """

    type: LibraryItemType
    file_type: LibraryFileType | None = None
    title: str = Field(min_length=1, max_length=_TITLE_MAX)
    description: str | None = Field(default=None, max_length=_DESCRIPTION_MAX)

    content_base64: str = Field(min_length=1, max_length=_CONTENT_BASE64_MAX)
    mime_type: str = Field(min_length=3, max_length=128)

    tags: list[str] | None = Field(default=None, max_length=_TAGS_MAX_COUNT)

    source: LibrarySource = "uploaded"
    provider: str | None = Field(default=None, max_length=32)
    model: str | None = Field(default=None, max_length=64)
    prompt: str | None = Field(default=None, max_length=_PROMPT_MAX)

    source_conversation_id: uuid.UUID | None = None
    source_message_id: uuid.UUID | None = None

    # Hints optionnels — si le client les a déjà calculés, pas de
    # re-calcul serveur (pas de PIL au C3). Sinon null.
    width_px: int | None = Field(default=None, gt=0)
    height_px: int | None = Field(default=None, gt=0)
    duration_ms: int | None = Field(default=None, ge=0)
    aspect_ratio: Decimal | None = Field(default=None, gt=0)

    metadata_json: dict[str, Any] | None = None

    # ── Validators unitaires ──────────────────────────────────

    @field_validator("title")
    @classmethod
    def title_not_blank(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Le titre ne peut pas être vide.")
        return stripped

    @field_validator("description")
    @classmethod
    def description_strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @field_validator("mime_type")
    @classmethod
    def mime_type_format(cls, v: str) -> str:
        v = v.strip().lower()
        if not _MIME_REGEX.match(v):
            raise ValueError("Le champ mime_type doit avoir la forme `type/subtype`.")
        return v

    @field_validator("tags")
    @classmethod
    def tags_normalize(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        cleaned: list[str] = []
        for tag in v:
            stripped = tag.strip().lower()
            if not stripped:
                continue
            if len(stripped) > _TAG_MAX_CHARS:
                raise ValueError(f"Chaque tag doit faire au maximum {_TAG_MAX_CHARS} caractères.")
            cleaned.append(stripped)
        # Dédup en préservant l'ordre.
        seen: set[str] = set()
        deduped = [t for t in cleaned if not (t in seen or seen.add(t))]
        return deduped or None

    # ── Validator transverse : cohérence type / file_type / mime ─

    @model_validator(mode="after")
    def check_type_consistency(self) -> LibraryItemCreate:
        # 1. file_type sémantique selon le type :
        #    - 'document' : file_type OBLIGATOIRE (pdf/docx/xlsx/pptx/other).
        #    - 'code' (C4.6) : file_type OPTIONNEL — NULL pour single
        #      code file (.py/.dart/etc.) OU 'zip' pour projet
        #      multi-fichiers sauvegardé via NxCodeProjectCard.
        #    - autres types (image/video/gif/audio/text) : file_type
        #      INTERDIT (non applicable).
        if self.type == "document":
            if self.file_type is None:
                raise ValueError("Le champ file_type est obligatoire pour type='document'.")
            if self.file_type == "zip":
                raise ValueError(
                    "file_type='zip' est réservé à type='code' (projet "
                    "multi-fichiers), pas type='document'."
                )
        elif self.type == "code":
            # file_type optionnel (None = single code file, 'zip' = projet).
            # Si défini, doit être 'zip' (les autres file_type pdf/docx/etc.
            # n'ont pas de sens pour du code).
            if self.file_type is not None and self.file_type != "zip":
                raise ValueError(
                    f"Pour type='code', file_type doit être None (single code "
                    f"file) ou 'zip' (projet multi-fichiers). Reçu : "
                    f"'{self.file_type}'."
                )
        elif self.file_type is not None:
            raise ValueError(
                "Le champ file_type n'est autorisé que pour type='document' "
                "ou type='code'."
            )

        # 2. mime_type doit correspondre au type déclaré.
        expected_prefix = _expected_mime_prefix(self.type)
        if expected_prefix is not None and not self.mime_type.startswith(expected_prefix):
            raise ValueError(
                f"Pour type='{self.type}', mime_type doit commencer par "
                f"'{expected_prefix}' (reçu : '{self.mime_type}')."
            )
        if self.type == "gif" and self.mime_type != "image/gif":
            raise ValueError("Pour type='gif', mime_type doit valoir 'image/gif'.")

        # 3. duration_ms pertinent uniquement pour audio/video.
        if self.duration_ms is not None and self.type not in {"audio", "video"}:
            raise ValueError("Le champ duration_ms n'est pertinent que pour audio ou video.")

        # 4. width/height cohérents — si un des deux est posé, l'autre aussi.
        if (self.width_px is None) != (self.height_px is None):
            raise ValueError("width_px et height_px doivent être posés ensemble, ou aucun.")

        return self


# ══════════════════════════════════════════════════════════════
# RESPONSE — avec presigned URL
# ══════════════════════════════════════════════════════════════


class LibraryItemResponse(BaseModel):
    """Item complet renvoyé par les endpoints Library.

    `url` = presigned URL MinIO générée au moment de la réponse (TTL
    `settings.s3_presigned_ttl_seconds`, défaut 1 h). Si le Flutter garde
    le payload en cache plus longtemps, il doit re-GET pour rafraîchir
    l'URL — le client gère l'expiration via un timer ou un retry sur 403.

    `storage_key` et `content_sha256` NE SONT PAS exposés : fuites
    potentielles (énumération d'URLs, corrélation inter-users via hash).
    """

    id: uuid.UUID
    user_id: uuid.UUID
    type: LibraryItemType
    file_type: LibraryFileType | None
    title: str
    description: str | None

    url: str  # presigned URL MinIO

    mime_type: str
    size_bytes: int
    width_px: int | None
    height_px: int | None
    duration_ms: int | None
    aspect_ratio: Decimal | None

    source: LibrarySource
    provider: str | None
    model: str | None
    prompt: str | None

    source_conversation_id: uuid.UUID | None
    source_message_id: uuid.UUID | None

    tags: list[str] | None
    metadata_json: dict[str, Any] | None

    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    model_config = {"from_attributes": True}


class LibraryItemListItem(BaseModel):
    """Item allégé pour `GET /library` (grille Flutter masonry).

    Omet `prompt`, `metadata_json`, `description` (rarement affichés en
    grille). Garde `url` pour la vignette directe.
    """

    id: uuid.UUID
    type: LibraryItemType
    file_type: LibraryFileType | None
    title: str
    url: str

    mime_type: str
    size_bytes: int
    width_px: int | None
    height_px: int | None
    duration_ms: int | None
    aspect_ratio: Decimal | None

    source: LibrarySource
    source_conversation_id: uuid.UUID | None
    tags: list[str] | None

    created_at: datetime

    model_config = {"from_attributes": True}


class LibraryPage(BaseModel):
    """Page paginée cursor-based de la Library."""

    items: list[LibraryItemListItem]
    next_cursor: str | None
