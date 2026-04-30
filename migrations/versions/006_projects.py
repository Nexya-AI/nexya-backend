"""Create project tables (`projects`, `project_files`) + FK `conversations.project_id`.

Revision ID: 006_projects
Revises: 005_fts_search
Create Date: 2026-04-24

Session C2 — « Projects CRUD complet ».

Principes posés par cette migration :

- **Soft-delete partout** : `deleted_at` nullable sur `projects` et `project_files`
  (cohérent avec `conversations` / `messages` du Lot 2). Rien n'est jamais
  supprimé physiquement par l'utilisateur ; `DELETE /projects/{id}` retourne 204
  et pose `deleted_at = NOW()`. Une purge définitive viendra plus tard avec la
  corbeille projets (hors scope C2).

- **FK `conversations.project_id ON DELETE SET NULL`** : si on en vient un jour
  à un vrai `DELETE` physique (RGPD, purge compte), les conversations du projet
  sont **détachées**, pas supprimées. Une conversation vaut plus qu'un projet
  (elle contient l'historique de messages, l'IA, des coûts déjà facturés). Le
  soft-delete utilisateur ne déclenche pas la FK : on fait l'UPDATE côté
  service (``SET project_id = NULL WHERE project_id = ?``) en parallèle du
  `deleted_at` pour que les conversations réapparaissent sans projet dans les
  listings actifs.

- **Unicité `(user_id, LOWER(name))` côté actifs uniquement** via index partiel
  `WHERE deleted_at IS NULL`. Un projet supprimé libère son nom : l'utilisateur
  peut recréer un projet « École » après avoir soft-deleté l'ancien. Case-
  insensitive pour coller à l'intuition UX (« École » et « école » sont le
  même projet du point de vue de l'user).

- **`idx_projects_name_trgm`** (GIN pg_trgm) : le C1 a activé `pg_trgm` au
  niveau DB ; on réutilise l'extension pour accélérer un ILIKE `%q%` sur les
  noms de projets (tolérance fautes de frappe gratuite sur un champ court,
  FTS `tsvector` serait overkill pour 3-4 mots).

- **Index partiels `WHERE deleted_at IS NULL`** sur tous les index de lecture :
  Postgres n'y écrit que les rows actives → index plus petit, scans plus
  rapides. La corbeille, moins lue, accepte d'être un peu plus lente.

- **CHECK constraints au niveau SQL** sur les bornes `icon_index` [0..24] et
  `color_index` [0..17] (miroirs des tailles des grilles Flutter `defaultGrid`
  et `ProjectColors.all`). L'API rejette déjà via Pydantic ; la DB ferme la
  porte en défense en profondeur contre une écriture directe ou un bug de
  service futur.

- **`file_type` en VARCHAR(16) + CHECK** plutôt qu'en ENUM Postgres : ajouter
  un type (`zip`, `csv`, ...) se fait sans migration DDL destructrice, comme
  pour `expert_id` et `role` côté chat.

Rollback propre : DROP INDEX (×5) + DROP FK + DROP COLUMN + DROP TABLE.
L'extension `pg_trgm` reste en place (partagée avec C1).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "006_projects"
down_revision = "005_fts_search"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Table projects ────────────────────────────────────────────────
    op.create_table(
        "projects",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("icon_index", sa.SmallInteger(), server_default="0", nullable=False),
        sa.Column("color_index", sa.SmallInteger(), server_default="3", nullable=False),
        sa.Column("instructions", sa.Text(), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "char_length(trim(name)) BETWEEN 1 AND 100",
            name="ck_projects_name_length",
        ),
        sa.CheckConstraint(
            "icon_index BETWEEN 0 AND 24",
            name="ck_projects_icon_index_range",
        ),
        sa.CheckConstraint(
            "color_index BETWEEN 0 AND 17",
            name="ck_projects_color_index_range",
        ),
        sa.CheckConstraint(
            "instructions IS NULL OR char_length(instructions) <= 4000",
            name="ck_projects_instructions_length",
        ),
    )
    op.create_index(
        "idx_projects_user_active",
        "projects",
        ["user_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # Unicité case-insensitive scope user, actifs seulement.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_projects_user_name_active
            ON projects (user_id, LOWER(name))
            WHERE deleted_at IS NULL
        """
    )
    # Fuzzy match trigram sur le nom de projet (réutilise pg_trgm de C1).
    op.execute(
        """
        CREATE INDEX idx_projects_name_trgm
            ON projects USING GIN (name gin_trgm_ops)
            WHERE deleted_at IS NULL
        """
    )

    # ── Table project_files ───────────────────────────────────────────
    op.create_table(
        "project_files",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("project_id", UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("file_type", sa.String(16), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column("mime_type", sa.String(128), nullable=True),
        sa.Column(
            "uploaded_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "char_length(trim(name)) BETWEEN 1 AND 255",
            name="ck_project_files_name_length",
        ),
        sa.CheckConstraint(
            "file_type IN ('image', 'pdf', 'doc', 'xls', 'ppt', 'audio', 'video', 'other')",
            name="ck_project_files_type",
        ),
        sa.CheckConstraint(
            "size_bytes IS NULL OR size_bytes >= 0",
            name="ck_project_files_size_non_negative",
        ),
    )
    op.create_index(
        "idx_project_files_project_active",
        "project_files",
        ["project_id", "uploaded_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # ── FK conversations.project_id ──────────────────────────────────
    op.add_column(
        "conversations",
        sa.Column("project_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_conversations_project",
        "conversations",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "idx_conversations_project",
        "conversations",
        ["project_id"],
        postgresql_where=sa.text("project_id IS NOT NULL AND deleted_at IS NULL"),
    )


def downgrade() -> None:
    # Ordre inverse strict : FK et index sur conversations d'abord.
    op.drop_index("idx_conversations_project", table_name="conversations")
    op.drop_constraint("fk_conversations_project", "conversations", type_="foreignkey")
    op.drop_column("conversations", "project_id")

    op.drop_index("idx_project_files_project_active", table_name="project_files")
    op.drop_table("project_files")

    op.execute("DROP INDEX IF EXISTS idx_projects_name_trgm")
    op.execute("DROP INDEX IF EXISTS uq_projects_user_name_active")
    op.drop_index("idx_projects_user_active", table_name="projects")
    op.drop_table("projects")
    # `pg_trgm` conservé : partagé avec C1.
