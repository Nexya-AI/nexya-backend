"""FTS française sur `messages.content` + trigram sur `conversations.title`.

Revision ID: 005_fts_search
Revises: 004_ai_calls_usage_daily
Create Date: 2026-04-22

Session C1 — recherche full-text côté Historique.

Deux index distincts, deux usages distincts :

- **`messages.search_vector`** — colonne générée STORED `tsvector` issue de
  `to_tsvector('french', coalesce(content, ''))`. Expression `IMMUTABLE`
  compatible avec `GENERATED ALWAYS ... STORED` (le dictionnaire `french`
  gère les stems et les stop-words FR). L'index GIN rend le match
  `@@ plainto_tsquery('french', :q)` O(log n) même à plusieurs millions de
  lignes. Pas de trigger à maintenir (la valeur est recalculée par Postgres
  à chaque INSERT/UPDATE de `content`).

- **`conversations.title`** — index GIN trigram (`gin_trgm_ops`) sur
  `coalesce(title, '')`. Sert le ILIKE `%q%` pour un fuzzy match tolérant
  aux fautes de frappe sur les titres courts (≤ 120 chars). pg_trgm est
  nécessaire car le tsvector n'est pas adapté à des chaînes aussi courtes
  (les titres sont générés par Gemini Flash, souvent sans verbe).

Le reste de la recherche (OR entre match titre et EXISTS sur messages) est
composé dans `ConversationService.list_for_user` — ici, on ne fait que
poser le socle DB.
"""

from __future__ import annotations

from alembic import op

revision = "005_fts_search"
down_revision = "004_ai_calls_usage_daily"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # pg_trgm — nécessaire pour le trigram fuzzy sur les titres de conv.
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    # Colonne générée `search_vector` sur messages.
    # Expression IMMUTABLE requise par GENERATED STORED : la forme à deux
    # arguments `to_tsvector('french', text)` l'est (le 1ᵉʳ arg est un
    # regconfig literal, pas un cast dynamique).
    op.execute(
        """
        ALTER TABLE messages
        ADD COLUMN search_vector tsvector
            GENERATED ALWAYS AS (to_tsvector('french', coalesce(content, '')))
            STORED
        """
    )

    # Index GIN sur la colonne générée — exploité par `@@ plainto_tsquery`.
    op.execute(
        """
        CREATE INDEX idx_messages_search_vector
            ON messages USING GIN (search_vector)
        """
    )

    # Index GIN trigram sur `conversations.title` (coalesce pour gérer les
    # titres NULL — avant que l'auto-title soit posé). Accélère les ILIKE
    # `%q%` à 3+ caractères.
    op.execute(
        """
        CREATE INDEX idx_conversations_title_trgm
            ON conversations
            USING GIN ((coalesce(title, '')) gin_trgm_ops)
        """
    )


def downgrade() -> None:
    # Ordre inverse de l'upgrade. Extension `pg_trgm` laissée en place :
    # la retirer casserait potentiellement d'autres migrations ou scripts
    # qui l'utilisent — retrait explicite seulement si nécessaire.
    op.execute("DROP INDEX IF EXISTS idx_conversations_title_trgm")
    op.execute("DROP INDEX IF EXISTS idx_messages_search_vector")
    op.execute("ALTER TABLE messages DROP COLUMN IF EXISTS search_vector")
