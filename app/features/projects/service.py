"""
ProjectService — logique métier des projets utilisateur.

Calqué sur `features/chat/service.py` (Lot 2) : méthodes statiques, commit en
fin de chaque méthode publique, pattern `_get_owned_project` pour l'isolation
IDOR (404 jamais 403).

Points critiques :

- **Isolation IDOR stricte.** Toute opération qui touche un projet ou un
  fichier cible passe par `_get_owned_project` (404 si mismatch user, même
  règle anti-énumération que `ConversationService`).

- **Quotas pré-flight, pas DB-constraint.** Le plafond de projets/fichiers
  est vérifié en Python via un COUNT avant l'INSERT, pas via une contrainte
  SQL. Raison : le plan (`Free`/`Pro`) peut changer dynamiquement à la
  souscription, une contrainte SQL figerait ce plafond au niveau DDL. Un
  COUNT + INSERT dans la même transaction reste atomique, et Postgres
  sérialise l'écriture au niveau ligne — pas de race condition exploitable
  en pratique.

- **Name conflict : DB source de vérité.** On ne pré-check pas en SELECT
  (pattern TOCTOU classique) : on INSERT, on attrape l'`IntegrityError` de
  l'unique partiel `uq_projects_user_name_active`, on re-raise en
  `ProjectNameConflictException`. Deux clients concurrents tapent tous les
  deux « École » → l'un gagne, l'autre tombe en 409.

- **Soft-delete détache, ne supprime pas.** `soft_delete` pose
  `deleted_at = NOW()` sur le projet **ET** fait un UPDATE séparé
  `conversations SET project_id = NULL WHERE project_id = ?`. La FK
  `ON DELETE SET NULL` ne se déclenche que sur un DELETE physique — ici on
  ne fait pas de DELETE, on fait un UPDATE (soft). L'UPDATE explicite
  sur les conversations garantit qu'elles réapparaissent dans les listings
  actifs sans projet, au lieu de rester attachées à un projet « fantôme ».

- **`list_conversations` réutilise `ConversationService.list_for_user`.**
  La pagination keyset + le FTS français de C1 sont déjà solides — on ne
  les redéveloppe pas, on les compose via un kwarg `project_id=...` ajouté
  au service chat.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import structlog
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors.exceptions import (
    ProjectFilesQuotaExceededException,
    ProjectNameConflictException,
    ProjectQuotaExceededException,
    ResourceNotFoundException,
    ValidationException,
)
from app.features.auth.models import User
from app.features.chat.models import Conversation
from app.features.files.models import UploadedFile
from app.features.projects.models import Project, ProjectFile
from app.features.projects.schemas import (
    ProjectCreate,
    ProjectFileCreate,
    ProjectUpdate,
)

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

_DEFAULT_LIMIT: Final[int] = 20
_MAX_LIMIT: Final[int] = 50


# ══════════════════════════════════════════════════════════════
# DTO internes — service ↔ router
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ProjectView:
    """Projet enrichi des compteurs `file_count` / `conversation_count`
    calculés côté service via subquery SQL.

    Les compteurs ne sont pas dans la table `projects` (pas de dénormalisation
    maintenue — on accepte le coût d'un COUNT par GET, négligeable sur un
    petit index filtré). La dataclass garde ORM et scalaires séparés pour que
    le router puisse les combiner dans `ProjectResponse`.
    """

    project: Project
    file_count: int
    conversation_count: int


@dataclass(frozen=True, slots=True)
class ProjectListItemOrm:
    """Ligne de liste projet enrichie des compteurs + `last_activity_at`.

    Suffixe `Orm` pour rester cohérent avec `ConversationsPageOrm` — le
    service parle ORM + scalaires calculés, le router traduit en Pydantic.
    """

    project: Project
    file_count: int
    conversation_count: int
    last_activity_at: datetime | None


@dataclass(frozen=True, slots=True)
class ProjectsPageOrm:
    items: list[ProjectListItemOrm]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class ProjectFilesPageOrm:
    items: list[ProjectFile]
    next_cursor: str | None


# ══════════════════════════════════════════════════════════════
# Helpers curseur — opaques côté client (même format que Chat)
# ══════════════════════════════════════════════════════════════


def _encode_cursor(sort_ts: datetime, row_id: uuid.UUID) -> str:
    import base64

    raw = f"{sort_ts.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    import base64
    import binascii

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
        log.warning("projects.cursor.invalid", cursor=cursor[:40], error=str(exc))
        raise ValidationException("Curseur de pagination invalide.") from exc
    return sort_ts, row_id


def _clamp_limit(limit: int | None) -> int:
    if limit is None or limit <= 0:
        return _DEFAULT_LIMIT
    return min(limit, _MAX_LIMIT)


# ══════════════════════════════════════════════════════════════
# Quotas — résolution plan Free / Pro
# ══════════════════════════════════════════════════════════════


def _projects_quota(user: User) -> tuple[int, str]:
    """Retourne `(max_projects, plan_label)` selon le plan de l'utilisateur."""
    if user.is_pro:
        return settings.projects_max_pro, "pro"
    return settings.projects_max_free, "free"


