"""
Schémas Pydantic du corpus Experts (G1).

Actuellement rien d'exposé en HTTP — le corpus est un socle interne
consommé par `build_expert_corpus_context` et le script d'ingestion.
Les schémas ici servent uniquement à documenter la shape des
dictionnaires retournés par `ExpertCorpusService` et à préparer
l'éventuel endpoint admin futur `GET /admin/experts/corpus`.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ExpertChunkItem(BaseModel):
    """Représentation publique d'un chunk corpus retourné par un search."""

    id: int
    content: str
    source: str
    language_pair: str | None = None
    similarity: float = Field(ge=0.0, le=1.0)
    metadata: dict[str, Any] = Field(default_factory=dict)
