"""
Modèles ORM Projects — Project, ProjectFile.

Schéma SQL aligné sur la migration `006_projects.py` et sur le modèle Flutter
`ProjectModel` pour garantir un contrat byte-à-byte identique avec le front.

Choix de design clés :

- **`icon_index` / `color_index` en SmallInteger** : miroir des indices de
  `ProjectIconCatalog.defaultGrid` (0..24) et `ProjectColors.all` (0..17)
  côté Flutter. Les CHECK SQL sont posés en migration 006 (défense en
  profondeur : Pydantic rejette déjà à 422 côté API).

- **`instructions` TEXT nullable avec CHECK ≤ 4000 chars** : c'est un system
  prompt par projet — pas un paragraphe libre. Le cap évite de charger un
  roman dans le contexte IA à chaque message de la conversation liée. La
  borne vit côté DB ET côté schéma Pydantic (les deux sont utiles : l'un
  protège de l'écriture, l'autre rend le message d'erreur sympathique).

- **`deleted_at` partout** : soft-delete systématique, cohérent avec le Lot 2
  chat. `DELETE /projects/{id}` pose `deleted_at = NOW()` + détache les
  conversations en `UPDATE conversations SET project_id = NULL` (géré côté
  service — la FK `ON DELETE SET NULL` ne se déclenche que sur un DELETE
  physique). Un futur lot corbeille projets symétrisera ce pattern.

- **Relation `Project.files` en `lazy='selectin'`** : un projet en détail est
  quasi toujours consulté avec sa liste de fichiers. `selectin` émet une
  deuxième requête `IN (...)` qui bat `joinedload` sur les lots (cardinalité
  ≤ 100 fichiers/projet), et évite le Cartesian explosion du `joinedload`
  sur plusieurs relations. Les listings (`list_for_user`) passent par des
  projections explicites et ne chargent pas cette relation.

- **Pas de relation `User.projects`** : même principe que pour les
  conversations — on ne veut jamais qu'un `selectin` implicite charge tous
  les projets d'un user quand le service manipule un `User` pour autre chose
  (auth, settings). Les listings passent par `ProjectService.list_for_user`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    SmallInteger,
    String,
    Text,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database.base import Base, UUIDMixin

# ══════════════════════════════════════════════════════════════
# Project — conteneur logique (nom + icône + couleur + instructions)
# ══════════════════════════════════════════════════════════════


class Project(Base, UUIDMixin):
    """Projet utilisateur — regroupe des conversations, des fichiers et des
    instructions système dédiées.

    - `icon_index` / `color_index` sont des entiers qui indexent les grilles
      Flutter (25 icônes / 18 couleurs au 2026-04-24). Les deux côtés se
      synchronisent par convention : un changement de taille d'une grille
      Flutter exigera une migration de CHECK pour élargir la borne SQL.
    - `instructions` est injecté comme system prompt quand une conversation
      du projet lance un `/chat/stream` (câblage futur — aujourd'hui stocké
      seulement, l'injection viendra avec les Tools LLM F1).
    - `deleted_at` : soft-delete. Le listing actif filtre `IS NULL`.
    """

    __tablename__ = "projects"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    icon_index: Mapped[int] = mapped_column(
        SmallInteger, server_default="0", default=0, nullable=False
    )
    color_index: Mapped[int] = mapped_column(
        SmallInteger, server_default="3", default=3, nullable=False
    )
    instructions: Mapped[str | None] = mapped_column(Text)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Relations ──────────────────────────────────────────────
    # `files` est chargé par `selectin` uniquement sur un GET détail — les
    # listes de projets n'accèdent pas à la relation (elles comptent via
    # subquery SQL). Cascade + passive_deletes pour que le DELETE physique
    # (RGPD futur) nettoie les fichiers orphelins sans roundtrip applicatif.
    files: Mapped[list[ProjectFile]] = relationship(
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="selectin",
        passive_deletes=True,
        order_by="ProjectFile.uploaded_at.desc()",
    )

    __table_args__ = (
        CheckConstraint(
            "char_length(trim(name)) BETWEEN 1 AND 100",
            name="ck_projects_name_length",
        ),
        CheckConstraint(
            "icon_index BETWEEN 0 AND 24",
            name="ck_projects_icon_index_range",
        ),
        CheckConstraint(
            "color_index BETWEEN 0 AND 17",
            name="ck_projects_color_index_range",
        ),
        CheckConstraint(
            "instructions IS NULL OR char_length(instructions) <= 4000",
            name="ck_projects_instructions_length",
        ),
        # Listing actif trié par récence (keyset created_at DESC).
        Index(
            "idx_projects_user_active",
            "user_id",
            "created_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )


# ══════════════════════════════════════════════════════════════
# ProjectFile — métadonnée d'un fichier attaché à un projet
# ══════════════════════════════════════════════════════════════


class ProjectFile(Base, UUIDMixin):
    """Fichier attaché à un projet — métadonnée seule.

    Session C2 livre le CRUD métadonnées uniquement. L'upload physique vers
    MinIO/S3 et la génération des URLs signées arrivent en session E3
    (`core/storage/s3.py` + `POST /files/upload`).

    - `storage_key` est la clé opaque MinIO/S3 (nullable tant que E3 n'est
      pas câblée — le Flutter peut créer des entrées avec `null` et y
      associer une clé plus tard via un update futur, ou E3 pourra le
      renseigner dès l'upload physique).
    - `file_type` en VARCHAR + CHECK permet d'ajouter un type sans migration.
    - `size_bytes` et `mime_type` sont renseignés quand E3 les extrait de
      l'upload réel (aujourd'hui laissés à None par défaut).
    """

    __tablename__ = "project_files"

    project_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(16), nullable=False)
    storage_key: Mapped[str | None] = mapped_column(String(512))
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    mime_type: Mapped[str | None] = mapped_column(String(128))
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=text("now()")
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped[Project] = relationship(back_populates="files")

    __table_args__ = (
        CheckConstraint(
            "char_length(trim(name)) BETWEEN 1 AND 255",
            name="ck_project_files_name_length",
        ),
        CheckConstraint(
            "file_type IN ('image', 'pdf', 'doc', 'xls', 'ppt', 'audio', 'video', 'other')",
            name="ck_project_files_type",
        ),
        CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_project_files_size_non_negative",
        ),
        Index(
            "idx_project_files_project_active",
            "project_id",
            "uploaded_at",
            postgresql_where=text("deleted_at IS NULL"),
        ),
    )
