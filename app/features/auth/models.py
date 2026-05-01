"""
Modèles ORM Auth — User, RefreshToken, DeviceToken.

Ces 3 tables sont le socle de toute authentification NEXYA.
Schéma SQL de référence : BACKEND_IA_NEXYA.md section 3.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
        return self.plan_expires_at > datetime.now(UTC)


class RefreshToken(Base):
    """Token de rafraîchissement — stocké hashé (SHA-256), jamais en clair.

    Rotation : chaque usage invalide l'ancien token et en crée un nouveau.
    revoked_at != None → token invalidé (logout ou rotation).
    """

    __tablename__ = "refresh_tokens"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
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
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )

    # ── Relation ───────────────────────────────────────────────
    user: Mapped[User] = relationship(back_populates="refresh_tokens")

    __table_args__ = (Index("idx_refresh_tokens_user", "user_id", "revoked_at"),)


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

    __table_args__ = (Index("idx_device_tokens_user", "user_id", "is_active"),)


class DeviceQuota(Base):
    """Quota journalier d'inscriptions pour un device donné.

    Une ligne par `(device_id, day)` — UPSERT atomique à chaque tentative
    d'inscription. Au-delà de `settings.device_registration_daily_limit`
    inscriptions réussies dans la même journée (UTC), on bloque.

    Pourquoi une table DB plutôt que Redis comme le rate limit IP :
    - **Auditabilité** : la table survit à un flush Redis — on peut
      investiguer a posteriori les comportements suspects.
    - **Rétention longue** : on peut purger les lignes > 30 jours via
      un job arq, mais conserver l'historique des abus courts.
    - **Jointure** : facile de voir quels devices ont spam récemment
      en croisant avec `auth_events`.

    La clé primaire composite `(device_id, day)` garantit qu'un même
    device ne peut pas avoir deux lignes pour le même jour → l'UPSERT
    `ON CONFLICT ... DO UPDATE SET count = count + 1` est atomique.
    """

    __tablename__ = "device_quotas"

    # `device_id` est soit un UUID Flutter (`X-Device-Id`), soit la
    # sentinelle `"unknown"` si le header est absent. Varchar plutôt
    # qu'UUID pour accommoder la sentinelle sans hack.
    device_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    # `day` est la date UTC de la journée qui compte — UTC et pas locale
    # pour éviter les bugs de fuseau (un attaquant en Australie sur une
    # timezone locale pourrait « avancer » artificiellement la journée).
    day: Mapped[datetime] = mapped_column(Date, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0", default=0)
    # Dernière IP observée pour ce device/day — utile pour la traque
    # d'attaques distribuées (même device_id tourne sur 50 IPs = bot).
    last_ip: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )


class AuthEvent(Base, UUIDMixin):
    """Journal des événements d'authentification — audit + forensic.

    Chaque action sensible (register, login réussi, login échoué, reset
    password, change password, logout, delete account) insère une ligne.
    Indispensable pour :
    - **Forensic post-incident** : quel compte a été compromis quand ?
    - **Détection de patterns** : 50 logins ratés sur le même user en
      30 min = brute-force ciblé → alerte.
    - **Conformité RGPD** : on doit pouvoir répondre « oui, tel compte
      a été désactivé à telle date via cet IP ».

    Design :
    - `user_id` nullable — un register échoué sur un email inexistant
      n'a pas d'user associé, mais on veut quand même la trace.
    - `event_type` contraint par CHECK SQL — 7 valeurs fermées,
      l'application ne peut pas écrire n'importe quoi.
    - `metadata_json` JSONB libre — détails non-PII (error_code,
      device_id hashé, user_agent tronqué).
    - `ip` stocké brut — assumé comme métadonnée légitime pour l'audit
      sécurité (pas un identifiant personnel au sens RGPD pour ce usage).
    - **Pas d'ON DELETE CASCADE** — quand un user est supprimé (RGPD),
      on ANONYMISE l'user mais on garde les events avec `user_id=NULL`
      après un UPDATE (le service auth s'en charge). Supprimer l'audit
      casserait la traçabilité.
    """

    __tablename__ = "auth_events"

    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    ip: Mapped[str | None] = mapped_column(String(64))
    # User-Agent tronqué à 256 chars — plus que ça, c'est du bruit
    # (un UA normal fait 100-200 chars).
    user_agent: Mapped[str | None] = mapped_column(String(256))
    device_id: Mapped[str | None] = mapped_column(String(128))
    # Métadonnées libres mais **jamais** de PII : pas d'email, pas de
    # mot de passe, pas de token. Que des codes + identifiants techniques.
    metadata_json: Mapped[dict | None] = mapped_column("metadata", JSONB)

    __table_args__ = (
        CheckConstraint(
            "event_type IN ("
            "'register_success', 'register_failed', "
            "'login_success', 'login_failed', "
            "'logout', "
            "'password_change', 'password_reset_request', 'password_reset_success', "
            "'account_delete', "
            "'captcha_failed', 'device_quota_exceeded'"
            ")",
            name="ck_auth_events_event_type",
        ),
        # Index pour requêtes forensic courantes : "tous les events de
        # cet user sur les 30 derniers jours", "tous les logins ratés
        # sur cette IP cette heure".
        Index("idx_auth_events_user_time", "user_id", "created_at"),
        Index("idx_auth_events_type_time", "event_type", "created_at"),
        Index("idx_auth_events_ip_time", "ip", "created_at"),
    )
