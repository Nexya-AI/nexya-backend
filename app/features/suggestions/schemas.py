"""Schémas Pydantic — Suggestions user → équipe NEXYA (Session N1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SuggestionType = Literal["bug", "feature", "expert_domain", "other"]
SuggestionStatus = Literal["open", "in_review", "resolved", "wontfix"]


class SuggestionCreate(BaseModel):
    """Body POST /suggestions."""

    suggestion_type: SuggestionType
    body: str = Field(min_length=1, max_length=2000)


class SuggestionResponse(BaseModel):
    """Réponse 201 — confirmation que la suggestion est enregistrée."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    suggestion_type: SuggestionType
    body: str
    processing_status: SuggestionStatus
    created_at: datetime
