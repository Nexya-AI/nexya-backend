"""Align `memories` + `document_chunks` embedding dim 1536 -> 768 (Gemini).

Bug 2026-06-25 — Mémoire IA « impossible d'enregistrer ».

Cause racine :
- En prod, seule la clé `GEMINI_API_KEY` est renseignée (`OPENAI_API_KEY`
  vide jusqu'au post-PayPal). `get_embeddings_provider()` choisit donc
  `GeminiEmbeddingsProvider`, qui produit nativement des vecteurs **768 dim**
  (`gemini-embedding-001`).
- Or `memories.embedding` et `document_chunks.embedding` ont été figés à
  `vector(1536)` (sessions D1/D4, alignés sur OpenAI `text-embedding-3-small`).
- À chaque INSERT, Postgres rejette le vecteur 768 dans une colonne
  `vector(1536)` -> l'ajout mémoire (et l'indexation RAG des documents)
  échoue silencieusement. Le corpus expert (`expert_corpus_chunks`, déjà en
  `vector(768)` depuis G1) fonctionne, lui — d'où la cuisine RAG OK.

Décision : aligner toute la stack embeddings sur **768** (le dim Gemini
déployé et fonctionnel), comme le corpus. Les deux tables sont VIDES en prod
(la feature a toujours échoué) -> `TRUNCATE` + `ALTER` sans perte de donnée.

Le `chunks_indexed_at` des `uploaded_files` est remis à NULL pour que les
documents déjà uploadés puissent être ré-indexés à la bonne dimension lors
d'un prochain passage (ou re-upload). Aucune ligne `document_chunks`
n'existait réellement (l'indexation échouait sur la dim), donc rien à perdre.

⚠️ Si Ivan récupère plus tard une clé OpenAI (1536 dim), ce sera une
migration inverse + une ré-ingestion complète (idem corpus). Documenté dans
`app/ai/embeddings/runtime.py`.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "031_align_embeddings_dim_gemini_768"
down_revision = "030_lifecycle_emails"
branch_labels = None
depends_on = None


_HNSW_PARAMS = "WITH (m = 16, ef_construction = 64)"


def upgrade() -> None:
    # ── memories : 1536 -> 768 ──────────────────────────────────
    op.execute("DROP INDEX IF EXISTS ix_memories_embedding_hnsw")
    # Vide (en prod la feature échouait à chaque INSERT) — TRUNCATE pour
    # permettre l'ALTER TYPE sans cast dimensionnel impossible.
    op.execute("TRUNCATE TABLE memories")
    op.execute("ALTER TABLE memories ALTER COLUMN embedding TYPE vector(768)")
    op.execute("ALTER TABLE memories ALTER COLUMN embedding_dim SET DEFAULT 768")
    op.execute(
        f"""
        CREATE INDEX ix_memories_embedding_hnsw
            ON memories
            USING hnsw (embedding vector_cosine_ops)
            {_HNSW_PARAMS}
            WHERE deleted_at IS NULL
        """
    )

    # ── document_chunks : 1536 -> 768 ───────────────────────────
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_hnsw")
    op.execute("TRUNCATE TABLE document_chunks")
    op.execute("ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector(768)")
    op.execute(
        "ALTER TABLE document_chunks "
        "ALTER COLUMN embedding_model SET DEFAULT 'gemini-embedding-001'"
    )
    op.execute(
        f"""
        CREATE INDEX ix_document_chunks_embedding_hnsw
            ON document_chunks
            USING hnsw (embedding vector_cosine_ops)
            {_HNSW_PARAMS}
        """
    )
    # Les documents déjà uploadés pourront se ré-indexer à 768.
    op.execute("UPDATE uploaded_files SET chunks_indexed_at = NULL")


def downgrade() -> None:
    # Retour à 1536 (OpenAI). Tables vidées — ré-ingestion requise après coup.
    op.execute("DROP INDEX IF EXISTS ix_document_chunks_embedding_hnsw")
    op.execute("TRUNCATE TABLE document_chunks")
    op.execute("ALTER TABLE document_chunks ALTER COLUMN embedding TYPE vector(1536)")
    op.execute(
        "ALTER TABLE document_chunks "
        "ALTER COLUMN embedding_model SET DEFAULT 'text-embedding-3-small'"
    )
    op.execute(
        f"""
        CREATE INDEX ix_document_chunks_embedding_hnsw
            ON document_chunks
            USING hnsw (embedding vector_cosine_ops)
            {_HNSW_PARAMS}
        """
    )

    op.execute("DROP INDEX IF EXISTS ix_memories_embedding_hnsw")
    op.execute("TRUNCATE TABLE memories")
    op.execute("ALTER TABLE memories ALTER COLUMN embedding TYPE vector(1536)")
    op.execute("ALTER TABLE memories ALTER COLUMN embedding_dim SET DEFAULT 1536")
    op.execute(
        f"""
        CREATE INDEX ix_memories_embedding_hnsw
            ON memories
            USING hnsw (embedding vector_cosine_ops)
            {_HNSW_PARAMS}
            WHERE deleted_at IS NULL
        """
    )
