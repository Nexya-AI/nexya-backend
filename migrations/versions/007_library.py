"""Create `library_items` table — Library CRUD (Session C3).

Revision ID: 007_library
Revises: 006_projects
Create Date: 2026-04-24

Socle DB pour la Bibliothèque NEXYA — stocke les métadonnées des médias
(images générées par l'IA, futurs uploads, audios, vidéos, documents) et
pointe vers le binaire hébergé sur MinIO/S3 via `storage_key`.

Choix d'architecture clés :

- **Dédup par `(user_id, storage_key)` UNIQUE partiel** : la `storage_key`
  intègre le SHA-256 du contenu binaire. Une même image générée deux fois
  par le même user partage la même clé → la contrainte UNIQUE permet un
  `INSERT ... ON CONFLICT DO NOTHING RETURNING` qui renvoie l'entrée
  existante sans erreur. UX idempotente, économie storage ~30 % sur les
  cas d'usage réels (régénérations, favoris multi-tag).

- **Partiel `WHERE deleted_at IS NULL`** : un objet soft-deleté libère sa
  clé pour une ré-insertion future (cas : user supprime une image, puis la
  re-génère — nouveau row, nouvelle entrée propre, pas de conflit avec la
  corbeille).

- **FK `source_conversation_id ON DELETE SET NULL`** : une conversation
  supprimée physiquement (RGPD) détache ses médias sans les effacer. La
  bibliothèque reste consultable. Même logique pour `source_message_id`.

- **Colonnes `width_px` / `height_px` / `duration_ms` / `aspect_ratio`
  nullables** : pas de détection côté serveur au C3 (pas de PIL/ffprobe).
  Le client POST ses hints quand il les a, sinon le Flutter les calcule à
  la volée (`Image.memory`). E3 ajoutera l'extraction automatique.

- **`tags TEXT[]` + GIN index** : array natif Postgres pour la recherche
  `WHERE 'cuisine' = ANY(tags)` en O(log N). Phase 12 (Library enrichie)
  exploitera via `?tag=recette&tag=camerounais`.

- **`metadata_json JSONB`** : extension future pour watermark C2PA (E4),
  EXIF dépouillé, paramètres de génération complets (seed, guidance,
  etc.), sans migration DDL.

- **`content_sha256 CHAR(64)`** séparé de `storage_key` : dédup explicite
  possible sans parser la clé. Facilite un futur `GET /library?sha=...`
  pour vérifier « cette image existe-t-elle déjà dans ma biblio ? »
  avant un upload client.

Rollback propre : DROP INDEX (×6) + DROP TABLE. `pg_trgm` conservée
(partagée C1 + C2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID

revision = "007_library"
down_revision = "006_projects"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Table library_items ──────────────────────────────────────────
    op.create_table(
        "library_items",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("file_type", sa.String(16), nullable=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("mime_type", sa.String(128), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column("width_px", sa.Integer(), nullable=True),
        sa.Column("height_px", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("aspect_ratio", sa.Numeric(6, 4), nullable=True),
        sa.Column(
            "source",
            sa.String(16),
            server_default="uploaded",
            nullable=False,
        ),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("source_conversation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("source_message_id", UUID(as_uuid=True), nullable=True),
        sa.Column("tags", ARRAY(sa.String()), nullable=True),
        sa.Column("metadata_json", JSONB(), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["source_conversation_id"],
            ["conversations.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["source_message_id"],
            ["messages.id"],
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "type IN ('image', 'video', 'gif', 'audio', 'document', 'text')",
            name="ck_library_items_type",
        ),
        sa.CheckConstraint(
            "file_type IS NULL OR file_type IN ('pdf', 'docx', 'xlsx', 'pptx', 'other')",
            name="ck_library_items_file_type",
        ),
        sa.CheckConstraint(
            "char_length(trim(title)) BETWEEN 1 AND 200",
            name="ck_library_items_title_length",
        ),
        sa.CheckConstraint(
            "description IS NULL OR char_length(description) <= 2000",
            name="ck_library_items_description_length",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0",
            name="ck_library_items_size_non_negative",
        ),
        sa.CheckConstraint(
            "width_px IS NULL OR width_px > 0",
            name="ck_library_items_width_positive",
        ),
        sa.CheckConstraint(
            "height_px IS NULL OR height_px > 0",
            name="ck_library_items_height_positive",
        ),
        sa.CheckConstraint(
            "duration_ms IS NULL OR duration_ms >= 0",
            name="ck_library_items_duration_non_negative",
        ),
        sa.CheckConstraint(
            "source IN ('generated', 'uploaded', 'imported', 'shared')",
            name="ck_library_items_source",
        ),
        sa.CheckConstraint(
            "prompt IS NULL OR char_length(prompt) <= 4000",
            name="ck_library_items_prompt_length",
        ),
        # `content_sha256` = 64 chars hex (sha256 digest).
        sa.CheckConstraint(
            "char_length(content_sha256) = 64",
            name="ck_library_items_sha256_length",
        ),
    )

    # ── Index partiels (tous actifs uniquement) ──────────────────────

    # 1. Liste principale : convs d'un user, tri récence DESC.
    op.create_index(
        "idx_library_user_active",
        "library_items",
        ["user_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # 2. Dédup par (user, storage_key) — SHA-based : même contenu uploadé
    # deux fois par le même user déclenche ON CONFLICT DO NOTHING dans le
    # service.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_library_user_storage_key_active
            ON library_items (user_id, storage_key)
            WHERE deleted_at IS NULL
        """
    )

    # 3. Filtre onglet (Images / Vidéos / Audios / Documents) — UX Flutter
    # LibraryScreen TabBar.
    op.create_index(
        "idx_library_user_type",
        "library_items",
        ["user_id", "type", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # 4. Filtre contextuel : « tous les médias issus de cette conversation ».
    op.create_index(
        "idx_library_user_conversation",
        "library_items",
        ["user_id", "source_conversation_id"],
        postgresql_where=sa.text("deleted_at IS NULL AND source_conversation_id IS NOT NULL"),
    )

    # 5. Fuzzy match sur le titre (trigram, réutilise pg_trgm posée en C1).
    op.execute(
        """
        CREATE INDEX idx_library_title_trgm
            ON library_items USING GIN (title gin_trgm_ops)
            WHERE deleted_at IS NULL
        """
    )

    # 6. Tags — GIN natif sur array Postgres (pour `'cuisine' = ANY(tags)`).
    op.execute(
        """
        CREATE INDEX idx_library_tags_gin
            ON library_items USING GIN (tags)
            WHERE deleted_at IS NULL
        """
    )


def downgrade() -> None:
    # Ordre inverse strict.
    op.execute("DROP INDEX IF EXISTS idx_library_tags_gin")
    op.execute("DROP INDEX IF EXISTS idx_library_title_trgm")
    op.drop_index("idx_library_user_conversation", table_name="library_items")
    op.drop_index("idx_library_user_type", table_name="library_items")
    op.execute("DROP INDEX IF EXISTS uq_library_user_storage_key_active")
    op.drop_index("idx_library_user_active", table_name="library_items")
    op.drop_table("library_items")
