"""
Schémas Pydantic — Rich content payload (C4.4).

Stocké dans `messages.metadata_json.rich_content` (JSONB déjà livré
planner-from-chat LOT B 2026-05-23). Discriminé par `kind`.

Caps stricts :
  - `subject` ≤ 300 chars (limite raisonnable Gmail/Outlook UI)
  - `body` ≤ 10 000 chars (cap dur anti-explosion mailto URL + WhatsApp
    payload, le client Flutter cap visuellement à 10k chars de toute façon)
  - `to` ≤ 320 chars (RFC 5321 longueur max d'une adresse email)
  - `phone` ≤ 20 chars (E.164 max 15 + caractères de format)

Le schéma `RichContentPayload` est exposé au client via
`MessageResponse.metadata_json["rich_content"]` (typage `dict` côté
Pydantic pour ne pas exploser le contrat). Le client Flutter parse en
fail-safe via `DraftPayload.tryFromMetadata`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

RichContentKind = Literal["email_draft", "whatsapp_draft"]


class EmailDraftData(BaseModel):
    """Payload d'un brouillon d'email.

    `subject` optionnel — certains emails informels n'en ont pas. Le
    client affichera un placeholder dans l'UI si absent.

    `to` optionnel — le LLM ne sait généralement pas à qui l'utilisateur
    veut envoyer le mail. Le client laisse le champ vide pour que l'user
    le complète après tap « ✉ Envoyer ».
    """

    subject: str | None = Field(default=None, max_length=300)
    body: str = Field(min_length=1, max_length=10_000)
    to: str | None = Field(default=None, max_length=320)

    @field_validator("subject", "to")
    @classmethod
    def _strip_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class WhatsAppDraftData(BaseModel):
    """Payload d'un brouillon WhatsApp.

    `phone` optionnel — comme `to` côté email, le LLM ne sait pas
    à qui envoyer. L'user complète après tap « 💬 Ouvrir WhatsApp ».
    Format attendu : E.164 sans préfixe `+` (WhatsApp accepte les deux,
    le client Flutter normalise).
    """

    phone: str | None = Field(default=None, max_length=20)
    body: str = Field(min_length=1, max_length=10_000)

    @field_validator("phone")
    @classmethod
    def _strip_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class RichContentPayload(BaseModel):
    """Discriminé par `kind`.

    Stocké tel quel dans `messages.metadata_json["rich_content"]`.
    Le `data` est un `dict` côté Pydantic — le caller détecteur produit
    un payload conforme à `EmailDraftData` ou `WhatsAppDraftData` selon
    le `kind`, validé au moment de la construction via les factory
    `RichContentPayload.email()` / `RichContentPayload.whatsapp()`.
    """

    kind: RichContentKind
    data: dict

    @classmethod
    def email(
        cls,
        *,
        subject: str | None,
        body: str,
        to: str | None = None,
    ) -> "RichContentPayload":
        """Construit un payload email avec validation Pydantic stricte.

        Lève `ValidationError` si `body` vide ou trop long, `subject`/`to`
        trop longs, etc. Le caller détecteur garantit que ces invariants
        sont respectés AVANT d'appeler ce constructeur.
        """
        data = EmailDraftData(subject=subject, body=body, to=to)
        return cls(kind="email_draft", data=data.model_dump())

    @classmethod
    def whatsapp(
        cls,
        *,
        phone: str | None,
        body: str,
    ) -> "RichContentPayload":
        """Construit un payload WhatsApp avec validation Pydantic stricte."""
        data = WhatsAppDraftData(phone=phone, body=body)
        return cls(kind="whatsapp_draft", data=data.model_dump())
