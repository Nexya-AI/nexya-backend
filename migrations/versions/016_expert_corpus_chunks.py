"""Create `expert_corpus_chunks` — corpus RAG globaux par expert (G1).

Revision ID: 016_expert_corpus_chunks
Revises: 015_notifications
Create Date: 2026-04-26

Session G1 — socle RAG Experts (Langues, puis G2-G7 par le même pattern).

## Différences clés vs D4 `document_chunks`

| Dimension               | `document_chunks` (D4)       | `expert_corpus_chunks` (G1)  |
|-------------------------|------------------------------|------------------------------|
| Propriétaire            | `user_id` (scope strict)     | Global (aucun user_id)       |
| Source                  | Upload user                  | Corpus curé (Tatoeba, etc.)  |
| Filtrage retrieval      | `WHERE user_id=:uid`         | `WHERE expert_slug=:slug`    |
| Dimension               | 1536 (OpenAI)                | **768 (Gemini)**             |
| Idempotence             | `UNIQUE(file_id,chunk_index)`| `UNIQUE(expert_slug,sha256)` |
| RGPD hard delete        | Oui (CASCADE user)           | Non (corpus public)          |
| Coût ingestion          | Variable (user upload)       | Fixe (one-shot admin)        |

Pas de FK vers `users` — le corpus est un bien commun de la plateforme,
pas une donnée personnelle. Les retrieval SQL n'auront jamais besoin de
joindre `users`, ce qui simplifie le plan d'exécution + évite un
vector IDOR cross-user absurde (il n'y a rien à isoler).

## Dim 768 figée au DDL (Gemini `text-embedding-004`)

Motivation : au 2026-04-26, seul `GEMINI_API_KEY` est rempli côté NEXYA.
Qualité retrieval Gemini 768 très proche de OpenAI 1536 sur les langues
européennes courantes — parfait pour le corpus Tatoeba FR/EN/ES/PT.

## Procédure backfill si switch futur vers dim 1536 (OpenAI)

1. `DROP INDEX ix_expert_corpus_embedding_hnsw;`
2. `ALTER TABLE expert_corpus_chunks ALTER COLUMN embedding TYPE vector(1536);`
   (la colonne doit être vidée — pgvector n'a pas de cast cross-dim).
3. `DELETE FROM expert_corpus_chunks WHERE expert_slug = 'language';`
4. `python scripts/import_expert_corpus_langues.py --force-reembed`
   (lit `EMBEDDINGS_PROVIDER=openai`, ré-embed Tatoeba FR/EN/ES/PT,
   idempotent via `ON CONFLICT DO NOTHING` sur `(expert_slug, content_sha256)`).
5. `CREATE INDEX ix_expert_corpus_embedding_hnsw ON expert_corpus_chunks
    USING hnsw (embedding vector_cosine_ops) WITH (m=16, ef_construction=64);`
6. Ajuster `expert_corpus_embedding_dim=1536` dans settings, redémarrer.

Estimé ~20 min + ~$0.12 pour le corpus Langues (6M tokens × $0.02/1M).

## Indexation HNSW

`vector_cosine_ops` — les embeddings Gemini sont normalisés L2, même
propriété que OpenAI/Mock. Paramètres défauts pgvector `m=16,
ef_construction=64` — tuneables Phase 12 selon rappel@k mesuré sur le
blind test G1 (`tests/eval_g1_langues/run_eval.py`).

Rollback : strict inverse. Extension `vector` conservée (partagée avec D1/D4).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "016_expert_corpus_chunks"
down_revision = "015_notifications"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Table expert_corpus_chunks (structure SQLAlchemy) ────
    op.create_table(
        "expert_corpus_chunks",
        sa.Column(
            "id",
            sa.BigInteger(),
            autoincrement=True,
            primary_key=True,
            nullable=False,
        ),
        sa.Column("expert_slug", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("content_sha256", sa.CHAR(64), nullable=False),
        sa.Column(
            "embedding_model",
            sa.String(64),
            nullable=False,
            server_default="gemini-embedding-001",
        ),
        sa.Column("source", sa.String(64), nullable=False),
        sa.Column("language_pair", sa.String(16), nullable=True),
        sa.Column(
            "metadata_json",
            JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "char_length(content_sha256) = 64",
            name="ck_expert_corpus_sha256_length",
        ),
        sa.CheckConstraint(
            "char_length(content) >= 1",
            name="ck_expert_corpus_content_non_empty",
        ),
        sa.UniqueConstraint(
            "expert_slug",
            "content_sha256",
            name="uq_expert_corpus_slug_sha",
        ),
    )

    # ── 2. Colonne vector(768) en SQL brut ──────────────────────
    # Même pattern que D1/D4 : SQLAlchemy Core ne connaît pas `vector`
    # nativement, on ajoute la colonne via ALTER TABLE.
    op.execute(
        """
        ALTER TABLE expert_corpus_chunks
        ADD COLUMN embedding vector(768) NOT NULL
        """
    )

    # ── 3. Index btree sur slug + slug×lang ─────────────────────
    op.create_index(
        "ix_expert_corpus_slug",
        "expert_corpus_chunks",
        ["expert_slug"],
    )
    op.create_index(
        "ix_expert_corpus_slug_lang",
        "expert_corpus_chunks",
        ["expert_slug", "language_pair"],
    )

    # ── 4. Index HNSW vectoriel ────────────────────────────────
    op.execute(
        """
        CREATE INDEX ix_expert_corpus_embedding_hnsw
            ON expert_corpus_chunks
            USING hnsw (embedding vector_cosine_ops)
            WITH (m = 16, ef_construction = 64)
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_expert_corpus_embedding_hnsw")
    op.drop_index("ix_expert_corpus_slug_lang", table_name="expert_corpus_chunks")
    op.drop_index("ix_expert_corpus_slug", table_name="expert_corpus_chunks")
    op.drop_table("expert_corpus_chunks")
    # Extension `vector` conservée (partagée — ne pas DROP).
