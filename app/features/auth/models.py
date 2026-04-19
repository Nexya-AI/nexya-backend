"""
Modèles ORM Auth — User, RefreshToken, DeviceToken.

Ces 3 tables sont le socle de toute authentification NEXYA.
Schéma SQL de référence : BACKEND_IA_NEXYA.md section 3.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database.base import Base, UUIDMixin


class User(Base, UUIDMixin):
    """Utilisateur NEXYA.

    Champs alignés sur le schéma SQL de BACKEND_IA_NEXYA.md.
    - password_hash : bcrypt, jamais le mot de passe en clair
    - plan : 'free' ou 'pro' — détermine les quotas et features accessibles
    - deleted_at : soft delete RGPD — l'utilisateur est anonymisé, pas supprimé
    """

    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False, index=True)
    username: Mapped[str | None] = mapped_column(String(50), unique=True, nullable=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(100))
    avatar_url: Mapped[str | None] = mapped_column(String(500))
    bio: Mapped[str | None] = mapped_column(Text)
    locale: Mapped[str] = mapped_column(String(10), server_default="fr", default="fr")
    timezone: Mapped[str] = mapped_column(
        String(50), server_default="Africa/Douala", default="Africa/Douala"
    )
    plan: Mapped[str] = mapped_column(String(20), server_default="free", default="free")
    plan_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    voice_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, server_default="false", default=False)
    mfa_secret: Mapped[str | None] = mapped_column(String(255))
    data_collection_enabled: Mapped[bool] = mapped_column(
        Boolean, server_default="true", default=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", default=True)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Relations ──────────────────────────────────────────────
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )
    device_tokens: Mapped[list[DeviceToken]] = relationship(
        back_populates="user", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_pro(self) -> bool:
        """L'utilisateur a-t-il un plan Pro actif (non expiré) ?"""
        if self.plan != "pro":
            return False
        if self.plan_expires_at is None:
            return True
        return self.plan_expires_at > datetime.now(timezone.utc)


class RefreshToken(Base):
    """Token de rafraîchissement — stocké hashé (SHA-256), jamais en clair.

    Rotation : chaque usage invalide l'ancien token et en crée un nouveau.
    revoked_at != None → token invalidé (logout ou rotation).
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
        server_default=func.gen_random_uuid(),
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.now(),
        nullable=False,
    )

    # ── Relation ───────────────────────────────────────────────
    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    __table_args__ = (
        Index("idx_refresh_tokens_user", "user_id", "revoked_at"),
    )


class DeviceToken(Base, UUIDMixin):
    """Token FCM pour les notifications push Flutter.

    Un utilisateur peut avoir plusieurs appareils (téléphone + tablette).
    is_active=False → token désactivé (déconnexion d'un appareil).
    """

    __tablename__ = "device_tokens"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    token: Mapped[str] = mapped_column(String(500), unique=True, nullable=False)
    platform: Mapped[str] = mapped_column(String(10), nullable=False)  # 'android' | 'ios'
    is_active: Mapped[bool] = mapped_column(Boolean, server_default="true", default=True)

    # ── Relation ───────────────────────────────────────────────
    user: Mapped[User] = relationship(back_populates="device_tokens")

    __table_args__ = (
        Index("idx_device_tokens_user", "user_id", "is_active"),
    )
