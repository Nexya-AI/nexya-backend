"""
NEXYA Couche IA — Modèles ORM pour la persistance des appels LLM.

Deux tables (migration 004) :

- `AiCall` : détail forensic d'un appel LLM terminé (un par stream).
- `UsageDaily` : agrégat par `(user_id, date_utc)`, UPSERT atomique.

Insertion orchestrée par `CostTracker.record_ai_call()` — fire-and-forget
depuis le StreamHandler en fin de SSE.

Ces modèles sont lus par `migrations/env.py` via `Base.metadata` pour
qu'Alembic les détecte (même si la migration 004 a été écrite à la main).
"""

from __future__ import annotations

import uuid
from datetime import date as _date
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    PrimaryKeyConstraint,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database.base import Base


class AiCall(Base):
    """Une ligne par appel LLM terminé (completed, cancelled, failed)."""

    __tablename__ = "ai_calls"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expert_id: Mapped[str] = mapped_column(String(32), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    completion_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    total_tokens: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    outcome: Mapped[str] = mapped_column(String(16), nullable=False)
    failure_code: Mapped[str | None] = mapped_column(String(32), nullable=True)
    first_chunk_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    total_duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    fallback_used: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    extra: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # Session J1 — AI Act EU Article 13 (registre des traitements)
    legal_basis: Mapped[str | None] = mapped_column(String(32), nullable=True)
    data_categories: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retention_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("session_id", name="uq_ai_calls_session_id"),
        CheckConstraint(
            "outcome IN ('completed', 'cancelled', 'failed')",
            name="ck_ai_calls_outcome",
        ),
        CheckConstraint(
            "prompt_tokens >= 0 AND completion_tokens >= 0 AND total_tokens >= 0",
            name="ck_ai_calls_tokens_non_negative",
        ),
        CheckConstraint("cost_usd >= 0", name="ck_ai_calls_cost_non_negative"),
        CheckConstraint(
            "legal_basis IS NULL OR legal_basis IN "
            "('contract','legitimate_interest','consent','legal_obligation')",
            name="ck_ai_calls_legal_basis",
        ),
        CheckConstraint(
            "data_categories IS NULL OR data_categories IN "
            "('user_input','prompt_history','file_content','voice_audio',"
            "'image_content','profile_data')",
            name="ck_ai_calls_data_categories",
        ),
        Index("idx_ai_calls_user_time", "user_id", "created_at"),
        Index("idx_ai_calls_outcome", "outcome", "created_at"),
        Index("idx_ai_calls_provider_model", "provider", "model", "created_at"),
        Index("ix_ai_calls_legal_basis_time", "legal_basis", "created_at"),
    )


class UsageDaily(Base):
    """Agrégat journalier par user — PK composite `(user_id, date_utc)`.

    UPSERT atomique via `ON CONFLICT (user_id, date_utc) DO UPDATE` dans
    le CostTracker. user_id nullable (bucket anonyme post-RGPD).
    """

    __tablename__ = "usage_daily"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    date_utc: Mapped[_date] = mapped_column(Date, nullable=False)
    chat_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    chat_tokens_in: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    chat_tokens_out: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    image_calls: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0"), server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )

    __table_args__ = (
        PrimaryKeyConstraint("user_id", "date_utc", name="pk_usage_daily"),
        CheckConstraint(
            "chat_calls >= 0 AND chat_tokens_in >= 0 AND chat_tokens_out >= 0 "
            "AND image_calls >= 0 AND cost_usd >= 0",
            name="ck_usage_daily_counters_non_negative",
        ),
        Index("idx_usage_daily_date", "date_utc"),
    )
