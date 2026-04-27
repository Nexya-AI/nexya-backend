"""
Helpdesk — services CrispEscalation + Metrics admin.

`CrispEscalationService.escalate()` est le point d'entrée appelé par le
hook `core/errors/handlers.py::_maybe_escalate` quand un user Pro
rencontre un incident critique (paiement, LLM down, RGPD, security).

Pipeline strict :
1. **Filtre `should_escalate(user, category, severity)`** — V1 : Pro
   uniquement, severity high/critical, kill-switch via setting.
2. **INSERT row local `helpdesk_escalations`** avec `status='open'` et
   `crisp_conversation_id=NULL` (pré-Crisp). La trace existe même si
   Crisp est down.
3. **Appel Crisp** (`CrispClient.create_conversation`) — fail-safe
   absolu : retourne None sur erreur.
4. **UPDATE crisp_conversation_id** si succès.
5. Log forensic.

`HelpdeskMetricsService.compute(db)` agrège SQL pour le dashboard admin.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from typing import Any

import structlog
from sqlalchemy import func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.features.auth.models import User
from app.features.helpdesk.models import HelpdeskEscalation
from app.features.helpdesk.schemas import (
    CategoryBreakdown,
    EscalationCategory,
    EscalationCreate,
    EscalationSeverity,
    HelpdeskMetricsResponse,
)
from app.integrations.crisp_client import (
    CrispConversationRequest,
    get_crisp_client,
)

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# ESCALATION
# ═══════════════════════════════════════════════════════════════════


class CrispEscalationService:
    """Orchestre l'escalation Crisp + persistance locale."""

    @staticmethod
    def should_escalate(
        *,
        user: User | None,
        category: EscalationCategory,
        severity: EscalationSeverity,
    ) -> bool:
        """Décide si l'incident mérite un ticket Crisp.

        V1 stratégie : **Pro user + severity high/critical**.
        - Free user → log seulement, pas de ticket Crisp (volume trop
          gros, équipe support ne suit pas).
        - Severity low/medium → trace locale possible mais pas Crisp
          (anti-spam ticket).
        - Kill-switch global via `settings.crisp_escalation_enabled`.
        """
        if not settings.crisp_escalation_enabled:
            return False
        if user is None or not getattr(user, "is_pro", False):
            return False
        if severity not in ("high", "critical"):
            return False
        if category not in ("payment", "llm_unavailable", "data_loss", "rgpd", "security"):
            return False
        return True

    @staticmethod
    async def escalate(
        *,
        body: EscalationCreate,
        user: User | None,
        db: AsyncSession,
    ) -> HelpdeskEscalation:
        """INSERT row local → tente Crisp → UPDATE crisp_conversation_id.

        Fail-safe absolu : Crisp KO ne lève jamais. La row reste avec
        `crisp_conversation_id=NULL` et un cron retry V2 pourra rejouer.
        Le caller (handler global) reçoit la row insérée.
        """
        row = HelpdeskEscalation(
            user_id=body.user_id,
            category=body.category,
            severity=body.severity,
            payload_json=body.payload,
            status="open",
        )
        db.add(row)
        try:
            await db.flush()
        except Exception as exc:  # noqa: BLE001
            log.warning("helpdesk.escalation.insert_failed", error=str(exc))
            await db.rollback()
            return row

        # Tentative Crisp (best-effort). Si succès, persiste l'ID.
        crisp_request = _build_crisp_request(body=body, user=user)
        try:
            client = get_crisp_client()
            session_id = await client.create_conversation(crisp_request)
        except Exception as exc:  # noqa: BLE001 — fail-safe absolu
            log.warning("helpdesk.crisp.unexpected", error=str(exc))
            session_id = None

        if session_id:
            row.crisp_conversation_id = session_id
            try:
                await db.flush()
            except Exception as exc:  # noqa: BLE001
                log.warning("helpdesk.crisp_id.update_failed", error=str(exc))

        try:
            await db.commit()
        except Exception as exc:  # noqa: BLE001
            log.warning("helpdesk.commit_failed", error=str(exc))
            await db.rollback()

        log.info(
            "helpdesk.escalation.recorded",
            row_id=str(row.id) if row.id else None,
            category=body.category,
            severity=body.severity,
            crisp_conversation_id=session_id,
            user_id=str(body.user_id) if body.user_id else None,
        )
        return row


