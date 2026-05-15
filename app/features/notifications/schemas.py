"""
Schémas Pydantic Notifications — F3.

Expose les types utilisés par le router `/notifications/*` et
`/user/notification-preferences`. Les secrets internes (message IDs
provider, compteurs d'attempts) ne fuitent jamais vers le client.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ═══════════════════════════════════════════════════════════════════
# Types partagés — alignés 1:1 sur les CHECK SQL
# ═══════════════════════════════════════════════════════════════════

NotificationCategory = Literal["tasks", "payments", "security", "digest", "product"]
"""Catégorie RGPD — sert de discriminateur de préférences et d'index.

- `tasks` : exécution d'une tâche planifiée.
- `payments` : webhook paiement réussi / échoué / abonnement renouvelé.
- `security` : login inhabituel, changement de mot de passe, suppression
  d'appareil, etc. **Non-désinscriptible par obligation légale** — le
  router `unsubscribe` refuse explicitement cette catégorie.
- `digest` : récapitulatif hebdomadaire / mensuel (Phase 12+).
- `product` : annonces produit (`update`/`feature`/`promo`/`tip` côté
  Flutter via `data_json.subtype`).
"""

NotificationChannel = Literal["push", "email", "both", "none"]
"""Canal choisi dans les préférences user."""

NotificationChannelUsed = Literal["push", "email", "both", "skipped"]
"""Canal réellement utilisé lors de l'envoi (post-fallback).

Distinct de `NotificationChannel` : pas de `none` ici (une row
`notifications` avec `channel_used='skipped'` trace une tentative
pour laquelle rien n'a été envoyé).
"""

NotificationSourceKind = Literal[
    "scheduled_task", "payment", "security", "digest", "product", "manual"
]
"""Type d'événement déclencheur — aide au diagnostic et aux dashboards."""


# ═══════════════════════════════════════════════════════════════════
# Timeline : list + read + delete
# ═══════════════════════════════════════════════════════════════════


class NotificationResponse(BaseModel):
    """Représentation publique d'une notification.

    `data` est renommé depuis `data_json` côté ORM pour exposer un nom
    naturel côté API. Les compteurs `attempts_push`/`attempts_email` et
    les message IDs provider NE sont PAS exposés (secrets internes).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category: NotificationCategory
    title: str
    body: str
    data: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="data_json",
        serialization_alias="data",
    )
    channel_used: NotificationChannelUsed
    source_task_id: uuid.UUID | None = None
    source_kind: NotificationSourceKind
    read_at: datetime | None = None
    sent_at: datetime


class NotificationListItem(BaseModel):
    """Variante allégée pour la timeline (sans `body` complet).

    Le body peut être tronqué côté service pour les grilles compactes ;
    le client rappelle `GET /notifications/{id}` pour le détail complet
    si besoin. Pour F3 on garde `body` complet mais le schéma est
    séparé pour permettre l'évolution sans casser le contrat `NotificationResponse`.
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    category: NotificationCategory
    title: str
    body: str
    data: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias="data_json",
        serialization_alias="data",
    )
    channel_used: NotificationChannelUsed
    source_task_id: uuid.UUID | None = None
    source_kind: NotificationSourceKind
    read_at: datetime | None = None
    sent_at: datetime


class NotificationsPage(BaseModel):
    """Page paginée keyset — cursor opaque + items."""

    items: list[NotificationListItem]
    next_cursor: str | None = None


class MarkReadRequest(BaseModel):
    """Corps de `POST /notifications/read`."""

    notification_ids: list[uuid.UUID] = Field(
        ...,
        min_length=1,
        max_length=100,
        description=(
            "Liste des IDs à marquer comme lus. Entre 1 et 100 IDs (au-delà, paginer côté client)."
        ),
    )

    @field_validator("notification_ids")
    @classmethod
    def _reject_duplicates(cls, v: list[uuid.UUID]) -> list[uuid.UUID]:
        if len(set(v)) != len(v):
            raise ValueError("IDs dupliqués interdits dans la même requête.")
        return v


class MarkReadResponse(BaseModel):
    """Réponse de `POST /notifications/read`."""

    marked: int = Field(
        ...,
        ge=0,
        description=(
            "Nombre de rows effectivement mises à jour. "
            "< len(notification_ids) si certains IDs n'appartenaient "
            "pas à l'user ou étaient déjà lus."
        ),
    )


# ═══════════════════════════════════════════════════════════════════
# Préférences
# ═══════════════════════════════════════════════════════════════════


class NotificationPreferenceItem(BaseModel):
    """Une ligne préférence : (catégorie, canal choisi)."""

    model_config = ConfigDict(from_attributes=True)

    category: NotificationCategory
    channel: NotificationChannel


class NotificationPreferencesResponse(BaseModel):
    """Réponse `GET /user/notification-preferences`.

    Contient TOUJOURS les 5 catégories — les defaults sont injectés
    côté service si l'user n'a pas de row pour une catégorie donnée.
    """

    preferences: list[NotificationPreferenceItem] = Field(
        ...,
        description="5 catégories RGPD, toutes toujours présentes.",
    )


class UpdatePreferencesRequest(BaseModel):
    """Corps de `PUT /user/notification-preferences`.

    Le user peut envoyer 1 à 5 lignes. Les catégories non envoyées
    gardent leur valeur actuelle (upsert partial — pas un replace
    total). Pas de doublon de catégorie autorisé.
    """

    preferences: list[NotificationPreferenceItem] = Field(
        ...,
        min_length=1,
        max_length=5,
    )

    @model_validator(mode="after")
    def _reject_duplicate_categories(self) -> UpdatePreferencesRequest:
        seen: set[str] = set()
        for pref in self.preferences:
            if pref.category in seen:
                raise ValueError(f"Catégorie '{pref.category}' envoyée plusieurs fois.")
            seen.add(pref.category)
        return self

    @model_validator(mode="after")
    def _reject_security_none(self) -> UpdatePreferencesRequest:
        # Bug-009 fix 2026-05-13 : la catégorie `security` ne peut PAS être
        # désactivée (`channel=none`). Obligation légale RGPD Article 33
        # (notification de violation) + AI Act Article 13 (transparence
        # alertes sécurité). Cohérence avec l'endpoint public
        # `/notifications/unsubscribe/{token}` qui refuse déjà ce cas via
        # `UnsubscribeSecurityRefusedException`.
        for pref in self.preferences:
            if pref.category == "security" and pref.channel == "none":
                raise ValueError(
                    "La catégorie 'security' ne peut pas être désactivée "
                    "(obligation légale de notifier les alertes sécurité)."
                )
        return self


# ═══════════════════════════════════════════════════════════════════
# Unsubscribe one-click
# ═══════════════════════════════════════════════════════════════════


class UnsubscribeConfirmationResponse(BaseModel):
    """Réponse `POST /notifications/unsubscribe/{token}`."""

    category: NotificationCategory
    channel_after: Literal["none"] = "none"
    message: str = Field(
        default=("Désinscription confirmée. Vous ne recevrez plus d'emails pour cette catégorie.")
    )
