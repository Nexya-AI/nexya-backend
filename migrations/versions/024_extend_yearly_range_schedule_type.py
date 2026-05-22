"""Extend ck_tasks_schedule_type CHECK with the F1.6 `yearly_range` type.

Revision ID: 024_yearly_range
Revises: 023_extend_range_schedules
Create Date: 2026-05-21

F1.6 — Range schedules Planner, extension `yearly_range`. Décidée juste
après F1.5 pour répondre à un besoin produit : pouvoir choisir un mois
précis (« chaque année, en janvier, du 15 au 30 ») là où `monthly_range`
se répète indistinctement tous les mois.

Le schema Pydantic `YearlyRangeConfig` a été ajouté dans
`app/features/planner/schemas.py` et le helper `compute_next_run`
(`scheduler.py`) sait calculer son `next_run_at` (clamp 29 février /
fin de mois via `calendar.monthrange()`). Cette migration met le CHECK
constraint SQL `ck_tasks_schedule_type` en cohérence avec le
discriminator Pydantic `ScheduleType` (10 valeurs après F1.6).

On repart des 9 types posés par la migration 023 (F1.5) et on ajoute
`yearly_range` — le `downgrade()` y revient proprement.
"""

from __future__ import annotations

from alembic import op

# revision identifiers, used by Alembic.
revision = "024_yearly_range"
down_revision = "023_extend_range_schedules"
branch_labels = None
depends_on = None

# Les 10 types après F1.6 (alignés sur le Literal `ScheduleType`).
_ALL_TYPES = (
    "'once','interval_minutes','daily','weekly','monthly','yearly',"
    "'weekly_range','monthly_range','multi_weekday','yearly_range'"
)
# Les 9 types pré-F1.6 (état posé par la migration 023).
_PRE_F16_TYPES = (
    "'once','interval_minutes','daily','weekly','monthly','yearly',"
    "'weekly_range','monthly_range','multi_weekday'"
)


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
        f"CHECK (schedule_type IN ({_PRE_F16_TYPES}))"
    )
