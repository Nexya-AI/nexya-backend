"""Schémas Pydantic — Helpdesk escalation + metrics admin."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# ═══════════════════════════════════════════════════════════════════
# LITERAL TYPES — alignés CHECK SQL migration 019_helpdesk
# ═══════════════════════════════════════════════════════════════════

EscalationCategory = Literal[
    "payment",
    "llm_unavailable",
    "data_loss",
    "rgpd",
    "security",
]

EscalationSeverity = Literal["low", "medium", "high", "critical"]

EscalationStatus = Literal["open", "in_progress", "resolved", "cancelled"]


# ═══════════════════════════════════════════════════════════════════
# CREATE / RESPONSE
# ═══════════════════════════════════════════════════════════════════


class EscalationCreate(BaseModel):
    """Requête de création d'escalation (côté service interne).

    Construit par le hook `core/errors/handlers.py::_maybe_escalate`
    quand un user Pro rencontre un incident critique. Pas exposé en
    HTTP direct — V2 si besoin d'un endpoint manuel `POST /escalate`.
    """

    user_id: uuid.UUID | None = Field(default=None)
    category: EscalationCategory
    severity: EscalationSeverity = "high"
    payload: dict[str, Any] | None = Field(default=None, max_length=10_000)


class EscalationResponse(BaseModel):
    """Réponse minimale (utilisée par les tests, pas par un endpoint live)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID | None
    category: EscalationCategory
    severity: EscalationSeverity
    crisp_conversation_id: str | None
    status: EscalationStatus
    created_at: datetime


# ═══════════════════════════════════════════════════════════════════
# METRICS ADMIN — GET /admin/helpdesk/metrics
# ═══════════════════════════════════════════════════════════════════


class CategoryBreakdown(BaseModel):
    """Décompose les escalations par catégorie."""

    category: EscalationCategory
    open_count: int
    in_progress_count: int
    resolved_count: int
    cancelled_count: int


class HelpdeskMetricsResponse(BaseModel):
    """Agrégat global pour le dashboard admin support.

    `median_age_hours` est calculé sur les escalations RÉSOLUES
    (`resolved_at - created_at`) — un signal de réactivité de l'équipe.
    Sur les `open` non encore résolues, on regarde plutôt
    `oldest_open_age_hours` (max age) — un signal de retard.
    """

    open_count: int
    in_progress_count: int
    resolved_count: int
    cancelled_count: int
    total_count: int
    median_resolved_age_hours: float | None
    oldest_open_age_hours: float | None
    breakdown_per_category: list[CategoryBreakdown]
