"""Add 'code' to library_items.type CHECK + 'zip' to file_type CHECK (C4.6).

Revision ID: 026_library_code
Revises: 025_messages_metadata
Create Date: 2026-05-30 (C4.6 — Code File + Code Project draft cards).

Étend les CHECK constraints de la table `library_items` pour supporter
le nouveau type `'code'` (single code files OU .zip projets multi-fichiers
sauvegardés depuis NxCodeFileCard / NxCodeProjectCard) et le nouveau
file_type `'zip'` (mandatory si type='code' ET file est un .zip projet,
NULL si type='code' ET file est un single text code file).

Cohabitation type/file_type pour le nouveau type 'code' :
  - type='code', file_type=NULL → single code file (.py/.dart/.js/etc.)
    sauvegardé depuis NxCodeFileCard. Content base64 = texte UTF-8 du
    code source.
  - type='code', file_type='zip' → projet code multi-fichiers sauvegardé
    depuis NxCodeProjectCard. Content base64 = bytes du .zip MinIO
    construit côté backend via /code-projects/build-zip.

Le validator Pydantic `LibraryItemCreate.check_type_consistency` doit
être étendu côté Pydantic (cf. app/features/library/schemas.py) pour
relaxer la règle « file_type uniquement si type='document' » à
« file_type uniquement si type IN ('document', 'code') ».

Aucune migration de données — on ajoute des valeurs autorisées au CHECK,
les rows existantes restent valides.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "026_library_code"
down_revision = "025_messages_metadata"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Étend les CHECK constraints `library_items.type` et `file_type`.

    PostgreSQL ne supporte pas `ALTER CONSTRAINT` pour les CHECK —
    on DROP + RECREATE le constraint avec la nouvelle valeur incluse.
    """
    # 1) Étend type CHECK pour inclure 'code'.
    op.execute("ALTER TABLE library_items DROP CONSTRAINT ck_library_items_type")
    op.execute(
        "ALTER TABLE library_items ADD CONSTRAINT ck_library_items_type "
        "CHECK (type IN ('image', 'video', 'gif', 'audio', 'document', 'text', 'code'))"
    )

    # 2) Étend file_type CHECK pour inclure 'zip'.
    op.execute("ALTER TABLE library_items DROP CONSTRAINT ck_library_items_file_type")
    op.execute(
        "ALTER TABLE library_items ADD CONSTRAINT ck_library_items_file_type "
        "CHECK (file_type IS NULL OR file_type IN ('pdf', 'docx', 'xlsx', 'pptx', 'other', 'zip'))"
    )


def downgrade() -> None:
    """Restaure les CHECK constraints pré-C4.6.

    ⚠️  Note : si des rows `type='code'` existent en DB au moment du
    downgrade, le ALTER CONSTRAINT échouera (Postgres refuse d'ajouter
    une contrainte violée par des rows existantes). Dans ce cas,
    DELETE les rows code AVANT downgrade :
        DELETE FROM library_items WHERE type = 'code';
    """
    op.execute("ALTER TABLE library_items DROP CONSTRAINT ck_library_items_type")
    op.execute(
        "ALTER TABLE library_items ADD CONSTRAINT ck_library_items_type "
        "CHECK (type IN ('image', 'video', 'gif', 'audio', 'document', 'text'))"
    )

    op.execute("ALTER TABLE library_items DROP CONSTRAINT ck_library_items_file_type")
    op.execute(
        "ALTER TABLE library_items ADD CONSTRAINT ck_library_items_file_type "
        "CHECK (file_type IS NULL OR file_type IN ('pdf', 'docx', 'xlsx', 'pptx', 'other'))"
    )
