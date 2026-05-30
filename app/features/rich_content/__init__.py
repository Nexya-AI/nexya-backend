"""
Module `rich_content` (C4.4 + C4.5 — NxDraftCard 6 variantes).

Détection automatique de **contenu rédactionnel actionnable** dans la réponse
LLM (brouillons d'email, messages WhatsApp/SMS, posts LinkedIn/Tweet,
documents long PDF). Quand la détection réussit, le caller
(`chat/router._finalize_in_fresh_session`) injecte un payload structuré
dans `messages.metadata_json.rich_content` qui survit à la réouverture de la
conversation et permet au client Flutter de rendre la carte `NxDraftCard` au
lieu d'un bloc texte brut.

V1 (C4.4 — 2026-05-26) : email_draft + whatsapp_draft.
V2 (C4.5 — 2026-05-30) : sms_draft + linkedin_post_draft + tweet_draft +
                         document_draft.

Cascade de détection (ordre du plus spécifique au plus générique) :
  1. **WhatsApp** — intent-based fast short-circuit
  2. **SMS** — intent-based, body court 1600 chars cap
  3. **Tweet** — intent-based + cap dur 280 chars
  4. **LinkedIn** — intent-based + cap 3000 chars
  5. **Document** — intent + body formel STRUCTURÉ (lettre administrative
     avec recipient « Madame la Maire de Yaoundé » + formules de
     politesse « Veuillez agréer / Salutations distinguées »), cap 50k
  6. **Email** — heuristique markers semi-formels (subject + greeting
     `Bonjour` + closing `Cordialement`)

Ordre justifié — pourquoi Document AVANT Email :
- Une lettre formelle administrative cumule TOUS les markers d'un email
  (subject, greeting, closing) PLUS des markers spécifiques (recipient
  formel avec titre + organisme, formules de politesse littéraires).
- Si Email était testé avant, une lettre administrative serait classée
  comme email → cassé en Flutter (rendu carte email avec mailto:
  alors qu'il faut un PDF).
- Document a un seuil minimal de 200 chars + recipient + closing formel
  → ne wrongly classe pas un email court "Bonjour Marie, ..."
- Email reste défensif sur le passage informel/semi-formel.

Aucun appel LLM, aucun I/O, aucun side-effect — le module est pur Python
synchrone, testable en isolation sans fixture réseau ni DB.

V3 (différé) : LLM tool call `propose_rich_content({kind, data})` qui
produit une carte 100% structurée sans heuristique. Architecture compatible
(le tool result alimenterait directement `RichContentPayload`).
"""

from __future__ import annotations

from app.features.rich_content.document_draft_detector import (
    detect_document_intent,
    detect_rich_content_document,
)
from app.features.rich_content.email_draft_detector import (
    detect_email_intent,
    detect_rich_content_email,
)
from app.features.rich_content.linkedin_draft_detector import (
    detect_linkedin_intent,
    detect_rich_content_linkedin,
)
from app.features.rich_content.schemas import (
    DocumentDraftData,
    EmailDraftData,
    LinkedInPostDraftData,
    RichContentKind,
    RichContentPayload,
    SmsDraftData,
    TweetDraftData,
    WhatsAppDraftData,
)
from app.features.rich_content.sms_draft_detector import (
    detect_rich_content_sms,
    detect_sms_intent,
)
from app.features.rich_content.tweet_draft_detector import (
    detect_rich_content_tweet,
    detect_tweet_intent,
)
from app.features.rich_content.whatsapp_draft_detector import (
    detect_rich_content_whatsapp,
    detect_whatsapp_intent,
)


def detect_rich_content(user_message: str, assistant_text: str) -> dict | None:
    """Point d'entrée principal — cascade WhatsApp → SMS → Tweet → LinkedIn → Email → Document.

    Cascade ordonnée du plus spécifique (cap court 280-1600 + intent fort)
    au plus générique (markers formels potentiellement multi-usage).

    Retourne un `dict` prêt à être stocké dans
    `messages.metadata_json["rich_content"]` (format conforme à
    `RichContentPayload`), ou `None` si aucun draft détecté.

    Premier détecteur qui matche gagne — pas de combinaison multiple
    (la carte UI n'affiche qu'un seul type de draft par message).

    Fail-safe : chaque détecteur est défensif sur ses inputs (empty string,
    None coerce, exception Pydantic catché). Le caller `chat/router`
    l'enveloppe quand même en try/except par défense en profondeur.
    """
    if not isinstance(user_message, str) or not isinstance(assistant_text, str):
        return None
    if not assistant_text.strip():
        return None

    # 1. WhatsApp (intent-strict, court-circuit le plus rapide)
    wa = detect_rich_content_whatsapp(user_message, assistant_text)
    if wa is not None:
        return wa

    # 2. SMS (intent-strict, body cap 1600)
    sms = detect_rich_content_sms(user_message, assistant_text)
    if sms is not None:
        return sms

    # 3. Tweet (intent-strict, body cap 280 dur)
    tweet = detect_rich_content_tweet(user_message, assistant_text)
    if tweet is not None:
        return tweet

    # 4. LinkedIn (intent-strict, body cap 3000)
    linkedin = detect_rich_content_linkedin(user_message, assistant_text)
    if linkedin is not None:
        return linkedin

    # 5. Document long PDF AVANT Email — une lettre formelle (recipient
    # + closing formel + cap 200 chars minimum) cumule tous les markers
    # d'un email. Si Email était testé avant, on classerait à tort.
    document = detect_rich_content_document(user_message, assistant_text)
    if document is not None:
        return document

    # 6. Email (intent OU body markers semi-formels — Bonjour/Cordialement)
    email = detect_rich_content_email(user_message, assistant_text)
    if email is not None:
        return email

    return None


__all__ = [
    "DocumentDraftData",
    "EmailDraftData",
    "LinkedInPostDraftData",
    "RichContentKind",
    "RichContentPayload",
    "SmsDraftData",
    "TweetDraftData",
    "WhatsAppDraftData",
    "detect_document_intent",
    "detect_email_intent",
    "detect_linkedin_intent",
    "detect_rich_content",
    "detect_rich_content_document",
    "detect_rich_content_email",
    "detect_rich_content_linkedin",
    "detect_rich_content_sms",
    "detect_rich_content_tweet",
    "detect_rich_content_whatsapp",
    "detect_sms_intent",
    "detect_tweet_intent",
    "detect_whatsapp_intent",
]
