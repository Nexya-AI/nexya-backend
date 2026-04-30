"""Ai calls + usage daily — persistance des appels LLM et de la conso.

Deux tables :

- `ai_calls` : une ligne par appel LLM terminé. Détail forensic
  (provider, model, expert, tokens, cost_usd, outcome, latence,
  session_id…). Insertion fire-and-forget par `CostTracker.record_ai_call`
  en fin de stream SSE.
- `usage_daily` : agrégat par `(user_id, date_utc)`. PK composite,
  UPSERT atomique `ON CONFLICT DO UPDATE` pour que deux streams
  simultanés ne se marchent pas dessus. Sert les quotas journaliers
  et les analytics par user.

Conventions :
- FK `user_id` en `ON DELETE SET NULL` (RGPD : un delete account
  anonymise l'historique mais conserve la stat globale).
- `session_id` est un UUID nullable + UNIQUE — servira au flush du
  SessionStore Redis (brique B3 suivante) qui fera UPSERT si
  besoin sans créer de doublons.
- `cost_usd` en `NUMERIC(12, 6)` — 6 décimales suffisent pour les
  micro-coûts Gemini Flash (~$0.000032 par appel), la partie entière
  autorise jusqu'à ~999 999 USD (largement au-delà de tout appel
  individuel réaliste).

Revision ID: 004_ai_calls_usage_daily
Revises: 003_auth_hardening
Create Date: 2026-04-22
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision = "004_ai_calls_usage_daily"
down_revision = "003_auth_hardening"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Table ai_calls ────────────────────────────────────────
    op.create_table(
        "ai_calls",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        # FK user — nullable + ON DELETE SET NULL (RGPD safe).
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        # session_id = clé du SessionStore Redis ; NULL autorisé pour les
        # appels hors session (tests, migrations, batch). UNIQUE pour que
        # le flush arq du SessionStore puisse faire un UPSERT idempotent.
        sa.Column("session_id", UUID(as_uuid=True), nullable=True),
        # trace_id OpenTelemetry (corrélation logs ↔ appels).
        sa.Column("trace_id", sa.String(64), nullable=True),
        sa.Column("expert_id", sa.String(32), nullable=False),
        sa.Column("provider", sa.String(32), nullable=False),
        sa.Column("model", sa.String(128), nullable=False),
        sa.Column("prompt_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("completion_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_tokens", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default="0", nullable=False),
        sa.Column("outcome", sa.String(16), nullable=False),
        sa.Column("failure_code", sa.String(32), nullable=True),
        sa.Column("first_chunk_ms", sa.Integer(), nullable=True),
        sa.Column("total_duration_ms", sa.Integer(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default="1", nullable=False),
        sa.Column("fallback_used", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("extra", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("session_id", name="uq_ai_calls_session_id"),
        sa.CheckConstraint(
            "outcome IN ('completed', 'cancelled', 'failed')",
            name="ck_ai_calls_outcome",
        ),
        sa.CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0",
            name="ck_ai_calls_tokens_non_negative",
        ),
        sa.CheckConstraint(
            "cost_usd >= 0",
            name="ck_ai_calls_cost_non_negative",
        ),
    )
    # Requêtes analytiques fréquentes : "tous les appels d'un user
    # dans une fenêtre de temps" (quota/analytics), "tous les appels
    # avec tel outcome", "tous les appels d'un provider/modèle" (audit
    # de fiabilité).
    op.create_index("idx_ai_calls_user_time", "ai_calls", ["user_id", "created_at"])
    op.create_index("idx_ai_calls_outcome", "ai_calls", ["outcome", "created_at"])
    op.create_index(
        "idx_ai_calls_provider_model",
        "ai_calls",
        ["provider", "model", "created_at"],
    )

    # ── Table usage_daily ──────────────────────────────────────
    # PK composite (user_id, date_utc) ; FK user avec
    # ON DELETE SET NULL, donc on a besoin que `user_id` soit
    # nullable. Le NULL "bucket anonyme" agrège les stats
    # post-suppression RGPD pour ne pas perdre la vue globale.
    op.create_table(
        "usage_daily",
        sa.Column("user_id", UUID(as_uuid=True), nullable=True),
        sa.Column("date_utc", sa.Date(), nullable=False),
        sa.Column("chat_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chat_tokens_in", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chat_tokens_out", sa.Integer(), server_default="0", nullable=False),
        sa.Column("image_calls", sa.Integer(), server_default="0", nullable=False),
        sa.Column("cost_usd", sa.Numeric(12, 6), server_default="0", nullable=False),
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
        sa.PrimaryKeyConstraint("user_id", "date_utc", name="pk_usage_daily"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.CheckConstraint(
            "chat_calls >= 0 AND chat_tokens_in >= 0 AND chat_tokens_out >= 0 "
            "AND image_calls >= 0 AND cost_usd >= 0",
            name="ck_usage_daily_counters_non_negative",
        ),
    )
    # Index partiel : quand on filtre par date (analytics "tous les
    # users actifs aujourd'hui"), un index sur date_utc seul est plus
    # efficace que la PK (PK = user_id, date_utc → tri sur user_id).
    op.create_index("idx_usage_daily_date", "usage_daily", ["date_utc"])


def downgrade() -> None:
    op.drop_table("usage_daily")
    op.drop_table("ai_calls")
