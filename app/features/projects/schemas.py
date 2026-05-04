"""
Schémas Pydantic Projects — Request/Response pour les endpoints /projects.

Conventions NEXYA :
- Suffixe Create / Update / Response / ListItem selon l'usage.
- `model_config = {"from_attributes": True}` pour les DTOs nourris d'ORM.
- `Literal[...]` pour les enums (pas d'ENUM Python) — miroir des CHECK SQL.

Design clés :

- **`icon_index` / `color_index` bornés via `Field(ge=..., le=...)`** : aligné
  sur la grille Flutter (25 icônes, 18 couleurs). Pydantic rejette à 422 avant
  même d'atteindre le service ; la DB fait la même chose en défense en
  profondeur via CHECK constraint.

- **`instructions` max 4000 chars** : cohérent avec le CHECK SQL. Un user Pro
  hit le plafond DB ; un user Free se verra appliquer un plafond applicatif
  plus strict côté service (`quotas.instructions_max_chars_free`) avant
  même d'atteindre la DB — deux barrières concentriques.

- **`ProjectUpdate` avec `clear_instructions`** : sémantique PATCH stricte.
  Un champ absent n'est pas touché. Un champ présent est écrit. Un `null`
  sur `instructions` NE vide PAS le champ (ambiguïté). Pour explicitement
  effacer les instructions : passer `clear_instructions=true`.

- **`ProjectResponse.file_count` / `conversation_count`** : calculés SQL via
  subquery côté service, pas chargés en relation (évite le N+1 + paie le
  coût du count une seule fois, même quand on affiche 20 projets en liste).

- **`ProjectListItem.last_activity_at`** : `MAX(conversations.last_message_at)`
  sur les convs actives du projet — sinon `project.updated_at`. Permet au
  Flutter de trier les projets par récence d'activité dans l'UX « Projets »
  sans requête supplémentaire côté client.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ══════════════════════════════════════════════════════════════
# Types communs
# ══════════════════════════════════════════════════════════════

# Miroir exact du CHECK SQL `file_type IN (...)`.
ProjectFileType = Literal[
    "image",
    "pdf",
    "doc",
    "xls",
    "ppt",
    "audio",
    "video",
    "other",
]

# Bornes miroirs de la migration 006 et des grilles Flutter.
_ICON_INDEX_MIN = 0
_ICON_INDEX_MAX = 24
_COLOR_INDEX_MIN = 0
_COLOR_INDEX_MAX = 17
_INSTRUCTIONS_MAX_CHARS = 4000
_NAME_MAX = 100
_FILE_NAME_MAX = 255


# ══════════════════════════════════════════════════════════════
# PROJECT — CRUD
# ══════════════════════════════════════════════════════════════


class ProjectCreate(BaseModel):
    """Création d'un projet pour l'utilisateur courant.

    Seul `name` est obligatoire — les autres champs ont des défauts aligné
    sur le comportement Flutter (icône fusée rouge, couleur verte par
    défaut, pas d'instructions).
    """

    name: str = Field(min_length=1, max_length=_NAME_MAX)
    icon_index: int = Field(default=0, ge=_ICON_INDEX_MIN, le=_ICON_INDEX_MAX)
    color_index: int = Field(default=3, ge=_COLOR_INDEX_MIN, le=_COLOR_INDEX_MAX)
    instructions: str | None = Field(default=None, max_length=_INSTRUCTIONS_MAX_CHARS)

    @field_validator("name")
    @classmethod
    def name_not_only_whitespace(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Le nom du projet ne peut pas être vide.")
        return stripped

    @field_validator("instructions")
    @classmethod
    def instructions_strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class ProjectUpdate(BaseModel):
    """Mise à jour partielle d'un projet — sémantique PATCH stricte.

    - Champ absent du body → non touché.
    - Champ présent (non-null) → écrit.
    - `instructions` ne peut PAS être vidé en passant `null` (ambigu : est-ce
      un oubli ou un effacement ?). Pour effacer, passer
      `clear_instructions=true` (champ dédié, sans ambiguïté).
    """

    name: str | None = Field(default=None, min_length=1, max_length=_NAME_MAX)
    icon_index: int | None = Field(default=None, ge=_ICON_INDEX_MIN, le=_ICON_INDEX_MAX)
    color_index: int | None = Field(default=None, ge=_COLOR_INDEX_MIN, le=_COLOR_INDEX_MAX)
    instructions: str | None = Field(default=None, max_length=_INSTRUCTIONS_MAX_CHARS)
    clear_instructions: bool = False

    @field_validator("name")
    @classmethod
    def name_not_only_whitespace(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("Le nom du projet ne peut pas être vide.")
        return stripped

    @field_validator("instructions")
    @classmethod
    def instructions_strip(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class ProjectResponse(BaseModel):
    """Projet complet renvoyé par GET /projects/{id} et POST /projects.

    `file_count` et `conversation_count` sont peuplés par le service via
    subqueries SQL (voir `ProjectService.get` / `.create`) — absents de la
    table `projects` pour rester atomiques et éviter un trigger de
    dénormalisation (pattern retenu pour `conversations.message_count`,
    à répliquer si le besoin d'O(1) se fait sentir sur les projets).
    """

    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    icon_index: int
    color_index: int
    instructions: str | None
    file_count: int = 0
    conversation_count: int = 0
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    model_config = {"from_attributes": True}


class ProjectListItem(BaseModel):
    """Item allégé pour GET /projects (liste paginée).

    `last_activity_at` = `MAX(conversations.last_message_at)` sur les convs
    actives du projet — ou `project.updated_at` si aucune conv ou aucune
    conv n'a jamais eu de message. Sert au tri secondaire côté Flutter
    « Projets récemment actifs ».
    """

    id: uuid.UUID
    name: str
    icon_index: int
    color_index: int
    file_count: int = 0
    conversation_count: int = 0
    last_activity_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class ProjectsPage(BaseModel):
    """Page paginée cursor-based de projets."""

    items: list[ProjectListItem]
    next_cursor: str | None


# ══════════════════════════════════════════════════════════════
# PROJECT FILE — métadonnée seulement (upload physique = E3)
# ══════════════════════════════════════════════════════════════


class ProjectFileCreate(BaseModel):
    """Création d'une métadonnée de fichier rattachée à un projet.

    Deux modes mutuellement exclusifs :

    1. **Mode legacy (C2)** — le client passe `storage_key` + `size_bytes`
       + `mime_type` à la main (ou les laisse tous à null pour stocker une
       métadonnée purement « logique » sans binaire attaché encore).
    2. **Mode upload_id (E3)** — le client a d'abord appelé
       `POST /files/upload`, il passe juste `upload_id: <UUID>` et le
       service copie automatiquement storage_key / size_bytes / mime_type
       depuis la row `uploaded_files`, et marque l'upload comme attaché.

    `file_type` est dérivé du `mime_type` si non-fourni (dispatch
    image/pdf/etc. côté service), donc **optionnel en mode upload_id**.
    """

    name: str = Field(min_length=1, max_length=_FILE_NAME_MAX)
    file_type: ProjectFileType | None = None
    storage_key: str | None = Field(default=None, max_length=512)
    size_bytes: int | None = Field(default=None, ge=0)
    mime_type: str | None = Field(default=None, max_length=128)
    upload_id: uuid.UUID | None = None

    @field_validator("name")
    @classmethod
    def name_not_only_whitespace(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("Le nom du fichier ne peut pas être vide.")
        return stripped

    @model_validator(mode="after")
    def upload_id_and_storage_key_mutually_exclusive(self) -> ProjectFileCreate:
        """Si `upload_id` est fourni, les champs legacy ne doivent pas l'être.

        Protège contre un client qui passerait les deux — on ne sait pas
        lequel fait foi. 422 force le client à choisir un mode cohérent.
        """
        if self.upload_id is not None:
            if (
                self.storage_key is not None
                or self.size_bytes is not None
                or self.mime_type is not None
            ):
                raise ValueError(
                    "upload_id est incompatible avec storage_key / size_bytes / "
                    "mime_type — choisissez un seul mode."
                )
        else:
            # Mode legacy : file_type est obligatoire (dérivé du mime côté
            # service seulement si on a un mime ou un upload).
            if self.file_type is None and self.mime_type is None:
                raise ValueError("file_type est obligatoire en mode legacy (sans upload_id).")
        return self


class ProjectFileResponse(BaseModel):
    """Fichier d'un projet retourné par les endpoints `/projects/{id}/files`.

    Champs enrichis D2.5 (2026-05-04) — peuplés par le helper
    `_project_file_to_response` côté router (pas de `model_validate` direct
    depuis l'ORM car ces champs viennent de jointures + signature à la volée) :

    - `presigned_url` : URL MinIO signée TTL 30 min, **régénérée à chaque
      lecture** (createList). Permet au client Flutter d'afficher la preview
      d'une image / de télécharger un PDF directement, même après refresh
      ou cold start. `None` si `storage_key` est null (mode legacy C2 pur
      sans binaire attaché).
    - `upload_id` : référence vers `uploaded_files.id` quand le fichier a
      été créé via le mode upload_id (E3). `None` en mode legacy. Le client
      l'utilise pour appeler `GET /files/{id}` lors du polling RAG.
    - `chunks_indexed_at` : sentinelle one-shot du worker RAG D4, recopiée
      depuis la row `uploaded_files` rattachée. `None` si pas encore indexé
      OU si MIME non-éligible OU si mode legacy. Le client n'a besoin de
      poller qu'une fois — au prochain refresh la valeur est persistante.
    """

    id: uuid.UUID
    project_id: uuid.UUID
    name: str
    file_type: ProjectFileType
    storage_key: str | None
    size_bytes: int | None
    mime_type: str | None
    uploaded_at: datetime
    created_at: datetime
    updated_at: datetime

    # D2.5 — enrichissements pour Flutter (preview + polling RAG).
    presigned_url: str | None = None
    upload_id: uuid.UUID | None = None
    chunks_indexed_at: datetime | None = None

    model_config = {"from_attributes": True}


class ProjectFilesPage(BaseModel):
    """Page paginée cursor-based de fichiers d'un projet."""

    items: list[ProjectFileResponse]
    next_cursor: str | None
