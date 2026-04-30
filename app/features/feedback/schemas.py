"""Schémas Pydantic — Feedback chat (Session N1)."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

FeedbackRating = Literal["like", "dislike"]


class FeedbackCreate(BaseModel):
    """Body POST /chat/messages/{message_id}/feedback."""

    rating: FeedbackRating
    comment: str | None = Field(default=None, max_length=1000)


class FeedbackResponse(BaseModel):
    """Réponse 201 — l'état courant du feedback après UPSERT."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    message_id: uuid.UUID
    rating: FeedbackRating
    comment: str | None
    created_at: datetime
    updated_at: datetime
