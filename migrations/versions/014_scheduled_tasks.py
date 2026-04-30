"""Create `scheduled_tasks` + `scheduled_task_results` — Session F1 Planner.

Revision ID: 014_scheduled_tasks
Revises: 013_vision_analyses
Create Date: 2026-04-24

Session F1 — Planner Scheduler. L'utilisateur planifie des prompts IA
(« tous les matins à 7h, résume-moi l'actu ») qui s'exécutent
automatiquement via des workers arq.

2 tables :

- `scheduled_tasks` : la tâche + son schedule (once/interval/daily/weekly)
  + statut machine + config retry. Indexée fortement sur `next_run_at`
  pour permettre au cron `dispatch_due_tasks` de scan rapidement les
  tâches dues via `SELECT ... FOR UPDATE SKIP LOCKED`.

- `scheduled_task_results` : historique des exécutions (résultat texte,
  tokens, coût, durée, erreur). Purgé après 30 jours via cron
  `cleanup_old_task_results`. ON DELETE CASCADE depuis `scheduled_tasks`.

Indexes critiques :
- `ix_tasks_next_run_due` partiel WHERE active + NOT paused + next_run_at
  IS NOT NULL + status NOT IN (running, completed) — c'est L'index que
  le dispatcher consomme chaque minute.
- `ix_task_results_task (task_id, ran_at)` pour `GET /tasks/{id}/results`.

Stratégie scheduling F1 : UTC partout. Timezone user-spécifique =
session future (nécessite colonne timezone sur `users`).

Hors scope F1 (sessions futures) :
- Expression cron full `0 9 * * MON` → nécessite dep `croniter`.
- Notifications FCM post-exécution → session F2.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "014_scheduled_tasks"
down_revision = "013_vision_analyses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ── Table scheduled_tasks ──────────────────────────────────
    op.create_table(
        "scheduled_tasks",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column(
            "expert_id",
            sa.String(32),
            nullable=False,
            server_default="general",
        ),
        sa.Column("schedule_type", sa.String(32), nullable=False),
        sa.Column("schedule_config", JSONB(), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False, server_default="UTC"),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="idle"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default="true"),
        sa.Column("paused", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "auto_delete_after_run",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
        sa.Column(
            "retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "max_retries",
            sa.Integer(),
            nullable=False,
            server_default="2",
        ),
        sa.Column(
            "run_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
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
        sa.CheckConstraint(
            "schedule_type IN ('once','interval_minutes','daily','weekly')",
            name="ck_tasks_schedule_type",
        ),
        sa.CheckConstraint(
            "status IN ('idle','pending','running','completed','failed','paused')",
            name="ck_tasks_status",
        ),
        sa.CheckConstraint(
            "retry_count >= 0 AND max_retries >= 0",
            name="ck_tasks_retries_non_neg",
        ),
        sa.CheckConstraint("run_count >= 0", name="ck_tasks_run_count_non_neg"),
        sa.CheckConstraint(
            "char_length(title) BETWEEN 1 AND 200",
            name="ck_tasks_title_length",
        ),
    )

    # Index utilisateur actif — pour listings UI.
    op.create_index(
        "ix_tasks_user_active",
        "scheduled_tasks",
        ["user_id", "created_at"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # Index critique dispatcher — scan des tâches dues chaque minute.
    op.create_index(
        "ix_tasks_next_run_due",
        "scheduled_tasks",
        ["next_run_at"],
        postgresql_where=sa.text(
            "deleted_at IS NULL "
            "AND active = true "
            "AND paused = false "
            "AND next_run_at IS NOT NULL "
            "AND status NOT IN ('running','completed')"
        ),
    )

    # ── Table scheduled_task_results ───────────────────────────
    op.create_table(
        "scheduled_task_results",
        sa.Column(
            "id",
            sa.BigInteger(),
            autoincrement=True,
            primary_key=True,
            nullable=False,
        ),
        sa.Column("task_id", UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "ran_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "duration_ms",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("result_text", sa.Text(), nullable=True),
        sa.Column("error_text", sa.Text(), nullable=True),
        sa.Column(
            "tokens_input",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "tokens_output",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "cost_usd",
            sa.Numeric(10, 6),
            nullable=False,
            server_default="0",
        ),
        sa.Column("model", sa.String(64), nullable=True),
        sa.Column("provider", sa.String(16), nullable=True),
        sa.Column("metadata_json", JSONB(), nullable=True),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["scheduled_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.CheckConstraint(
            "status IN ('success','failed','skipped')",
            name="ck_task_results_status",
        ),
        sa.CheckConstraint("duration_ms >= 0", name="ck_task_results_duration"),
        sa.CheckConstraint("cost_usd >= 0", name="ck_task_results_cost_non_neg"),
    )
    op.create_index(
        "ix_task_results_task",
        "scheduled_task_results",
        ["task_id", "ran_at"],
    )
    op.create_index(
        "ix_task_results_user_ran",
        "scheduled_task_results",
        ["user_id", "ran_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_task_results_user_ran", table_name="scheduled_task_results")
    op.drop_index("ix_task_results_task", table_name="scheduled_task_results")
    op.drop_table("scheduled_task_results")
    op.drop_index("ix_tasks_next_run_due", table_name="scheduled_tasks")
    op.drop_index("ix_tasks_user_active", table_name="scheduled_tasks")
    op.drop_table("scheduled_tasks")
