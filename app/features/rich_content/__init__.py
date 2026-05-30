"""
Module `rich_content` (C4.4 — NxDraftCard email + WhatsApp).

Détection automatique de **contenu rédactionnel actionnable** dans la réponse
LLM (brouillons d'email, messages WhatsApp). Quand la détection réussit, le
caller (`chat/router._finalize_in_fresh_session`) injecte un payload structuré
dans `messages.metadata_json.rich_content` qui survit à la réouverture de la
conversation et permet au client Flutter de rendre la carte `NxDraftCard` au
lieu d'un bloc texte brut.

V1 cascade de détection :
  1. **Heuristique markers** sur la réponse assistante finalisée
     (« Sujet : », « Bonjour <Nom>, », « Cordialement », etc.).
  2. **Intent classifier** sur le message user upstream
     (« rédige un mail », « write an email to », etc.).
  3. Combinaison `intent_match ∧ body_match` → confiance haute.
     `body_match` seul avec subject extrait → confiance moyenne.

V2 (différé) : LLM tool call `draft_email({to?, subject, body})` qui produit
une carte 100 % structurée sans heuristique. Architecture compatible (le tool
result alimenterait directement `RichContentPayload`).

Aucun appel LLM, aucun I/O, aucun side-effect — le module est pur Python
synchrone, testable en isolation sans fixture réseau ni DB.
"""

from __future__ import annotations

from app.features.rich_content.email_draft_detector import (
    detect_email_intent,
    detect_rich_content_email,
)
from app.features.rich_content.schemas import (
    EmailDraftData,
    RichContentKind,
    RichContentPayload,
    WhatsAppDraftData,
)
from app.features.rich_content.whatsapp_draft_detector import (
    detect_rich_content_whatsapp,
    detect_whatsapp_intent,
)


def detect_rich_content(user_message: str, assistant_text: str) -> dict | None:
    """Point d'entrée principal — tente WhatsApp puis email.

    Ordre choisi :
      1. **WhatsApp d'abord** — détection majoritairement intent-based
         (le body WhatsApp n'a pas de markers formels type « Sujet »),
         court-circuit rapide si l'user n'a pas parlé de WhatsApp.
      2. **Email ensuite** — heuristique markers + intent combinés, coût
         de scan supérieur (5+ regex sur le body assistant).

    Retourne un `dict` prêt à être stocké dans
    `messages.metadata_json["rich_content"]` (format conforme à
    `RichContentPayload`), ou `None` si aucun draft détecté.

    Fail-safe : exception dans un détecteur → log warning + skip à
    l'extérieur. Ici la fonction est pure, les détecteurs sont défensifs
    sur les inputs (empty string, None coerce, etc.) — pas besoin de
    try/except global. Le caller chat l'enveloppe quand même par défense
    en profondeur.
    """
    if not isinstance(user_message, str) or not isinstance(assistant_text, str):
        return None
    if not assistant_text.strip():
        return None

    wa = detect_rich_content_whatsapp(user_message, assistant_text)
    if wa is not None:
        return wa

    email = detect_rich_content_email(user_message, assistant_text)
    if email is not None:
        return email

    return None


__all__ = [
    "EmailDraftData",
    "RichContentKind",
    "RichContentPayload",
    "WhatsAppDraftData",
    "detect_email_intent",
    "detect_rich_content",
    "detect_rich_content_email",
    "detect_rich_content_whatsapp",
    "detect_whatsapp_intent",
]