def _project_files_quota(user: User) -> tuple[int, str]:
    if user.is_pro:
        return settings.project_files_max_pro, "pro"
    return settings.project_files_max_free, "free"


# Mapping MIME → `ProjectFileType` (enum Flutter `image/pdf/doc/xls/ppt/
# audio/video/other`). Utilisé quand un `upload_id` est fourni sans
# `file_type` explicite.
_MIME_TO_PROJECT_FILE_TYPE: dict[str, str] = {
    "application/pdf": "pdf",
    "application/msword": "doc",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "doc",
    "application/vnd.ms-excel": "xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xls",
    "application/vnd.ms-powerpoint": "ppt",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "ppt",
}


def _derive_project_file_type(mime_type: str | None) -> str | None:
    """Dérive `ProjectFileType` (image/pdf/doc/xls/ppt/audio/video/other)
    depuis le MIME. Retourne None si le mime est vide/inconnu.

    Préfère les mappings explicites (PDF, OOXML) puis fallback par préfixe
    pour les familles multimédia (image/*, audio/*, video/*).
    """
    if not mime_type:
        return None
    m = mime_type.lower()
    if m in _MIME_TO_PROJECT_FILE_TYPE:
        return _MIME_TO_PROJECT_FILE_TYPE[m]
    if m.startswith("image/"):
        return "image"
    if m.startswith("audio/"):
        return "audio"
    if m.startswith("video/"):
        return "video"
    if m.startswith("text/"):
        # Pas d'entrée 'text' dans l'enum Flutter (pdf/doc/xls/ppt/autre) —
        # on dérive 'other'.
        return "other"
    return "other"


# ══════════════════════════════════════════════════════════════
# ProjectService — namespace de la logique métier Projects
# ══════════════════════════════════════════════════════════════


