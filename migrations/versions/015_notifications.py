"""Create `notifications` + `notification_preferences` — Session F3 Notifications.

Revision ID: 015_notifications
Revises: 014_scheduled_tasks
Create Date: 2026-04-25

Session F3 — NotificationDispatcher dual-channel (push FCM + email fallback)
avec préférences par catégorie × canal + timeline in-app persistée + lien
unsubscribe one-click RGPD/CAN-SPAM.

## Tables

### `notifications` — timeline in-app

Trace **chaque envoi** de notification à l'utilisateur, qu'il ait réussi
en push, en email, en les deux, ou échoué. Sert à :
- Afficher la timeline in-app (`GET /notifications`).
- Audit forensic (« combien d'emails digest sur les 7 derniers jours ? »).
- Diagnostic client (« mon push ne part pas ? » → lookup du row :
  `channel_used='skipped'` + `attempts_push=3` → device invalide).

Design décisions :
- `category` = catégorie RGPD (`tasks/payments/security/digest/product`),
  pas catégorie Flutter (`update/feature/tip/promo/plannerTask`). Le
  mapping Flutter se fait via `data_json.subtype`, ce qui permet au
  backend de rester RGPD-compliant sans coupler au front.
- `channel_used` = canal RÉELLEMENT utilisé après fallback, distinct de
  la préférence user. Un user pref=push qui a bascule en email via
  fallback a `channel_used='email'`.
- `source_task_id` FK `ON DELETE SET NULL` — si la tâche planifiée est
  purgée, la notification historique reste lisible en timeline.
- Pas de FK vers `conversations` — le lien vers une conversation IA
  (issue d'une task exécutée) est porté par `data_json.conversation_id`
  pour flexibilité (une notif peut pointer vers une conv, un paiement,
  une page sécurité…).

### `notification_preferences` — choix user par (catégorie, canal)

Composite PK `(user_id, category)` — un user × une catégorie = une row.
L'absence de row = comportement par défaut appliqué côté service
(`tasks=push`, `payments=email`, `security=email`, `digest=email`,
`product=email`). Les defaults sont **en dur dans le service Python**,
pas en SQL `DEFAULT` — changer les defaults ne nécessite pas de migration.

## Indexes

- `ix_notifications_user_active (user_id, sent_at DESC) WHERE deleted_at
  IS NULL` — la timeline `GET /notifications`.
- `ix_notifications_user_unread (user_id, sent_at DESC) WHERE read_at
  IS NULL AND deleted_at IS NULL` — le badge « N non-lus » / filtre
  `?unread_only=true`.
- `ix_notifications_category_time (category, sent_at DESC)` — futurs
  dashboards admin (« volume email digest cette semaine ? »).
- `ix_notification_prefs_user (user_id) WHERE channel != 'none'` —
  rapide lookup des catégories actives pour un user.

Rollback strict inverse (notifications → notification_preferences).
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

# revision identifiers, used by Alembic.
revision = "015_notifications"
down_revision = "014_scheduled_tasks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ═══════════════════════════════════════════════════════════════
    # TABLE : notification_preferences
    # ═══════════════════════════════════════════════════════════════
    op.create_table(
        "notification_preferences",
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
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
        sa.PrimaryKeyConstraint("user_id", "category", name="pk_notification_prefs"),
        sa.CheckConstraint(
            "category IN ('tasks','payments','security','digest','product')",
            name="ck_notification_prefs_category",
        ),
        sa.CheckConstraint(
            "channel IN ('push','email','both','none')",
            name="ck_notification_prefs_channel",
        ),
    )
    op.create_index(
        "ix_notification_prefs_user",
        "notification_preferences",
        ["user_id"],
        postgresql_where=sa.text("channel != 'none'"),
    )

    # ═══════════════════════════════════════════════════════════════
    # TABLE : notifications
    # ═══════════════════════════════════════════════════════════════
    op.create_table(
        "notifications",
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
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("data_json", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("channel_used", sa.String(16), nullable=False),
        sa.Column(
            "source_task_id",
            UUID(as_uuid=True),
            sa.ForeignKey("scheduled_tasks.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("push_message_id", sa.String(256), nullable=True),
        sa.Column("email_message_id", sa.String(256), nullable=True),
        sa.Column(
            "attempts_push",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column(
            "attempts_email",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "sent_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
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
        sa.CheckConstraint(
            "category IN ('tasks','payments','security','digest','product')",
            name="ck_notifications_category",
        ),
        sa.CheckConstraint(
            "channel_used IN ('push','email','both','skipped')",
            name="ck_notifications_channel_used",
        ),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200",
            name="ck_notifications_title_len",
        ),
        sa.CheckConstraint(
            "attempts_push >= 0 AND attempts_email >= 0",
            name="ck_notifications_attempts_non_neg",
        ),
        sa.CheckConstraint(
            "source_kind IN ('scheduled_task','payment','security','digest','product','manual')",
            name="ck_notifications_source_kind",
        ),
    )

    # Timeline active — tri décroissant sur sent_at
    op.create_index(
        "ix_notifications_user_active",
        "notifications",
        ["user_id", sa.text("sent_at DESC")],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )
    # Badge « non-lus » — filtre ?unread_only=true
    op.create_index(
        "ix_notifications_user_unread",
        "notifications",
        ["user_id", sa.text("sent_at DESC")],
        postgresql_where=sa.text("read_at IS NULL AND deleted_at IS NULL"),
    )
    # Dashboard admin / analytics par catégorie
    op.create_index(
        "ix_notifications_category_time",
        "notifications",
        ["category", sa.text("sent_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_notifications_category_time", table_name="notifications")
    op.drop_index("ix_notifications_user_unread", table_name="notifications")
    op.drop_index("ix_notifications_user_active", table_name="notifications")
    op.drop_table("notifications")
    op.drop_index("ix_notification_prefs_user", table_name="notification_preferences")
    op.drop_table("notification_preferences")
