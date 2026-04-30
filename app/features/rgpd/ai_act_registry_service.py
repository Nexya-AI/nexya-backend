"""AIActRegistryService — registre des traitements IA.

Session J1 — Conformité AI Act EU 2024/1689 Article 13 (transparence
et information sur les systèmes IA utilisés). Applicable août 2026.

L'AI Act exige des fournisseurs de systèmes IA qu'ils maintiennent un
registre des traitements automatisés détaillant :
- Quel modèle a été utilisé (provider + model).
- À quelle finalité (expert_id ↔ domaine fonctionnel).
- Sur quelle base légale (consent / contract / legitimate_interest /
  legal_obligation).
- Sur quelles catégories de données (user_input, file_content...).
- Combien de temps conservées (retention_until).

NEXYA stocke ces informations directement sur `ai_calls` (enrichi en
J1 avec 3 colonnes), évitant une table dédiée et garantissant la
cohérence avec l'observabilité.

Export CSV (UTF-8 BOM Excel-friendly) ou JSON pour audit DPO/CNIL.
"""

from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.models import AiCall
from app.features.rgpd.schemas import AIActRegistryFilters

log = structlog.get_logger(__name__)


# Ordre des colonnes du CSV — figé pour stabilité audit.
_CSV_COLUMNS = [
    "created_at",
    "user_id",
    "expert_id",
    "provider",
    "model",
    "legal_basis",
    "data_categories",
    "retention_until",
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "cost_usd",
    "outcome",
    "failure_code",
]


def _row_to_dict(row: AiCall) -> dict[str, Any]:
    """Sérialise une AiCall en dict prêt à exporter (sans `extra`/prompt)."""
    return {
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "user_id": str(row.user_id) if row.user_id else None,
        "expert_id": row.expert_id,
        "provider": row.provider,
        "model": row.model,
        "legal_basis": row.legal_basis,
        "data_categories": row.data_categories,
        "retention_until": (row.retention_until.isoformat() if row.retention_until else None),
        "prompt_tokens": row.prompt_tokens,
        "completion_tokens": row.completion_tokens,
        "total_tokens": row.total_tokens,
        "cost_usd": str(row.cost_usd) if row.cost_usd is not None else "0",
        "outcome": row.outcome,
        "failure_code": row.failure_code,
    }


class AIActRegistryService:
    """Export du registre AI Act — CSV ou JSON."""

    @staticmethod
    async def fetch_rows(filters: AIActRegistryFilters, db: AsyncSession) -> list[AiCall]:
        """Charge les rows ai_calls dans la fenêtre temporelle demandée."""
        stmt = select(AiCall)
        if filters.date_from is not None:
            stmt = stmt.where(AiCall.created_at >= filters.date_from)
        if filters.date_to is not None:
            stmt = stmt.where(AiCall.created_at <= filters.date_to)
        stmt = stmt.order_by(AiCall.created_at.desc()).limit(filters.limit)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    @staticmethod
    def export_csv(rows: list[AiCall]) -> bytes:
        """Sérialise les rows en CSV UTF-8 BOM (Excel-friendly).

        Le BOM `﻿` placé en début de fichier indique à Excel que
        l'encoding est UTF-8 (sinon il interprète en cp1252 sur Windows
        et casse les accents FR).
        """
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for row in rows:
            d = _row_to_dict(row)
            # csv.DictWriter ignore les clés non listées dans fieldnames
            writer.writerow(d)
        text = buffer.getvalue()
        # BOM UTF-8 prepended
        return ("﻿" + text).encode("utf-8")

    @staticmethod
    def export_json(rows: list[AiCall]) -> bytes:
        """Sérialise en JSON UTF-8 (programmatic consumer)."""
        items = [_row_to_dict(row) for row in rows]
        payload = {
            "exported_at": datetime.utcnow().isoformat(),
            "row_count": len(items),
            "items": items,
        }
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
