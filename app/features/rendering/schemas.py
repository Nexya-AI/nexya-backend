"""Schémas Pydantic pour `POST /render/mermaid` (Session C4.3)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class MermaidRenderRequest(BaseModel):
    """Requête de rendu d'un diagramme Mermaid.

    Cap source à 10 000 chars (déjà très généreux — un diagramme dense
    fait ~2-3 KB max). Au-delà, Kroki.io rejette ou rame.
    """

    source: str = Field(
        ...,
        min_length=1,
        max_length=10_000,
        description=(
            "Source Mermaid (ex: `graph TD; A-->B; B-->C;`). "
            "Cap 10k chars. Tout langage Mermaid supporté : flowchart, "
            "sequence, gantt, class, ER, state, mindmap, pie, git, "
            "user-journey."
        ),
    )


class MermaidRenderResponse(BaseModel):
    """Réponse de rendu — SVG inline + métadonnées cache."""

    svg: str = Field(
        ...,
        description=(
            "SVG complet rendu par Kroki.io. Typiquement < 50 KB. "
            "Le frontend l'injecte directement dans `flutter_svg`."
        ),
    )
    sha256: str = Field(
        ...,
        min_length=64,
        max_length=64,
        description=(
            "Hash de la source Mermaid (clé Redis cache). Utile au "
            "frontend pour mémoiser localement et éviter de re-fetch "
            "le même diagramme."
        ),
    )
    fetched_at: datetime = Field(
        ...,
        description="Timestamp UTC du fetch backend (cache debugging).",
    )
    from_cache: bool = Field(
        False,
        description=(
            "True si le SVG vient du cache Redis (TTL 7j). Le frontend "
            "peut afficher un indicateur subtil 'depuis cache' (V2)."
        ),
    )
