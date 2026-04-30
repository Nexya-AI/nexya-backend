"""Endpoints divers — `message_feedback` + `user_suggestions`.

Revision ID: 018_n1
Revises: 017_rgpd
Create Date: 2026-04-27

Session N1 — 4 endpoints manquants (feedback chat, voice/list, models,
suggestions). Cette migration ajoute 2 tables :

## `message_feedback`
Trace les thumbs up/down posés par un user sur un message assistant.

Design décisions :
- **Table dédiée** plutôt qu'une colonne sur `messages` — normalisation
  (1 user × 1 message = UNIQUE composite), évolutivité future (comments,
  helpful_votes), schema clarté (auth_events, abuse_reports,
  message_feedback : 3 tables forensic distinctes).
- **UPSERT atomique** côté service via
  `pg_insert.on_conflict_do_update(index_elements=['user_id','message_id'])`.
  La race TOCTOU entre 2 clics thumbs simultanés est éliminée au niveau DB.
- **FK CASCADE** sur user_id ET message_id : si l'user est purgé RGPD ou
  la conversation supprimée physiquement, le feedback disparaît.
- **`comment` ≤ 1000 chars** (pas 2000) — le feedback sert à signaler une
  réponse mauvaise rapidement, pas à écrire un essai.

## `user_suggestions`
Formulaire user → équipe NEXYA pour bugs / feature requests.

Design décisions :
- **FK SET NULL** sur user_id — RGPD-safe : si l'user demande la
  suppression, on garde la suggestion anonyme (utile roadmap produit).
  Un email a déjà été envoyé à l'équipe au moment de la submit, donc
  l'info est doublement préservée.
- **`processing_status`** : queue admin V2. V1 = `'open'` par défaut,
  l'équipe le passe à `in_review` / `resolved` / `wontfix` manuellement
  (pas d'endpoint admin V1 — UI V2).
- **Index partiel `WHERE processing_status='open'`** — la queue admin
  ne lira que les `open`, l'index ne grossit pas avec les
  `resolved/wontfix` historiques.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "018_n1"
down_revision = "017_rgpd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ═══════════════════════════════════════════════════════════════
    # TABLE : message_feedback
    # ═══════════════════════════════════════════════════════════════
    op.create_table(
        "message_feedback",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "message_id",
            UUID(as_uuid=True),
            sa.ForeignKey("messages.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rating", sa.String(16), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
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
        sa.CheckConstraint(
            "rating IN ('like', 'dislike')",
            name="ck_message_feedback_rating",
        ),
        sa.CheckConstraint(
            "comment IS NULL OR char_length(comment) <= 1000",
            name="ck_message_feedback_comment_length",
        ),
        sa.UniqueConstraint("user_id", "message_id", name="uq_message_feedback_user_message"),
    )
    # Index pour l'agrégat futur N3 « combien de like/dislike sur ce
    # message ? » — utilisé par les évals IA pour calibrer la qualité.
    op.create_index(
        "idx_message_feedback_message_rating",
        "message_feedback",
        ["message_id", "rating"],
    )

    # ═══════════════════════════════════════════════════════════════
    # TABLE : user_suggestions
    # ═══════════════════════════════════════════════════════════════
    op.create_table(
        "user_suggestions",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("suggestion_type", sa.String(32), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(256), nullable=True),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "processing_status",
            sa.String(16),
            server_default="open",
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "suggestion_type IN ('bug', 'feature', 'expert_domain', 'other')",
            name="ck_user_suggestions_type",
        ),
        sa.CheckConstraint(
            "char_length(body) BETWEEN 1 AND 2000",
            name="ck_user_suggestions_body_length",
        ),
        sa.CheckConstraint(
            "processing_status IN ('open', 'in_review', 'resolved', 'wontfix')",
            name="ck_user_suggestions_status",
        ),
    )
    # Index partiel pour la queue admin V2 — ne lit que les `open`.
    op.create_index(
        "idx_user_suggestions_open_time",
        "user_suggestions",
        ["created_at"],
        postgresql_where=sa.text("processing_status = 'open'"),
    )


def downgrade() -> None:
    op.drop_index("idx_user_suggestions_open_time", table_name="user_suggestions")
    op.drop_table("user_suggestions")
    op.drop_index("idx_message_feedback_message_rating", table_name="message_feedback")
    op.drop_table("message_feedback")
