"""
Modèles ORM Chat — Conversation, Message, AbuseReport.

Ces 3 tables forment le socle de la persistance Chat NEXYA.
Schéma SQL aligné sur BACKEND_IA_NEXYA.md section 3 (Chat & Messages).

Choix de design clés :
- `deleted_at` (nullable) partout → soft-delete RGPD ; l'utilisateur peut
  restaurer, l'admin voit toujours pour audit, CASCADE côté DB ne frappe
  que lors d'une suppression définitive du compte.
- `last_message_at` + `message_count` dénormalisés sur Conversation →
  tri et comptage O(1) sans scan de la table messages (critique à 950k+).
- `status` VARCHAR + CHECK plutôt qu'ENUM Postgres → ajouter un statut ne
  requiert pas de migration DDL coûteuse.
- Pas de relation `User.conversations` → évite le N+1 silencieux quand un
  service charge un User et déclenche un `selectin` sur toutes ses convs.
  Les requêtes de liste passent explicitement par ConversationService.
- UNIQUE (user_id, message_id) sur AbuseReport → un user ne peut signaler
  deux fois le même message (anti-spam naturel + idempotence côté client).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database.base import Base, UUIDMixin

# ══════════════════════════════════════════════════════════════
# Conversation — un fil de discussion appartenant à un user
# ══════════════════════════════════════════════════════════════


class Conversation(Base, UUIDMixin):
    """Fil de chat persisté.

    - `title` est généré automatiquement par un job arq après le premier
      échange (user+assistant complet). `title_generated_at` sert de
      sentinelle anti-relance pour garantir un one-shot.
    - `expert_id` est stocké en VARCHAR (pas d'ENUM) pour laisser la liberté
      d'ajouter un expert sans migration DDL. Le CHECK constraint interdit
      uniquement la chaîne vide.
    - `last_message_at` et `message_count` sont dénormalisés : maintenus par
      le service à chaque nouveau message via UPDATE atomique
      (`SET message_count = message_count + 1, last_message_at = NOW()`).
    - `is_archived` et `is_favorite` sont des flags UI sans impact sur le
      cycle de vie : archive ≠ soft-delete, favori ≠ ordre de tri absolu.
    """

    __tablename__ = "conversations"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Rattachement optionnel à un projet — `ON DELETE SET NULL` côté FK :
    # une purge physique d'un projet détache ses conversations sans les
    # perdre (voir migration 006 + note dans Project). Le soft-delete d'un
    # projet se traduit par un UPDATE côté service (`SET project_id = NULL`).
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("projects.id", ondelete="SET NULL"),
        nullable=True,
    )
    title: Mapped[str | None] = mapped_column(String(120))
    expert_id: Mapped[str] = mapped_column(
        String(32), server_default="general", default="general", nullable=False
    )
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    message_count: Mapped[int] = mapped_column(
        Integer, server_default="0", default=0, nullable=False
    )
    is_archived: Mapped[bool] = mapped_column(
        Boolean, server_default="false", default=False, nullable=False
    )
    is_favorite: Mapped[bool] = mapped_column(
        Boolean, server_default="false", default=False, nullable=False
    )
    title_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Sentinelle one-shot pour le worker D2 `extract_durable_facts` —
    # posée à `NOW()` dès que le job a tourné (même si 0 fait extrait),
    # empêche toute ré-exécution ultérieure sur la même conversation.
    # Même discipline que `title_generated_at` (B5 auto-title).
    memory_extracted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Relations ──────────────────────────────────────────────
    # lazy="noload" : on ne charge JAMAIS tous les messages en suivant la
    # relation — ils passent toujours par une query paginée cursor-based.
    messages: Mapped[list[Message]] = relationship(
        back_populates="conversation",
        cascade="all, delete-orphan",
        lazy="noload",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("char_length(expert_id) > 0", name="ck_conversations_expert_id_not_empty"),
        CheckConstraint("message_count >= 0", name="ck_conversations_message_count_non_negative"),
        # Liste principale paginée : convs d'un user, non supprimées, triées par récence.
        Index(
            "idx_conversations_user_time",
            "user_id",
            "deleted_at",
            "last_message_at",
        ),
        # Filtre "favoris" en index partiel (peu de favoris → index léger).
        Index(
            "idx_conversations_user_favorite",
            "user_id",
            "last_message_at",
            postgresql_where=text("is_favorite = true AND deleted_at IS NULL"),
        ),
    )


# ══════════════════════════════════════════════════════════════
# Message — un tour de parole dans une conversation
# ══════════════════════════════════════════════════════════════


class Message(Base, UUIDMixin):
    """Message unitaire — user, assistant ou system.

    - `content` est TEXT (pas de limite DB) ; le plafond applicatif est
      imposé par le schéma Pydantic pour couper les inputs abusifs.
    - `status` reflète l'état du stream côté assistant :
        - `streaming` : le StreamHandler écrit en cours de route.
        - `completed` : stream terminé proprement (événement `done`).
        - `failed`    : le provider a levé une erreur non-récupérable.
        - `cancelled` : l'utilisateur a appelé /chat/stop en cours.
      Les messages user/system sont toujours `completed` à l'insertion.
    - Les colonnes de tokens/coût ne sont renseignées que pour les messages
      `role='assistant'` après la fin du stream (finalisation atomique).
    - `NUMERIC(10, 6)` pour `cost_usd` : 4 chiffres avant, 6 après, suffisant
      jusqu'à 9 999 $ par message (largement au-delà du worst-case).
    """

    __tablename__ = "messages"

    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(Text, server_default="", default="", nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), server_default="completed", default="completed", nullable=False
    )
    provider: Mapped[str | None] = mapped_column(String(32))
    model: Mapped[str | None] = mapped_column(String(64))
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None] = mapped_column(Numeric(10, 6))
    error_code: Mapped[str | None] = mapped_column(String(64))
    # planner-from-chat (2026-05-22) — métadonnées structurées d'un message
    # assistant. V1 : `{"tool_calls": [{id, name, success, data, error}, …]}`
    # — instantané des tool calls Planner exécutés pendant le stream. Permet
    # à la carte de tâche du chat de survivre à la réouverture de la
    # conversation (sans ça, les tool calls vivaient seulement en mémoire).
    # `None` pour la quasi-totalité des messages.
    metadata_json: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # ── Relation ───────────────────────────────────────────────
    conversation: Mapped[Conversation] = relationship(back_populates="messages")

    __table_args__ = (
        CheckConstraint("role IN ('user', 'assistant', 'system')", name="ck_messages_role"),
        CheckConstraint(
            "status IN ('streaming', 'completed', 'failed', 'cancelled')",
            name="ck_messages_status",
        ),
        # Pagination cursor-based : (conv_id, created_at, id) unique et ordonné.
        # L'ajout de `id` en 3e position garantit la stabilité du curseur
        # quand deux messages partagent la même `created_at` (collision rare
        # mais possible en charge).
        Index("idx_messages_conv_time", "conversation_id", "created_at", "id"),
    )


# ══════════════════════════════════════════════════════════════
# AbuseReport — signalement d'un message par un utilisateur
# ══════════════════════════════════════════════════════════════


class AbuseReport(Base, UUIDMixin):
    """Signalement d'un message abusif — exigence App Store §1.2.

    - `conversation_id` est dénormalisé pour permettre à l'admin de cluster
      les signalements par conversation sans JOIN supplémentaire.
    - `UNIQUE (user_id, message_id)` interdit les doublons : si le Flutter
      re-soumet le même signalement (retry réseau, double tap), on retourne
      409 sans créer d'entrée fantôme.
    - `reviewed_by` est un UUID nullable sans FK (table `admin_users` pas
      encore définie). On ajoute la contrainte quand l'admin panel arrive.
    - `status` en VARCHAR+CHECK : ajouter un statut (ex: `escalated`) sans
      migration DDL destructrice.
    """

    __tablename__ = "abuse_reports"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
    )
    reason: Mapped[str] = mapped_column(String(32), nullable=False)
    detail: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(
        String(16), server_default="pending", default="pending", nullable=False
    )
    reviewer_notes: Mapped[str | None] = mapped_column(Text)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewed_by: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        UniqueConstraint("user_id", "message_id", name="uq_abuse_reports_user_message"),
        CheckConstraint(
            "reason IN ('offensive', 'dangerous', 'illegal', 'harassment', "
            "'misinformation', 'other')",
            name="ck_abuse_reports_reason",
        ),
        CheckConstraint(
            "status IN ('pending', 'reviewed', 'dismissed', 'action_taken')",
            name="ck_abuse_reports_status",
        ),
        # Queue admin : signalements en attente, plus vieux en premier.
        Index(
            "idx_abuse_reports_status_created",
            "status",
            "created_at",
        ),
        # Historique de signalements d'un user (anti-abus du signalement).
        Index(
            "idx_abuse_reports_user_created",
            "user_id",
            "created_at",
        ),
    )
