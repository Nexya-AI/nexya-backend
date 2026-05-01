"""
Schémas de réponse partagés — utilisés par TOUS les endpoints NEXYA.

Règle absolue : ne jamais retourner un dict brut.
Toujours NexyaResponse[T] ou PaginatedResponse[T].
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class NexyaResponse(BaseModel, Generic[T]):
    """Enveloppe standard pour toutes les réponses API.

    Succès : NexyaResponse(success=True, data=result)
    Erreur : NexyaResponse(success=False, error="Message", code="ERROR_CODE")
    """

    success: bool
    data: T | None = None
    error: str | None = None
    code: str | None = None
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))


class PaginatedResponse(BaseModel, Generic[T]):
    """Réponse paginée pour les listes (historique, projets, bibliothèque...).

    Le frontend lit `has_next` pour décider s'il charge la page suivante.
    `total` permet d'afficher "X résultats" dans l'UI.
    """

    success: bool = True
    items: list[T]
    total: int
    page: int
    limit: int
    has_next: bool
