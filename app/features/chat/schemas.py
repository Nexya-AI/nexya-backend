"""
Schémas Pydantic Chat — Request/Response pour les endpoints /conversations,
/chat/stream, /chat/stop, /reports.

Conventions NEXYA :
- Suffixe Request / Response selon l'usage
- Validation stricte côté API (longueurs, enums, regex)
- model_config = {"from_attributes": True} sur les DTOs nourris d'ORM
- Types enum-like via Literal[...] Pydantic (pas d'ENUM Python côté serveur)

Design clés :
- `ChatStreamRequest` garde la compat descendante : si `conversation_id=None`
  et `history` non vide, le backend exécute le chemin legacy stateless
  (le message n'est pas persisté). Quand le Flutter bascule sur l'appel
  avec `conversation_id`, le backend persiste et renvoie l'identifiant via
  le header `X-Conversation-Id`.
- `MessagesPage` utilise un curseur opaque base64 (ISO timestamp + UUID)
  pour éviter le drame OFFSET au-delà de 10k messages.
- `AbuseReportCreate.reason` est un Literal aligné sur la CHECK constraint
  DB : toute divergence est un bug.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator

# ══════════════════════════════════════════════════════════════
# Types communs
# ══════════════════════════════════════════════════════════════

MessageRole = Literal["user", "assistant", "system"]
MessageStatus = Literal["streaming", "completed", "failed", "cancelled"]
AbuseReason = Literal["offensive", "dangerous", "illegal", "harassment", "misinformation", "other"]
AbuseStatus = Literal["pending", "reviewed", "dismissed", "action_taken"]


# Longueur max d'un message côté API : 32k chars ≈ 8k tokens, au-dessus la
# latence et le coût explosent sans valeur utilisateur. La DB (TEXT) accepte
# beaucoup plus — ce cap est volontaire, pas structurel.
_MESSAGE_MAX_CHARS = 32_000


# ══════════════════════════════════════════════════════════════
# CONVERSATION — CRUD
# ══════════════════════════════════════════════════════════════


class ConversationCreate(BaseModel):
    """Création explicite d'une conversation (sans envoyer de message).

    Usage principal : le Flutter crée la conv côté UI avant de streamer.
    `title` et `expert_id` sont optionnels — un titre vide est remplacé par
    le job d'auto-génération après le premier échange.

    `project_id` (D3 — 2026-05-04) attache la conversation à un projet
    existant dès la création. Le service vérifie l'ownership du projet
    via `ProjectService._get_owned_project` et lève
    `ResourceNotFoundException("Projet")` 404 IDOR-safe si le projet
    n'existe pas, n'appartient pas à l'utilisateur courant, ou est
    soft-deleted. Pas de 403 (anti-énumération UUID).
    """

    title: str | None = Field(default=None, max_length=120)
    expert_id: str | None = Field(default=None, min_length=1, max_length=32)
    project_id: uuid.UUID | None = None


class ConversationUpdate(BaseModel):
    """Mise à jour partielle — seuls les champs envoyés sont modifiés.

    Note : on ne laisse pas modifier `expert_id` après création pour
    éviter qu'un user force un expert "medicine" sur une conv existante et
    contourne la logique de tarification / disclaimer.
    """

    title: str | None = Field(default=None, max_length=120)
    is_archived: bool | None = None
    is_favorite: bool | None = None

    @field_validator("title")
    @classmethod
    def title_not_only_whitespace(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        if not stripped:
            raise ValueError("Le titre ne peut pas être vide.")
        return stripped


class ConversationResponse(BaseModel):
    """Conversation complète renvoyée par GET /conversations/{id}.

    `deleted_at` est `None` sur les endpoints actifs (la clause SQL
    `deleted_at IS NULL` garantit qu'aucune conv supprimée ne fuit)
    et est peuplé sur les endpoints de corbeille (`GET /conversations/trash`).
    """

    id: uuid.UUID
    user_id: uuid.UUID
    title: str | None
    expert_id: str
    last_message_at: datetime | None
    message_count: int
    is_archived: bool
    is_favorite: bool
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    model_config = {"from_attributes": True}


class ConversationListItem(BaseModel):
    """Item allégé pour GET /conversations (liste paginée).

    Contient tout ce dont l'écran d'historique Flutter a besoin pour
    afficher une ligne : titre, expert, preview timing, compteurs, flags.
    Pas de `user_id` (implicite — c'est le user connecté).

    `deleted_at` : cf. `ConversationResponse` — `None` hors corbeille.
    """

    id: uuid.UUID
    title: str | None
    expert_id: str
    last_message_at: datetime | None
    message_count: int
    is_archived: bool
    is_favorite: bool
    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    model_config = {"from_attributes": True}


class ConversationsPage(BaseModel):
    """Page de conversations paginée cursor-based.

    `next_cursor` est une chaîne opaque (base64 de `{sort_ts.isoformat()}|{id}`)
    que le client renvoie tel quel via `?cursor=...` pour demander la page
    suivante. `None` signifie fin de liste — le client arrête de paginer.
    """

    items: list[ConversationListItem]
    next_cursor: str | None


# ══════════════════════════════════════════════════════════════
# MESSAGE — lecture paginée cursor-based
# ══════════════════════════════════════════════════════════════


class MessageResponse(BaseModel):
    """Message unitaire — complet, incluant métriques de coût et statut.

    Les champs nullables (`provider`, `model`, `*_tokens`, `cost_usd`,
    `error_code`, `finished_at`) ne sont présents que pour les messages
    `role='assistant'` terminés.
    """

    id: uuid.UUID
    conversation_id: uuid.UUID
    role: MessageRole
    content: str
    status: MessageStatus
    provider: str | None
    model: str | None
    prompt_tokens: int | None
    completion_tokens: int | None
    total_tokens: int | None
    cost_usd: Decimal | None
    error_code: str | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MessagesPage(BaseModel):
    """Page de messages paginée cursor-based.

    `next_cursor` est une chaîne opaque (base64 de `{created_at.isoformat()}|{id}`)
    que le client repasse tel quel à la requête suivante via `?cursor=...`.
    `None` signifie fin de liste.
    """

    items: list[MessageResponse]
    next_cursor: str | None


# ══════════════════════════════════════════════════════════════
# CHAT STREAM — SSE
# ══════════════════════════════════════════════════════════════


class ChatStreamInlineMessage(BaseModel):
    """Message inline du champ `history` (chemin legacy stateless).

    Gardé pour la compat du Flutter actuel, qui envoie l'historique complet
    à chaque requête. Dès que le Flutter passe `conversation_id`, ce champ
    est ignoré par le backend (la vérité est en base).
    """

    role: str = Field(min_length=1, max_length=16)
    content: str = Field(max_length=_MESSAGE_MAX_CHARS)


class ChatStreamRequest(BaseModel):
    """Corps de POST /chat/stream.

    Comportement selon les champs présents :

    1. `conversation_id=None` et `history=[]` → création implicite d'une
       nouvelle conversation, persistance user+assistant, titre généré
       plus tard par un job arq.
    2. `conversation_id=<UUID>` → ajout à une conversation existante ;
       `history` ignoré (le backend rebuild le contexte depuis la base).
    3. `conversation_id=None` et `history=[...]` → chemin legacy stateless :
       le message n'est PAS persisté. À retirer quand le Flutter migre.

    `project_id` (D3 — 2026-05-04) attache la conversation au projet
    spécifié uniquement dans le mode 1 (création implicite). Sémantique
    selon la combinaison :

    - `conversation_id=None` ET `project_id=<UUID>` → création d'une
      nouvelle conv attachée au projet (ownership check via
      `ProjectService._get_owned_project`, 404 IDOR-safe sinon).
    - `conversation_id=<UUID>` ET `project_id=<UUID>` → **ignore
      silencieusement** `project_id` + log debug. Le rattachement d'une
      conv existante à un projet ne passe PAS par `/chat/stream` (V1) —
      sera exposé via un futur `PATCH /chat/conversations/{id}` quand le
      backend supportera la mutation `project_id`. Ce choix V1 garde le
      contrat simple côté front (un seul appel par message, pas de
      mutation cross-feature transparente).
    - `conversation_id=None` ET `project_id=None` → comportement legacy
      strictement préservé (rétrocompat A1+B1+B2+B3+B4).
    """

    message: str = Field(min_length=1, max_length=_MESSAGE_MAX_CHARS)
    conversation_id: uuid.UUID | None = None
    expert_id: str | None = Field(default=None, min_length=1, max_length=32)
    session_id: str | None = Field(default=None, max_length=128)
    project_id: uuid.UUID | None = None
    history: list[ChatStreamInlineMessage] = Field(default_factory=list, max_length=50)

    @field_validator("message")
    @classmethod
    def message_not_only_whitespace(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Le message ne peut pas être vide.")
        return v


class ChatStopRequest(BaseModel):
    """Corps de POST /chat/stop — annule un stream actif via clé Redis."""

    session_id: str = Field(min_length=1, max_length=128)


# ══════════════════════════════════════════════════════════════
# IMAGE — génération
# ══════════════════════════════════════════════════════════════


class ImageGenerateRequest(BaseModel):
    """Corps de POST /image/generate."""

    prompt: str = Field(min_length=1, max_length=2_000)
    count: int = Field(default=1, ge=1, le=4)
    expert_id: str | None = Field(default="studio", min_length=1, max_length=32)


# ══════════════════════════════════════════════════════════════
# ABUSE REPORTS — signalement de messages
# ══════════════════════════════════════════════════════════════


class AbuseReportCreate(BaseModel):
    """Corps de POST /reports — signalement d'un message abusif.

    `reason` est un Literal aligné sur la CHECK constraint SQL. `detail`
    est optionnel (500 chars max) — le Flutter peut l'afficher comme
    commentaire libre dans le formulaire.
    """

    message_id: uuid.UUID
    reason: AbuseReason
    detail: str | None = Field(default=None, max_length=500)

    @field_validator("detail")
    @classmethod
    def detail_not_only_whitespace(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class AbuseReportResponse(BaseModel):
    """AbuseReport retourné à l'utilisateur après signalement.

    L'utilisateur ne voit jamais les champs `reviewer_notes` / `reviewed_by` /
    `reviewed_at` — ils sont réservés aux endpoints admin (non livrés dans
    cette phase).
    """

    id: uuid.UUID
    message_id: uuid.UUID
    conversation_id: uuid.UUID
    reason: AbuseReason
    detail: str | None
    status: AbuseStatus
    created_at: datetime

    model_config = {"from_attributes": True}
