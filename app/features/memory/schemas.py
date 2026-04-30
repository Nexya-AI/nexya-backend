"""
Schémas Pydantic — endpoints publics Mémoire IA (D5).

Contrat API exposé aux clients Flutter :
- `POST /memory/index` : `MemoryCreate` → `MemoryResponse` (201).
- `POST /memory/search` : `MemorySearchRequest` → `MemorySearchResponse`.
- `GET /memory` : paginé keyset → `MemoryListResponse`.
- `DELETE /memory/{id}` : 204 (pas de body).

Discipline :
- `source` est un `Literal[...]` 1:1 aligné sur le CHECK SQL D1.
- Validators strippent le content avant validation de longueur.
- `MemoryResponse` expose `content` et métadonnées — pas le vecteur
  embedding brut (1536 floats = bruit pour le client + fuite de
  l'empreinte sémantique vers le frontend).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config import settings

# ══════════════════════════════════════════════════════════════
# Types partagés
# ══════════════════════════════════════════════════════════════

MemorySource = Literal["manual", "extracted", "imported", "system"]


# ══════════════════════════════════════════════════════════════
# Requêtes
# ══════════════════════════════════════════════════════════════


class MemoryCreate(BaseModel):
    """Body de `POST /memory/index`.

    `content` : fait durable en clair, strippé côté validator, 1-2000
    chars (cap aligné `embeddings_content_max_chars`). `importance` ∈
    [0, 10], défaut 1 (même pondération qu'une mémoire extraite auto).
    `metadata_json` : dict libre pour extensions futures (tags,
    confidence score, etc.) — non obligatoire.
    """

    content: str = Field(min_length=1, max_length=2000)
    importance: int = Field(default=1, ge=0, le=10)
    metadata_json: dict | None = None

    @field_validator("content", mode="before")
    @classmethod
    def _strip_content(cls, v):
        return v.strip() if isinstance(v, str) else v

    @field_validator("content", mode="after")
    @classmethod
    def _enforce_max_chars_setting(cls, v: str) -> str:
        # Double garde-fou avec le setting qui pilote aussi D1 `add`.
        max_chars = settings.embeddings_content_max_chars
        if len(v) > max_chars:
            raise ValueError(f"Le contenu dépasse {max_chars} caractères.")
        return v


class MemorySearchRequest(BaseModel):
    """Body de `POST /memory/search`.

    `query` : texte libre 1-500 chars. `k` ∈ [1, 50] (aligné cap
    `_MAX_SEARCH_K` du service D1). `min_similarity` plancher dans
    [0, 1] — défaut 0.7 comme D3 (filtre les mémoires tangentielles).
    `source` : filtre optionnel pour ne rechercher que dans « ce que
    J'AI ajouté » (manual) vs « ce que l'IA a extrait » (extracted).
    """

    query: str = Field(min_length=1, max_length=500)
    k: int = Field(default=5, ge=1, le=50)
    min_similarity: float = Field(default=0.7, ge=0.0, le=1.0)
    source: MemorySource | None = None

    @field_validator("query", mode="before")
    @classmethod
    def _strip_query(cls, v):
        return v.strip() if isinstance(v, str) else v


# ══════════════════════════════════════════════════════════════
# Réponses
# ══════════════════════════════════════════════════════════════


class MemoryResponse(BaseModel):
    """Mémoire sérialisée pour le client.

    Expose `content` et métadonnées utiles. N'expose PAS le vecteur
    `embedding` (1536 floats, bruit et fuite d'empreinte sémantique).
    N'expose PAS `content_sha256` (interne à la dédup).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    content: str
    source: MemorySource
    importance: int
    source_conversation_id: uuid.UUID | None = None
    source_message_id: uuid.UUID | None = None
    metadata_json: dict | None = None
    created_at: datetime
    updated_at: datetime


class MemorySearchItem(BaseModel):
    """Un hit de recherche : la mémoire + son score cosinus."""

    memory: MemoryResponse
    similarity: float = Field(ge=-1.0, le=1.0)


class MemorySearchResponse(BaseModel):
    items: list[MemorySearchItem]


class MemoryListResponse(BaseModel):
    """Page keyset. `next_cursor=None` signifie la dernière page."""

    items: list[MemoryResponse]
    next_cursor: str | None = None
