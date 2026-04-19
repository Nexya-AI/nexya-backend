"""
Base ORM SQLAlchemy — toutes les tables NEXYA héritent de Base.

UUIDMixin fournit id + created_at + updated_at à chaque modèle
pour éviter de réécrire ces colonnes 15 fois.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Classe de base pour tous les modèles ORM NEXYA.

    Hérite de DeclarativeBase (SQLAlchemy 2.0 style).
    Chaque modèle qui en hérite sera détecté automatiquement par Alembic.
    """


class UUIDMixin:
    """Mixin fournissant id UUID + timestamps à toutes les tables.

    Usage :
        class User(Base, UUIDMixin):
            __tablename__ = "users"
            email: Mapped[str] = mapped_column(unique=True)
    """

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