def _build_crisp_request(
    *,
    body: EscalationCreate,
    user: User | None,
) -> CrispConversationRequest:
    """Construit le payload Crisp à partir de l'escalation interne."""
    nickname = "Anonyme"
    email: str | None = None
    if user is not None:
        nickname = (
            getattr(user, "display_name", None)
            or getattr(user, "username", None)
            or getattr(user, "email", None)
            or str(getattr(user, "id", "")) or "Anonyme"
        )
        email = getattr(user, "email", None)

    payload_dict: dict[str, Any] = body.payload or {}
    summary_lines = [
        f"⚠️ Incident automatique NEXYA — {body.category} ({body.severity})",
        "",
        f"User : {nickname}" + (f" — {email}" if email else ""),
        f"User ID : {body.user_id or 'N/A'}",
        "",
        "Détails :",
    ]
    for key, value in sorted(payload_dict.items()):
        summary_lines.append(f"  • {key} : {value}")
    message = "\n".join(summary_lines)

    return CrispConversationRequest(
        nickname=nickname,
        email=email,
        message=message,
        metadata={
            "category": body.category,
            "severity": body.severity,
            "user_id": str(body.user_id) if body.user_id else None,
            **payload_dict,
        },
    )


# ═══════════════════════════════════════════════════════════════════
# METRICS — admin dashboard
# ═══════════════════════════════════════════════════════════════════


class HelpdeskMetricsService:
    """Agrégat SQL pour `GET /admin/helpdesk/metrics`.

    3 KPIs principaux pour le dashboard :
    - **Counts par status** (open, in_progress, resolved, cancelled)
    - **`median_resolved_age_hours`** : signal de réactivité équipe.
    - **`oldest_open_age_hours`** : signal de retard sur le backlog.
    + Breakdown par catégorie.
    """

    @staticmethod
    async def compute(db: AsyncSession) -> HelpdeskMetricsResponse:
        # Counts par status (toutes catégories)
        rows = await _select_counts_per_status(db)
        counts = {
            "open": 0,
            "in_progress": 0,
            "resolved": 0,
            "cancelled": 0,
        }
        for status_, n in rows:
            counts[status_] = int(n or 0)
        total = sum(counts.values())

        # Median age sur les RESOLVED (signal qualité support)
        median_h = await _select_median_resolved_age_hours(db)

        # Max age sur les OPEN (signal retard backlog)
        oldest_h = await _select_oldest_open_age_hours(db)

        # Breakdown par catégorie
        breakdown = await _select_breakdown_per_category(db)

        return HelpdeskMetricsResponse(
            open_count=counts["open"],
            in_progress_count=counts["in_progress"],
            resolved_count=counts["resolved"],
            cancelled_count=counts["cancelled"],
            total_count=total,
            median_resolved_age_hours=median_h,
            oldest_open_age_hours=oldest_h,
            breakdown_per_category=breakdown,
        )


# ─── SQL helpers ──────────────────────────────────────────────


async def _select_counts_per_status(db: AsyncSession) -> list[tuple[str, int]]:
    stmt = (
        select(HelpdeskEscalation.status, func.count())
        .where(HelpdeskEscalation.deleted_at.is_(None))
        .group_by(HelpdeskEscalation.status)
    )
    result = await db.execute(stmt)
    return list(result.all())


async def _select_median_resolved_age_hours(db: AsyncSession) -> float | None:
    """Median SQL via `percentile_cont(0.5) WITHIN GROUP (ORDER BY age)`."""
    stmt = text(
        """
        SELECT percentile_cont(0.5) WITHIN GROUP (
            ORDER BY EXTRACT(EPOCH FROM (resolved_at - created_at)) / 3600.0
        ) AS median_hours
        FROM helpdesk_escalations
        WHERE status = 'resolved'
          AND resolved_at IS NOT NULL
          AND deleted_at IS NULL
        """
    )
    result = await db.execute(stmt)
    val = result.scalar_one_or_none()
    return float(val) if val is not None else None


async def _select_oldest_open_age_hours(db: AsyncSession) -> float | None:
    """Max age sur les open (NOW - created_at, en heures)."""
    stmt = text(
        """
        SELECT MAX(EXTRACT(EPOCH FROM (NOW() - created_at)) / 3600.0)
        FROM helpdesk_escalations
        WHERE status = 'open'
          AND deleted_at IS NULL
        """
    )
    result = await db.execute(stmt)
    val = result.scalar_one_or_none()
    return float(val) if val is not None else None


async def _select_breakdown_per_category(db: AsyncSession) -> list[CategoryBreakdown]:
    stmt = (
        select(HelpdeskEscalation.category, HelpdeskEscalation.status, func.count())
        .where(HelpdeskEscalation.deleted_at.is_(None))
        .group_by(HelpdeskEscalation.category, HelpdeskEscalation.status)
    )
    result = await db.execute(stmt)
    rows = list(result.all())
    by_cat: dict[str, dict[str, int]] = defaultdict(
        lambda: {"open": 0, "in_progress": 0, "resolved": 0, "cancelled": 0}
    )
    for cat, status_, n in rows:
        if cat is None:
            continue
        by_cat[cat][status_] = int(n or 0)
    return [
        CategoryBreakdown(
            category=cat,  # type: ignore[arg-type]
            open_count=stats["open"],
            in_progress_count=stats["in_progress"],
            resolved_count=stats["resolved"],
            cancelled_count=stats["cancelled"],
        )
        for cat, stats in sorted(by_cat.items())
    ]
