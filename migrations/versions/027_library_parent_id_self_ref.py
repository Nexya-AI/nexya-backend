"""Add `library_items.parent_library_id` self-ref FK + partial index (C4.11).

Revision ID: 027_library_parent_id_self_ref
Revises: 026_library_code_type
Create Date: 2026-06-04 (C4.11 — Versioning auto docs v1/v2/v3).

Architecture du versioning (C4.11) :

  - `parent_library_id UUID NULL FK library_items(id) ON DELETE SET NULL`
    : référence vers le doc RACINE d'une lignée de versions. NULL = c'est
    le doc racine lui-même (version 1). Non-NULL = c'est une version
    descendante (v2, v3, ...) qui pointe vers la racine.

  - Pattern « arbre plat » : on n'utilise PAS un linked-list (v3→v2→v1)
    car ça forcerait des JOINs récursifs côté SQL. Tous les enfants
    pointent directement vers la racine, ce qui permet de récupérer
    toutes les versions d'un lineage en 1 SELECT trivial :
      `WHERE id = root_id OR parent_library_id = root_id`.

  - `ON DELETE SET NULL` : si l'user soft-delete la racine, les
    versions descendantes survivent (leur `parent_library_id` reste
    pointant — soft-delete préserve la ligne). Si l'user HARD-delete
    la racine (RGPD Article 17 K1, hors scope V1), les descendantes
    deviennent orphelines (parent_library_id=NULL) — elles restent
    consultables individuellement, juste sans lineage. Anti-perte de
    données par cascade involontaire.

  - **`version_number` n'est PAS une colonne** dédiée — il est stocké
    dans `metadata_json.version_number` (déjà JSONB en place depuis C3),
    minimal disruption schéma. La valeur est calculée à la création
    via `MAX(version_number) + 1` sur les siblings actifs.

  - **Detection de régénération SOUPLE** (décision senior C4.11) :
    deux docs sont liés s'ils partagent `(user_id, source_message_id)`
    ET sont `source='generated'` ET non soft-deleted. Le `file_type`
    est IGNORÉ : un user qui régénère un message en PDF puis DOCX
    obtient v1+v2 du même « doc logique » (UX intuitive « mes
    versions du compte rendu » sans se soucier du format).

Index partiel `idx_library_versions` :
  - Scope : `user_id + parent_library_id` partial
    `WHERE deleted_at IS NULL AND parent_library_id IS NOT NULL`
  - Usage hot-path : `GET /library/{root_id}/versions` (V2) ou
    `versions_count` calculé via `SELECT COUNT(*) FROM library_items
    WHERE user_id=? AND parent_library_id=? AND deleted_at IS NULL`
  - Partiel pour rester compact (la majorité des items sont racines
    `parent_library_id IS NULL` — typiquement 95% du volume).

Rollback strict inverse : DROP INDEX + DROP CONSTRAINT + DROP COLUMN.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

# revision identifiers, used by Alembic.
revision = "027_library_parent_id_self_ref"
down_revision = "026_library_code"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Ajoute `library_items.parent_library_id` self-ref FK + index partiel."""
    # 1) Nouvelle colonne UUID nullable (NULL = doc racine de son lineage).
    op.add_column(
        "library_items",
        sa.Column("parent_library_id", UUID(as_uuid=True), nullable=True),
    )

    # 2) Foreign Key self-ref ON DELETE SET NULL (anti-perte de données).
    op.create_foreign_key(
        "fk_library_items_parent",
        source_table="library_items",
        referent_table="library_items",
        local_cols=["parent_library_id"],
        remote_cols=["id"],
        ondelete="SET NULL",
    )

    # 3) Index partiel pour le hot-path versioning :
    #    - `versions_count` côté router (`SELECT COUNT(*) WHERE
    #      user_id=? AND parent_library_id=?`)
    #    - Future endpoint `GET /library/{root_id}/versions` (V2).
    op.create_index(
        "idx_library_versions",
        "library_items",
        ["user_id", "parent_library_id"],
        postgresql_where=sa.text("deleted_at IS NULL AND parent_library_id IS NOT NULL"),
    )


def downgrade() -> None:
    """Rollback strict inverse : DROP INDEX + FK + COLUMN."""
    op.drop_index("idx_library_versions", table_name="library_items")
    op.drop_constraint(
        "fk_library_items_parent",
        "library_items",
        type_="foreignkey",
    )
    op.drop_column("library_items", "parent_library_id")
