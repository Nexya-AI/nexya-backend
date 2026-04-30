"""Create `memories` table with pgvector HNSW index — Session D1.

Revision ID: 009_memories
Revises: 008_uploaded_files
Create Date: 2026-04-24

Session D1 — Socle DB de la **mémoire IA à long terme** de NEXYA.

Cette migration pose les fondations que les sessions D2 (extraction
post-conversation), D3 (injection dans system prompt) et D5 (RAG
endpoint `/memory/search`) consommeront. D1 n'expose **aucun endpoint
HTTP** — c'est uniquement le socle DB + wrapper embeddings + service
interne.

Choix architecturaux clés :

- **Extension pgvector** : ajoute le type `vector(N)` à Postgres. Même
  idempotence `CREATE EXTENSION IF NOT EXISTS` que `pg_trgm` (C1).

- **Dimension 1536 figée** : aligne la colonne sur le modèle par défaut
  OpenAI `text-embedding-3-small` (1536 dim). Changer la dimension
  implique une migration backfill coûteuse — on le fait une fois
  proprement. Le Mock dev produit aussi des vecteurs 1536 dim pour
  rester compatible.

- **Index HNSW (Hierarchical Navigable Small World)** avec
  `vector_cosine_ops` : structure arborescente qui rend la recherche
  top-K vecteurs en O(log N) au lieu d'un full-scan O(N). Paramètres
  `m=16, ef_construction=64` : défauts documentés pgvector, bon
  compromis recall/latence pour < 10M vecteurs. Tuneable en phase 12
  si la charge réelle l'exige.

- **Opérateur cosinus `<=>`** : retourne une **distance** (0 =
  identique, 2 = opposé). Le service convertit en `similarity =
  1 - distance` pour exposer `[0..1]` côté API (1 = parfait match).

- **Dédup `(user_id, content_sha256)` UNIQUE partielle** : aligné sur
  C3 Library. Un même user qui ré-indexe le même contenu retourne
  l'entrée existante sans double appel API OpenAI (économie directe
  mesurable — embeddings = facturés au token).

- **FK `source_conversation_id` / `source_message_id` ON DELETE SET
  NULL** : une conversation / message supprimé détache la mémoire sans
  la perdre (même discipline que Library C3). La mémoire « Ivan est
  dev Flutter » extraite d'une conv supprimée 3 mois plus tard reste
  pertinente.

- **`embedding_model` + `embedding_dim`** stockés sur chaque row :
  traçabilité forensique. Le jour où on migre vers `text-embedding-3-
  large` (3072 dim), on pourra identifier les rows à backfill vs
  celles déjà indexées avec le nouveau modèle.

Rollback : inverse strict. Extension `vector` conservée (partagée
avec de futures features RAG D4/D5).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "009_memories"
down_revision = "008_uploaded_files"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Extension pgvector ─────────────────────────────────────
    # Idempotent — équivalent pattern C1 pour pg_trgm.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ── 2. Table memories ─────────────────────────────────────────
    op.create_table(
        "memories",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        # Colonne vector(1536) déclarée en SQL brut — SQLAlchemy
        # Core ne connaît pas nativement `vector`, on passe par
        # `sa.text()` dans op.execute() après le create_table basic.
        sa.Column("embedding_model", sa.String(64), nullable=False),
        sa.Column("embedding_dim", sa.SmallInteger(), nullable=False, server_default="1536"),
        sa.Column(
            "source",
            sa.String(16),
            nullable=False,
            server_default="manual",
        ),
        sa.Column("source_conversation_id", UUID(as_uuid=True), nullable=True),
        sa.Column("source_message_id", UUID(as_uuid=True), nullable=True),
        sa.Column(
            "importance",
            sa.SmallInteger(),
            nullable=False,
            server_default="1",
        ),
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
            "char_length(trim(content)) BETWEEN 1 AND 2000",
            name="ck_memories_content_length",
        ),
        sa.CheckConstraint(
            "char_length(content_sha256) = 64",
            name="ck_memories_sha256_length",
        ),
        sa.CheckConstraint(
            "source IN ('manual','extracted','imported','system')",
            name="ck_memories_source",
        ),
        sa.CheckConstraint(
            "importance BETWEEN 0 AND 10",
            name="ck_memories_importance_range",
        ),
        sa.CheckConstraint(
            "embedding_dim > 0",
            name="ck_memories_dim_positive",
        ),
    )

    # ── 3. Colonne vector(1536) — ajoutée en SQL brut ────────────
    # SQLAlchemy Core ne déclare pas nativement `vector`. On utilise
    # ALTER TABLE ADD COLUMN ... vector(1536) NOT NULL après le create.
    op.execute(
        """
        ALTER TABLE memories
        ADD COLUMN embedding vector(1536) NOT NULL
        """
    )

    # ── 4. Index partiels (scope actifs uniquement) ──────────────

    # a. Liste user tri récence DESC — exploite ix lors des listings
    # « toutes mes mémoires » Phase D5.
    op.create_index(
        "ix_memories_user_active",
        "memories",
        ["user_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # b. **Dédup par contenu** — UNIQUE partiel, aligné pattern
    # Library C3 / Uploads E3. Un même user qui ré-indexe exactement
    # le même contenu → ON CONFLICT DO NOTHING + SELECT existant, pas
    # de double appel API OpenAI.
    op.execute(
        """
        CREATE UNIQUE INDEX uq_memories_user_sha_active
            ON memories (user_id, content_sha256)
            WHERE deleted_at IS NULL
        """
    )

    # c. Filtre par source (manual / extracted / imported / system).
    # Utile pour « ne me montrer que les mémoires que J'AI ajoutées »
    # (manual) vs « ce que l'IA a extrait de mes conversations »
    # (extracted).
    op.create_index(
        "ix_memories_user_source",
        "memories",
        ["user_id", "source"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # d. Filtre par conversation source — pour retrouver toutes les
    # mémoires extraites d'une conv spécifique (D2 post-conversation).
    op.create_index(
        "ix_memories_conv",
        "memories",
        ["source_conversation_id"],
        postgresql_where=sa.text("deleted_at IS NULL AND source_conversation_id IS NOT NULL"),
    )

    # e. **Index HNSW vectoriel** — la brique critique pour la perf.
    # O(log N) lookup top-K via structure arborescente petit-monde.
    # Paramètres m=16, ef_construction=64 = défauts pgvector,
    # bon compromis recall/latence < 10M vecteurs. `vector_cosine_ops`
    # car notre Mock + OpenAI produisent tous deux des vecteurs
    # normalisés L2 (cosinus = inner product pour vecteurs unitaires).
    # WHERE deleted_at IS NULL : l'index ne contient que les actifs,
    # reste petit même avec une grosse corbeille.
    op.execute(
        """
        CREATE INDEX ix_memories_embedding_hnsw
            ON memories
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
            WHERE deleted_at IS NULL
        """
    )


def downgrade() -> None:
    # Ordre inverse strict.
    op.execute("DROP INDEX IF EXISTS ix_memories_embedding_hnsw")
    op.drop_index("ix_memories_conv", table_name="memories")
    op.drop_index("ix_memories_user_source", table_name="memories")
    op.execute("DROP INDEX IF EXISTS uq_memories_user_sha_active")
    op.drop_index("ix_memories_user_active", table_name="memories")
    op.drop_table("memories")
    # Extension `vector` conservée (partagée — ne pas DROP).