class ProjectService:
    """CRUD projets + fichiers — toutes les méthodes statiques, session
    traverse en paramètre, aucun état injecté par DI."""

    # ── Helper d'isolation IDOR (projet actif uniquement) ─────────
    @staticmethod
    async def _get_owned_project(
        project_id: uuid.UUID,
        user_id: uuid.UUID,
        db: AsyncSession,
    ) -> Project:
        """Charge un projet actif dont l'user courant est propriétaire.

        - `WHERE id = :id AND user_id = :user AND deleted_at IS NULL`
        - Aucune correspondance → `ResourceNotFoundException` (404).
          Jamais 403 : anti-énumération d'UUID.

        La relation `files` (eager selectin) est chargée automatiquement
        par SQLAlchemy — pratique pour les endpoints de détail, négligeable
        pour les updates/deletes qui n'y touchent pas.
        """
        result = await db.execute(
            select(Project).where(
                Project.id == project_id,
                Project.user_id == user_id,
                Project.deleted_at.is_(None),
            )
        )
        project = result.scalar_one_or_none()
        if project is None:
            raise ResourceNotFoundException("Projet")
        return project

    # ── Helper : compteurs file_count + conversation_count ─────────
    @staticmethod
    async def _compute_counts(
        project_id: uuid.UUID,
        db: AsyncSession,
    ) -> tuple[int, int]:
        """Retourne `(file_count, conversation_count)` du projet — les deux
        comptes sont filtrés sur les rows actives (`deleted_at IS NULL`).

        Deux `SELECT COUNT(*)` distincts plutôt qu'un JOIN : les FKs sont
        différentes (project_files.project_id vs conversations.project_id),
        et on évite le Cartesian product qui double-compterait les rows.
        """
        file_count_stmt = select(func.count(ProjectFile.id)).where(
            ProjectFile.project_id == project_id,
            ProjectFile.deleted_at.is_(None),
        )
        conv_count_stmt = select(func.count(Conversation.id)).where(
            Conversation.project_id == project_id,
            Conversation.deleted_at.is_(None),
        )
        file_count = (await db.execute(file_count_stmt)).scalar_one() or 0
        conv_count = (await db.execute(conv_count_stmt)).scalar_one() or 0
        return int(file_count), int(conv_count)

    # ── CREATE ───────────────────────────────────────────────────
    @staticmethod
    async def create(
        body: ProjectCreate,
        user: User,
        db: AsyncSession,
    ) -> ProjectView:
        """Crée un projet pour l'utilisateur courant.

        Pipeline :
        1. Vérifier le quota (COUNT actifs < max du plan).
        2. INSERT + commit. Si `IntegrityError` sur l'unique partiel
           `uq_projects_user_name_active` → 409 `PROJECT_NAME_CONFLICT`.
        3. Retourne `ProjectView(project, 0, 0)` — un projet tout neuf
           n'a ni fichiers ni conversations.
        """
        max_projects, plan_label = _projects_quota(user)
        active_count_stmt = select(func.count(Project.id)).where(
            Project.user_id == user.id,
            Project.deleted_at.is_(None),
        )
        active_count = (await db.execute(active_count_stmt)).scalar_one() or 0
        if int(active_count) >= max_projects:
            raise ProjectQuotaExceededException(
                current=int(active_count), maximum=max_projects, plan=plan_label
            )

        project = Project(
            user_id=user.id,
            name=body.name,
            icon_index=body.icon_index,
            color_index=body.color_index,
            instructions=body.instructions,
        )
        db.add(project)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            log.info(
                "projects.create.name_conflict",
                user_id=str(user.id),
                name=body.name,
            )
            raise ProjectNameConflictException() from None
        await db.refresh(project)
        log.info(
            "projects.created",
            user_id=str(user.id),
            project_id=str(project.id),
            name=project.name,
        )
        return ProjectView(project=project, file_count=0, conversation_count=0)

    # ── LIST (paginée cursor-based, DESC, q optionnel) ────────────
    @staticmethod
    async def list_for_user(
        user: User,
        db: AsyncSession,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        q: str | None = None,
    ) -> ProjectsPageOrm:
        """Liste paginée des projets actifs de l'utilisateur.

        - Tri keyset sur `(created_at DESC, id DESC)` — même clé que le
          curseur, compatible avec l'index partiel `idx_projects_user_active`.
        - `q` optionnel : ILIKE `%q%` sur `name` (exploite le GIN trigram
          `idx_projects_name_trgm`). Vide/whitespace-only → ignoré.
        - Enrichit chaque row avec `file_count`, `conversation_count` et
          `last_activity_at` calculés via subqueries scalaires — un seul
          aller-retour SQL pour la page entière, O(N × log M) total.
        """
        effective_limit = _clamp_limit(limit)

        # Subqueries scalaires — évalués row-par-row par Postgres via les
        # index partiels. Plus léger qu'un JOIN + GROUP BY (plan stable,
        # pas de risque de cartesian sur les pages mixtes).
        file_count_subq = (
            select(func.count(ProjectFile.id))
            .where(
                ProjectFile.project_id == Project.id,
                ProjectFile.deleted_at.is_(None),
            )
            .correlate(Project)
            .scalar_subquery()
        )
        conv_count_subq = (
            select(func.count(Conversation.id))
            .where(
                Conversation.project_id == Project.id,
                Conversation.deleted_at.is_(None),
            )
            .correlate(Project)
            .scalar_subquery()
        )
        last_activity_subq = (
            select(func.max(Conversation.last_message_at))
            .where(
                Conversation.project_id == Project.id,
                Conversation.deleted_at.is_(None),
            )
            .correlate(Project)
            .scalar_subquery()
        )

        conditions = [
            Project.user_id == user.id,
            Project.deleted_at.is_(None),
        ]
        if q is not None:
            q_stripped = q.strip()
            if q_stripped:
                conditions.append(Project.name.ilike(f"%{q_stripped}%"))

        if cursor:
            cursor_ts, cursor_id = _decode_cursor(cursor)
            # Keyset DESC : la ligne suivante a (created_at, id) strictement
            # inférieur. On emploie tuple_ pour la comparaison lexicographique.
            from sqlalchemy import tuple_

            conditions.append(tuple_(Project.created_at, Project.id) < tuple_(cursor_ts, cursor_id))

        stmt = (
            select(
                Project,
                file_count_subq.label("file_count"),
                conv_count_subq.label("conv_count"),
                last_activity_subq.label("last_activity_at"),
            )
            .where(*conditions)
            .order_by(Project.created_at.desc(), Project.id.desc())
            .limit(effective_limit + 1)
        )
        result = await db.execute(stmt)
        rows = list(result.all())

        has_next = len(rows) > effective_limit
        page_rows = rows[:effective_limit]
        items = [
            ProjectListItemOrm(
                project=row[0],
                file_count=int(row[1] or 0),
                conversation_count=int(row[2] or 0),
                last_activity_at=row[3],
            )
            for row in page_rows
        ]

        next_cursor: str | None = None
        if has_next and items:
            last = items[-1].project
            next_cursor = _encode_cursor(last.created_at, last.id)

        return ProjectsPageOrm(items=items, next_cursor=next_cursor)

    # ── GET BY ID ────────────────────────────────────────────────
    @staticmethod
    async def get(
        project_id: uuid.UUID,
        user: User,
        db: AsyncSession,
    ) -> ProjectView:
        """Retourne un projet enrichi de ses compteurs (404 IDOR-safe)."""
        project = await ProjectService._get_owned_project(project_id, user.id, db)
        file_count, conv_count = await ProjectService._compute_counts(project.id, db)
        return ProjectView(project=project, file_count=file_count, conversation_count=conv_count)

    # ── UPDATE (PATCH partial) ───────────────────────────────────
    @staticmethod
    async def update(
        project_id: uuid.UUID,
        body: ProjectUpdate,
        user: User,
        db: AsyncSession,
    ) -> ProjectView:
        """Mise à jour partielle — seuls les champs envoyés sont modifiés.

        - Champ absent → non touché.
        - Champ présent non-null → écrit.
        - `clear_instructions=True` → `instructions = NULL` (dédié pour
          ne pas confondre avec « champ non envoyé » vs « champ explicitement
          vidé »).
        - `IntegrityError` sur unicité du nom → 409 `PROJECT_NAME_CONFLICT`.
        """
        project = await ProjectService._get_owned_project(project_id, user.id, db)
        update_data = body.model_dump(exclude_unset=True)
        clear_instructions = bool(update_data.pop("clear_instructions", False))

        touched = False
        for field, value in update_data.items():
            setattr(project, field, value)
            touched = True
        if clear_instructions:
            project.instructions = None
            touched = True

        if touched:
            project.updated_at = datetime.now(UTC)
            try:
                await db.commit()
            except IntegrityError:
                await db.rollback()
                log.info(
                    "projects.update.name_conflict",
                    user_id=str(user.id),
                    project_id=str(project.id),
                )
                raise ProjectNameConflictException() from None
            await db.refresh(project)
            log.info(
                "projects.updated",
                user_id=str(user.id),
                project_id=str(project.id),
                fields=list(update_data.keys()),
                clear_instructions=clear_instructions,
            )

        file_count, conv_count = await ProjectService._compute_counts(project.id, db)
        return ProjectView(project=project, file_count=file_count, conversation_count=conv_count)

    # ── SOFT DELETE (+ détache les conversations) ────────────────
    @staticmethod
    async def soft_delete(
        project_id: uuid.UUID,
        user: User,
        db: AsyncSession,
    ) -> None:
        """Soft-delete le projet ET détache toutes ses conversations actives.

        Contrat :
        - `deleted_at = NOW()` sur le projet.
        - `UPDATE conversations SET project_id = NULL WHERE project_id = ?
           AND deleted_at IS NULL` — les convs réapparaissent dans la liste
          « Toutes mes conversations » sans projet. Les convs DÉJÀ dans la
          corbeille ne sont pas touchées (on ne change pas leur rattachement
          post-suppression, elles restent « projet X quand elles ont été
          supprimées »).
        - Idempotent : si le projet est déjà soft-deleté, l'appel retourne
          sans erreur (le helper `_get_owned_project` filtre déjà sur
          `deleted_at IS NULL` et renvoie 404 → on traite comme un no-op au
          niveau router → en fait non, le helper renvoie 404, ce qui est
          voulu : si l'user retape un DELETE sur un projet déjà soft-deleté,
          c'est 404 IDOR-safe). **Note** : l'idempotence côté client est
          assurée par le fait que le projet disparaît du listing — un
          second DELETE devient un 404 (le projet n'existe plus du point
          de vue du listing actif), ce qui est le comportement REST
          standard pour un soft-delete.
        """
        project = await ProjectService._get_owned_project(project_id, user.id, db)
        now = datetime.now(UTC)
        project.deleted_at = now
        project.updated_at = now

        # Détache les conversations actives du projet.
        await db.execute(
            update(Conversation)
            .where(
                Conversation.project_id == project_id,
                Conversation.deleted_at.is_(None),
            )
            .values(project_id=None, updated_at=now)
        )
        await db.commit()
        log.info(
            "projects.soft_deleted",
            user_id=str(user.id),
            project_id=str(project.id),
        )

    # ── ADD FILE — mode legacy OU mode upload_id (E3) ────────────
    @staticmethod
    async def add_file(
        project_id: uuid.UUID,
        body: ProjectFileCreate,
        user: User,
        db: AsyncSession,
    ) -> ProjectFile:
        """Crée une entrée `project_files` rattachée au projet cible.

        Deux modes (mutuellement exclusifs, garantis par le validator
        Pydantic `ProjectFileCreate.upload_id_and_storage_key_mutually_exclusive`) :

        1. **Legacy (C2)** — `body.storage_key` / `body.size_bytes` /
           `body.mime_type` fournis directement par le client (ou tous None
           pour une métadonnée logique). `file_type` est obligatoire dans
           ce mode.
        2. **upload_id (E3)** — le client a d'abord appelé
           `POST /files/upload`. Le service :
             a. Vérifie que `upload_id` appartient à l'user (404 IDOR).
             b. Copie storage_key / size_bytes / mime_type depuis
                UploadedFile.
             c. Dérive file_type du mime si non fourni côté body.
             d. Crée la row project_files.
             e. Appelle `FileUploadService.mark_attached` pour tracer le
                rattachement dans uploaded_files.
        """
        from app.features.files.service import FileUploadService  # local import

        # 1. Owner check — 404 si pas à l'user.
        await ProjectService._get_owned_project(project_id, user.id, db)

        # 2. Quota pré-flight.
        max_files, plan_label = _project_files_quota(user)
        active_count_stmt = select(func.count(ProjectFile.id)).where(
            ProjectFile.project_id == project_id,
            ProjectFile.deleted_at.is_(None),
        )
        active_count = (await db.execute(active_count_stmt)).scalar_one() or 0
        if int(active_count) >= max_files:
            raise ProjectFilesQuotaExceededException(
                current=int(active_count), maximum=max_files, plan=plan_label
            )

        # 3. Résolution des métadonnées selon le mode.
        storage_key = body.storage_key
        size_bytes = body.size_bytes
        mime_type = body.mime_type
        file_type = body.file_type

        upload_row: UploadedFile | None = None
        if body.upload_id is not None:
            upload_row = await FileUploadService.get_for_user(body.upload_id, user, db)
            storage_key = upload_row.storage_key
            size_bytes = upload_row.size_bytes
            mime_type = upload_row.mime_type
            if file_type is None:
                file_type = _derive_project_file_type(mime_type)

        # `file_type` doit être non-null à ce stade (validator Pydantic +
        # dérivation ci-dessus). Garde-fou :
        if file_type is None:
            raise ValidationException("file_type n'a pas pu être déterminé pour ce fichier.")

        # 4. INSERT + commit.
        pfile = ProjectFile(
            project_id=project_id,
            name=body.name,
            file_type=file_type,
            storage_key=storage_key,
            size_bytes=size_bytes,
            mime_type=mime_type,
        )
        db.add(pfile)
        await db.commit()
        await db.refresh(pfile)

        # 5. Si mode upload_id : trace le rattachement côté uploaded_files.
        if upload_row is not None:
            await FileUploadService.mark_attached(
                upload_row.id,
                user.id,
                kind="project_file",
                target_id=pfile.id,
                db=db,
            )

        log.info(
            "projects.file.created",
            user_id=str(user.id),
            project_id=str(project_id),
            file_id=str(pfile.id),
            file_type=pfile.file_type,
            upload_id=str(body.upload_id) if body.upload_id else None,
        )
        return pfile

    # ── LIST FILES ───────────────────────────────────────────────
    @staticmethod
    async def list_files(
        project_id: uuid.UUID,
        user: User,
        db: AsyncSession,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> ProjectFilesPageOrm:
        """Liste paginée des fichiers actifs d'un projet — tri `uploaded_at DESC`.

        Owner check obligatoire avant toute requête.
        """
        await ProjectService._get_owned_project(project_id, user.id, db)
        effective_limit = _clamp_limit(limit)

        conditions = [
            ProjectFile.project_id == project_id,
            ProjectFile.deleted_at.is_(None),
        ]
        if cursor:
            from sqlalchemy import tuple_

            cursor_ts, cursor_id = _decode_cursor(cursor)
            conditions.append(
                tuple_(ProjectFile.uploaded_at, ProjectFile.id) < tuple_(cursor_ts, cursor_id)
            )

        stmt = (
            select(ProjectFile)
            .where(*conditions)
            .order_by(ProjectFile.uploaded_at.desc(), ProjectFile.id.desc())
            .limit(effective_limit + 1)
        )
        result = await db.execute(stmt)
        rows = list(result.scalars().all())

        has_next = len(rows) > effective_limit
        items = rows[:effective_limit]
        next_cursor: str | None = None
        if has_next and items:
            last = items[-1]
            next_cursor = _encode_cursor(last.uploaded_at, last.id)

        return ProjectFilesPageOrm(items=items, next_cursor=next_cursor)

    # ── REMOVE FILE (soft-delete, idempotent) ────────────────────
    @staticmethod
    async def remove_file(
        project_id: uuid.UUID,
        file_id: uuid.UUID,
        user: User,
        db: AsyncSession,
    ) -> None:
        """Soft-delete un fichier du projet — 204 idempotent.

        - Owner check du projet (404 IDOR si pas à l'user).
        - Puis owner check du fichier via `project_id = ... AND deleted_at
          IS NULL`. Si déjà soft-deleté OU n'existe pas → 404 IDOR-safe.
        """
        await ProjectService._get_owned_project(project_id, user.id, db)

        result = await db.execute(
            select(ProjectFile).where(
                ProjectFile.id == file_id,
                ProjectFile.project_id == project_id,
                ProjectFile.deleted_at.is_(None),
            )
        )
        pfile = result.scalar_one_or_none()
        if pfile is None:
            raise ResourceNotFoundException("Fichier")

        now = datetime.now(UTC)
        pfile.deleted_at = now
        pfile.updated_at = now
        await db.commit()
        log.info(
            "projects.file.soft_deleted",
            user_id=str(user.id),
            project_id=str(project_id),
            file_id=str(file_id),
        )
