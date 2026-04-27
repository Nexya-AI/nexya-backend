"""Phase 18 mineur — `helpdesk_escalations` (escalation Crisp).

Revision ID: 019_helpdesk
Revises: 018_n1
Create Date: 2026-04-27

Session N4 volet B — Phase 18 backend mineur. Cette migration ajoute
1 table pour tracer les escalations automatiques vers Crisp quand un
user Pro rencontre un incident critique (paiement, LLM down, etc.).

## `helpdesk_escalations`
Trace local de chaque escalation tentée + réponse Crisp.

Design décisions :
- **FK SET NULL** sur `user_id` — RGPD-safe : un user purgé garde la
  trace forensic de son ticket (utile post-incident audit) sans
  conserver son identité.
- **`crisp_conversation_id` nullable** — si l'API Crisp est down au
  moment de l'escalation, on garde la trace locale (`status='open'`,
  `crisp_conversation_id=NULL`) et un cron de retry futur (V2) pourra
  rejouer la création du ticket Crisp. Le hook handlers.py est
  fail-safe absolu : Crisp KO ne cascade jamais sur la 500 user.
- **CHECK `category` ∈ {payment, llm_unavailable, data_loss, rgpd, security}** —
  5 catégories critiques mappées sur les exceptions backend hookées.
  V2 ajustable selon les types d'incident remontés.
- **CHECK `severity` ∈ {low, medium, high, critical}** — V1 escalation
  uniquement sur `high`+`critical`. `low`/`medium` loggés mais non
  envoyés vers Crisp (anti-spam ticket).
- **CHECK `status` ∈ {open, in_progress, resolved, cancelled}** —
  cycle de vie aligné Crisp. `open` = créé localement (peut-être pas
  encore poussé Crisp si KO), `in_progress` = pris par l'équipe,
  `resolved` = clos.
- **3 index partiels** :
  1. `(user_id, created_at DESC)` actifs — historique user pour
     dashboard support.
  2. `(severity, created_at DESC) WHERE status='open' AND severity IN ('high','critical')` —
     queue admin priorisée pour `GET /admin/helpdesk/metrics`.
  3. `crisp_conversation_id` partiel `WHERE crisp_conversation_id IS NOT NULL` —
     lookup rapide depuis un webhook Crisp inbound (V2).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "019_helpdesk"
down_revision = "018_n1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "helpdesk_escalations",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column(
            "payload_json",
            sa.dialects.postgresql.JSONB,
            nullable=True,
        ),
        sa.Column("crisp_conversation_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.CheckConstraint(
            "category IN ('payment','llm_unavailable','data_loss','rgpd','security')",
            name="ck_helpdesk_category",
        ),
        sa.CheckConstraint(
            "severity IN ('low','medium','high','critical')",
            name="ck_helpdesk_severity",
        ),
        sa.CheckConstraint(
            "status IN ('open','in_progress','resolved','cancelled')",
            name="ck_helpdesk_status",
        ),
        sa.CheckConstraint(
            "(resolved_at IS NULL) OR (resolved_at >= created_at)",
            name="ck_helpdesk_resolved_after_created",
        ),
    )

    # Index actifs par user (dashboard / forensic)
    op.create_index(
        "ix_helpdesk_user_created",
        "helpdesk_escalations",
        ["user_id", sa.text("created_at DESC")],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # Queue admin priorisée (open + high/critical)
    op.create_index(
        "ix_helpdesk_admin_queue",
        "helpdesk_escalations",
        ["severity", sa.text("created_at DESC")],
        postgresql_where=sa.text(
            "status = 'open' AND severity IN ('high', 'critical') AND deleted_at IS NULL"
        ),
    )

    # Lookup Crisp inbound (V2 webhook) — partial pour ne pas indexer les NULL
    op.create_index(
        "ix_helpdesk_crisp_lookup",
        "helpdesk_escalations",
        ["crisp_conversation_id"],
        postgresql_where=sa.text("crisp_conversation_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_helpdesk_crisp_lookup", table_name="helpdesk_escalations")
    op.drop_index("ix_helpdesk_admin_queue", table_name="helpdesk_escalations")
    op.drop_index("ix_helpdesk_user_created", table_name="helpdesk_escalations")
    op.drop_table("helpdesk_escalations")
