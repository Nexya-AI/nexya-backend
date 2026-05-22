"""Extend ck_tasks_schedule_type CHECK with the 3 F1.5 range schedule types.

Revision ID: 023_extend_range_schedules
Revises: 022_legacy_updated_at
Create Date: 2026-05-21

F1.5 — Range schedules Planner. La session ajoute 3 nouveaux
`schedule_type` au Planner :

- `weekly_range`   — range continu de jours de semaine (lundi → vendredi).
- `monthly_range`  — range continu de jours du mois (du 15 au 30).
- `multi_weekday`  — liste de jours non-continus (mardi + jeudi).

Les schemas Pydantic `WeeklyRangeConfig` / `MonthlyRangeConfig` /
`MultiWeekdayConfig` ont été ajoutés dans `app/features/planner/schemas.py`
et le helper `compute_next_run` (`scheduler.py`) sait désormais calculer
leur `next_run_at`. Cette migration met le CHECK constraint SQL
`ck_tasks_schedule_type` en cohérence avec le discriminator Pydantic
`ScheduleType`.

Note : la migration `020_extend_schedule_check` n'avait élargi le CHECK
qu'aux 6 types F1 + F0.5 (`once`, `interval_minutes`, `daily`, `weekly`,
`monthly`, `yearly`). On repart de ces 6 valeurs et on ajoute les 3
nouvelles — le `downgrade()` y revient proprement.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "023_extend_range_schedules"
down_revision = "022_legacy_updated_at"
branch_labels = None
depends_on = None

# Les 9 types après F1.5 (alignés sur le Literal `ScheduleType`).
_ALL_TYPES = (
    "'once','interval_minutes','daily','weekly','monthly','yearly',"
    "'weekly_range','monthly_range','multi_weekday'"
)
# Les 6 types pré-F1.5 (état posé par la migration 020).
_PRE_F15_TYPES = "'once','interval_minutes','daily','weekly','monthly','yearly'"


def upgrade() -> None:
    op.execute("ALTER TABLE scheduled_tasks DROP CONSTRAINT IF EXISTS ck_tasks_schedule_type")
    op.execute(
        "ALTER TABLE scheduled_tasks ADD CONSTRAINT ck_tasks_schedule_type "
        f"CHECK (schedule_type IN ({_ALL_TYPES}))"
    )


def downgrade() -> None:
    op.execute("ALTER TABLE scheduled_tasks DROP CONSTRAINT IF EXISTS ck_tasks_schedule_type")
    op.execute(
        "ALTER TABLE scheduled_tasks ADD CONSTRAINT ck_tasks_schedule_type "
        f"CHECK (schedule_type IN ({_PRE_F15_TYPES}))"
    )
