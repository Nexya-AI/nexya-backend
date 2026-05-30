"""
Schémas Pydantic — Rich content payload (C4.4 + C4.5).

Stocké dans `messages.metadata_json.rich_content` (JSONB déjà livré
planner-from-chat LOT B 2026-05-23). Discriminé par `kind`.

V1 (C4.4) — 2 kinds : email_draft + whatsapp_draft.
V2 (C4.5) — 4 nouveaux kinds : sms_draft + linkedin_post_draft +
            tweet_draft + document_draft.

Caps stricts par kind :
  EMAIL (C4.4) :
  - `subject` ≤ 300 chars (limite raisonnable Gmail/Outlook UI)
  - `body` ≤ 10 000 chars
  - `to` ≤ 320 chars (RFC 5321 longueur max email)

  WHATSAPP (C4.4) :
  - `phone` ≤ 20 chars (E.164 max 15 + format)
  - `body` ≤ 10 000 chars

  SMS (C4.5) :
  - `phone` ≤ 20 chars (E.164 max 15 + format)
  - `body` ≤ 1 600 chars (cap dur — 10 segments SMS de 160 chars,
    au-delà les opérateurs basculent en MMS ou tronquent silencieusement).

  LINKEDIN POST (C4.5) :
  - `body` ≤ 3 000 chars (limite officielle LinkedIn Posts 2026,
    cf. https://www.linkedin.com/help/linkedin/answer/a566188).

  TWEET (C4.5) :
  - `body` ≤ 280 chars (limite officielle Twitter/X 2026,
    cap dur côté backend pour économiser le round-trip si le LLM
    sur-génère, le client refuse aussi côté UI compose).

  DOCUMENT (C4.5) :
  - `title` ≤ 300 chars (titre du document, optionnel)
  - `body` ≤ 50 000 chars (~10 pages PDF A4 dense, cap dur anti
    explosion taille fichier généré côté Flutter `printing` lib)
  - `recipient` ≤ 200 chars (destinataire formel optionnel,
    p.ex. « Madame Le Maire de Yaoundé », pour entête lettre)

Le schéma `RichContentPayload` est exposé au client via
`MessageResponse.metadata_json["rich_content"]` (typage `dict` côté
Pydantic pour ne pas exploser le contrat). Le client Flutter parse en
fail-safe via `DraftPayload.tryFromMetadata`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator

RichContentKind = Literal[
    "email_draft",
    "whatsapp_draft",
    "sms_draft",
    "linkedin_post_draft",
    "tweet_draft",
    "document_draft",
]


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


class SmsDraftData(BaseModel):
    """Payload d'un brouillon SMS (C4.5).

    `phone` optionnel — pattern aligné WhatsApp. L'user complète après
    tap « 📱 Envoyer SMS ». Format attendu : E.164.

    `body` capé 1600 chars (= 10 segments SMS de 160 chars). Au-delà
    les opérateurs Afrique francophone (Orange/MTN/Wave) basculent en
    MMS coûteux ou tronquent silencieusement à 160 chars — on bloque
    côté backend pour épargner ce piège à l'user.
    """

    phone: str | None = Field(default=None, max_length=20)
    body: str = Field(min_length=1, max_length=1_600)

    @field_validator("phone")
    @classmethod
    def _strip_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class LinkedInPostDraftData(BaseModel):
    """Payload d'un brouillon LinkedIn post (C4.5).

    Pas de destinataire (un post LinkedIn est publié sur le mur de
    l'utilisateur, pas envoyé à un contact). L'user édite le body
    dans la NxDraftCard puis tape « 💼 Ouvrir LinkedIn » qui ouvre
    le composer LinkedIn natif avec le texte pré-rempli (deep link
    `linkedin://shareArticle?text=` ou fallback https://linkedin.com/feed/?shareActive=true).

    `body` capé 3000 chars (limite officielle LinkedIn 2026, cf.
    `https://www.linkedin.com/help/linkedin/answer/a566188`).
    """

    body: str = Field(min_length=1, max_length=3_000)


class TweetDraftData(BaseModel):
    """Payload d'un brouillon Tweet/X post (C4.5).

    Pas de destinataire (un tweet est publié sur le profil).
    L'user tape « 🐦 Ouvrir X » qui ouvre le composer X natif avec
    le texte pré-rempli (deep link `twitter://post?message=` ou fallback
    https://twitter.com/intent/tweet?text=).

    `body` capé 280 chars (limite officielle Twitter/X 2026, cap dur
    côté backend pour économiser le round-trip si le LLM sur-génère).
    Le client refuse aussi côté UI compose.

    Note : la limite étendue 25 000 chars pour les abonnés X Premium
    n'est PAS supportée V1 (cas marginal, le compose natif X coupera
    proprement à 280 si l'user n'est pas Premium).
    """

    body: str = Field(min_length=1, max_length=280)


class DocumentDraftData(BaseModel):
    """Payload d'un brouillon de document long (C4.5).

    Cas d'usage : « rédige-moi une lettre formelle au maire de Yaoundé
    pour demander un acte de naissance », « rédige un rapport de
    réunion », « rédige un compte-rendu de stage », « génère un cours
    sur les boucles for en Python ».

    L'user tape « 📄 Générer PDF » qui appelle `printing` lib côté
    Flutter et produit un PDF natif partageable via share_plus.

    `title` optionnel — sert d'entête PDF + filename (sanitizé côté
    Flutter). Si absent, fallback `Document NEXYA - YYYY-MM-DD.pdf`.

    `recipient` optionnel — pour les lettres formelles, sert d'entête
    formelle « À l'attention de <recipient> ». Pour les cours/rapports
    sans destinataire, laisser None.

    `body` capé 50 000 chars (~10 pages PDF A4 dense). Cap dur anti
    explosion taille fichier généré (un PDF de 50k chars fait ~500 KB
    raisonnable Africa-first 2G/3G, au-delà = friction partage).
    """

    title: str | None = Field(default=None, max_length=300)
    body: str = Field(min_length=1, max_length=50_000)
    recipient: str | None = Field(default=None, max_length=200)

    @field_validator("title", "recipient")
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
    un payload conforme à `XxxDraftData` selon le `kind`, validé au
    moment de la construction via les factory `RichContentPayload.xxx()`.
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

    @classmethod
    def sms(
        cls,
        *,
        phone: str | None,
        body: str,
    ) -> "RichContentPayload":
        """Construit un payload SMS avec validation Pydantic stricte (C4.5).

        Lève `ValidationError` si `body` vide ou > 1600 chars.
        """
        data = SmsDraftData(phone=phone, body=body)
        return cls(kind="sms_draft", data=data.model_dump())

    @classmethod
    def linkedin_post(
        cls,
        *,
        body: str,
    ) -> "RichContentPayload":
        """Construit un payload LinkedIn post avec validation stricte (C4.5).

        Lève `ValidationError` si `body` vide ou > 3000 chars.
        """
        data = LinkedInPostDraftData(body=body)
        return cls(kind="linkedin_post_draft", data=data.model_dump())

    @classmethod
    def tweet(
        cls,
        *,
        body: str,
    ) -> "RichContentPayload":
        """Construit un payload Tweet/X avec validation stricte (C4.5).

        Lève `ValidationError` si `body` vide ou > 280 chars.
        """
        data = TweetDraftData(body=body)
        return cls(kind="tweet_draft", data=data.model_dump())

    @classmethod
    def document(
        cls,
        *,
        title: str | None,
        body: str,
        recipient: str | None = None,
    ) -> "RichContentPayload":
        """Construit un payload document long avec validation stricte (C4.5).

        Lève `ValidationError` si `body` vide ou > 50 000 chars, `title`/
        `recipient` trop longs.
        """
        data = DocumentDraftData(title=title, body=body, recipient=recipient)
        return cls(kind="document_draft", data=data.model_dump())
