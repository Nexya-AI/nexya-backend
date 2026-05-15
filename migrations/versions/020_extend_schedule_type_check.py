"""Extend ck_tasks_schedule_type CHECK constraint with monthly + yearly.

Revision ID: 020_extend_schedule_check
Revises: 019_helpdesk
Create Date: 2026-05-13

Bug-008 fix — F0.5 (commit `ebbde1c`, 2026-05-04) a ajouté les schemas
Pydantic `MonthlyConfig` + `YearlyConfig` côté Python mais a OUBLIÉ de
mettre à jour le CHECK constraint SQL `ck_tasks_schedule_type` posé par
la migration 014.

Conséquence : tout INSERT avec `schedule_type IN ('monthly', 'yearly')`
échoue avec `psycopg.errors.CheckViolation` → 500 IntegrityError côté
API, sheet de création Planner reste ouverte côté Flutter, seules
`once` + `interval_minutes` + `daily` + `weekly` (les 4 valeurs
historiques du check) peuvent être créées.

Fix : DROP + ADD du CHECK avec les 6 valeurs alignées sur le
discriminator Pydantic `ScheduleType` Literal défini dans
`app/features/planner/schemas.py`.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "020_extend_schedule_check"
down_revision = "019_helpdesk"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "ALTER TABLE scheduled_tasks DROP CONSTRAINT IF EXISTS ck_tasks_schedule_type"
    )
    op.execute(
        "ALTER TABLE scheduled_tasks ADD CONSTRAINT ck_tasks_schedule_type "
        "CHECK (schedule_type IN ('once','interval_minutes','daily','weekly','monthly','yearly'))"
    )


def downgrade() -> None:
    op.execute(
        "ALTER TABLE scheduled_tasks DROP CONSTRAINT IF EXISTS ck_tasks_schedule_type"
    )
    op.execute(
        "ALTER TABLE scheduled_tasks ADD CONSTRAINT ck_tasks_schedule_type "
        "CHECK (schedule_type IN ('once','interval_minutes','daily','weekly'))"
    )
