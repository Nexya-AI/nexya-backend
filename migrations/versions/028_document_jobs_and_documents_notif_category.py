"""Table document_jobs + catégorie notif 'documents' (C4.12).

Revision ID: 028_document_jobs
Revises: 027_library_parent_id_self_ref
Create Date: 2026-06-05 (C4.12 — génération async de docs lourds + push FCM).

Deux volets dans une seule révision atomique :

1. **Table `document_jobs`** — trace l'état d'une génération de document
   déportée sur le worker arq (docs lourds > seuil). Permet le polling de
   secours côté Flutter, l'idempotence du worker, et l'audit forensic.

2. **Catégorie notification `documents`** — la notif « 📄 Ton doc est prêt »
   dispatchée par le worker a besoin d'une catégorie dédiée. Les CHECK
   constraints des tables `notifications` et `notification_preferences`
   n'autorisaient que `tasks/payments/security/digest/product` ; le CHECK
   `source_kind` n'autorisait pas `document_generator`. On étend les 3.

   ⚠️ Sans cette extension, `get_channel_for_category('documents')` renvoie
   `none` côté `NotificationPreferencesService` → AUCUN push silencieux. Le
   default channel `documents=push` est posé en Python (pas en SQL).

Aucune migration de données — on ajoute des valeurs autorisées aux CHECK,
les rows existantes restent valides.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "028_document_jobs"
down_revision = "027_library_parent_id_self_ref"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── 1. Table document_jobs ──────────────────────────────────────
    op.create_table(
        "document_jobs",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("conversation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("message_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "status",
            sa.String(16),
            server_default="queued",
            nullable=False,
        ),
        sa.Column("format", sa.String(8), nullable=False),
        sa.Column("template", sa.String(32), nullable=False),
        sa.Column(
            "params_json",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("library_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("filename", sa.String(255), nullable=True),
        sa.Column("pages", sa.Integer(), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=True),
        sa.Column("truncated", sa.Boolean(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('queued', 'processing', 'done', 'failed')",
            name="ck_document_jobs_status",
        ),
        sa.CheckConstraint(
            "format IN ('pdf', 'docx')",
            name="ck_document_jobs_format",
        ),
    )
    op.create_index(
        "ix_document_jobs_user_active",
        "document_jobs",
        ["user_id", sa.text("created_at DESC")],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    op.create_index(
        "ix_document_jobs_pending",
        "document_jobs",
        ["created_at"],
        postgresql_where=sa.text("status IN ('queued', 'processing')"),
    )

    # ── 2. Catégorie 'documents' + source_kind 'document_generator' ──
    # PostgreSQL ne supporte pas ALTER CONSTRAINT pour les CHECK : DROP+ADD.
    op.execute("ALTER TABLE notifications DROP CONSTRAINT ck_notifications_category")
    op.execute(
        "ALTER TABLE notifications ADD CONSTRAINT ck_notifications_category "
        "CHECK (category IN ('tasks','payments','security','digest','product','documents'))"
    )

    op.execute(
        "ALTER TABLE notification_preferences DROP CONSTRAINT ck_notification_prefs_category"
    )
    op.execute(
        "ALTER TABLE notification_preferences ADD CONSTRAINT ck_notification_prefs_category "
        "CHECK (category IN ('tasks','payments','security','digest','product','documents'))"
    )

    op.execute("ALTER TABLE notifications DROP CONSTRAINT ck_notifications_source_kind")
    op.execute(
        "ALTER TABLE notifications ADD CONSTRAINT ck_notifications_source_kind "
        "CHECK (source_kind IN "
        "('scheduled_task','payment','security','digest','product','manual','document_generator'))"
    )


def downgrade() -> None:
    # ── Restaure les CHECK pré-C4.12 ────────────────────────────────
    # ⚠️ Si des rows 'documents'/'document_generator' existent, le ALTER
    # échouera (Postgres refuse une contrainte violée). Purger d'abord :
    #   DELETE FROM notifications WHERE category='documents'
    #     OR source_kind='document_generator';
    #   DELETE FROM notification_preferences WHERE category='documents';
    op.execute("ALTER TABLE notifications DROP CONSTRAINT ck_notifications_source_kind")
    op.execute(
        "ALTER TABLE notifications ADD CONSTRAINT ck_notifications_source_kind "
        "CHECK (source_kind IN "
        "('scheduled_task','payment','security','digest','product','manual'))"
    )

    op.execute(
        "ALTER TABLE notification_preferences DROP CONSTRAINT ck_notification_prefs_category"
    )
    op.execute(
        "ALTER TABLE notification_preferences ADD CONSTRAINT ck_notification_prefs_category "
        "CHECK (category IN ('tasks','payments','security','digest','product'))"
    )

    op.execute("ALTER TABLE notifications DROP CONSTRAINT ck_notifications_category")
    op.execute(
        "ALTER TABLE notifications ADD CONSTRAINT ck_notifications_category "
        "CHECK (category IN ('tasks','payments','security','digest','product'))"
    )

    # ── Table document_jobs ─────────────────────────────────────────
    op.drop_index("ix_document_jobs_pending", table_name="document_jobs")
    op.drop_index("ix_document_jobs_user_active", table_name="document_jobs")
    op.drop_table("document_jobs")
