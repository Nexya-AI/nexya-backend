"""
Dépendances FastAPI partagées — injectées via Depends() dans les endpoints.

get_db et get_current_user sont définis dans leurs modules respectifs
(core/database/postgres.py et core/auth/guards.py) et réexportés ici
quand ils seront implémentés.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import Query

from app.config import settings


@dataclass(frozen=True, slots=True)
class PaginationParams:
    """Paramètres de pagination validés et bornés."""

    page: int
    limit: int
    offset: int


def get_pagination(
    page: int = Query(default=1, ge=1, description="Numéro de page (commence à 1)"),
    limit: int = Query(default=20, ge=1, le=50, description="Nombre d'items par page (max 50)"),
) -> PaginationParams:
    """Dépendance FastAPI pour la pagination.

    Borne `limit` au maximum configuré (50 par défaut).
    Calcule l'offset SQL automatiquement.
    """
    clamped_limit = min(limit, settings.pagination_max_limit)
    offset = (page - 1) * clamped_limit
    return PaginationParams(page=page, limit=clamped_limit, offset=offset)
