"""
Détecteur de brouillon LinkedIn post (C4.5).

Use case : « rédige-moi un post LinkedIn pour annoncer ma promotion »,
« écris un post pour partager mon nouveau projet », « génère un post
LinkedIn pour célébrer 5 ans dans l'entreprise ».

Pas de destinataire : un post LinkedIn est publié sur le mur de
l'utilisateur (publique ou cercle pro), pas envoyé à un contact.

Pipeline cascade aligné email mais avec body markers PRO-spécifiques :
  1. `detect_linkedin_intent(user_message)` — scan keywords FR + EN.
  2. `detect_linkedin_body(assistant_text)` — markers structurels
     (hashtags `#xxx`, mentions `@xxx`, structure post type
     « Aujourd'hui je suis fier de... »).
  3. Combinaison :
     - `intent_match` → flag direct (confiance HAUTE)
     - `body_match ∧ ¬intent` → SKIP (un text avec hashtags pourrait
       être un blog post, on évite faux positif)

Cap body 3000 chars (limite officielle LinkedIn 2026).
"""

from __future__ import annotations

import re
from typing import Pattern

from app.features.rich_content.schemas import RichContentPayload

# Cap body LinkedIn aligné cap schéma LinkedInPostDraftData (3000).
_LINKEDIN_BODY_MAX_CHARS = 3_000

# ── INTENT — message user upstream ────────────────────────────────────

_INTENT_PATTERNS_FR: tuple[Pattern[str], ...] = (
    # « rédige un post LinkedIn » / « écris une publication LinkedIn »
    re.compile(
        r"\b(rédige|redige|écris|ecris|écrire|ecrire|rédiger|rediger|prépare|prepare|"
        r"crée|cree|génère|genere|tape|tapes|publie|publier)\b[^.\n]{0,60}?"
        r"\b(post|publication|article)?\s*\b(linkedin|linked\s*in)\b",
        re.IGNORECASE,
    ),
    # « post LinkedIn pour... » / « publication LinkedIn de... »
    re.compile(
        r"\b(post|publication|article)\s+linkedin\b",
        re.IGNORECASE,
    ),
    # « LinkedIn de remerciement / d'annonce / de présentation »
    re.compile(
        r"\blinkedin\s+(de|d'|pour\s+(annoncer|présenter|partager|célébrer))\b",
        re.IGNORECASE,
    ),
)

_INTENT_PATTERNS_EN: tuple[Pattern[str], ...] = (
    # "write a LinkedIn post" / "draft a LinkedIn article"
    re.compile(
        r"\b(write|draft|compose|prepare|create|publish|post)\b[^.\n]{0,60}?"
        r"\b(post|article|publication)?\s*\blinkedin\b",
        re.IGNORECASE,
    ),
    # "LinkedIn post to announce..." / "LinkedIn article about..."
    re.compile(
        r"\blinkedin\s+(post|article|update)\b",
        re.IGNORECASE,
    ),
)


def detect_linkedin_intent(user_message: str) -> bool:
    """Scan keywords FR + EN. True si l'user demande clairement un post LinkedIn.

    Strict : exige le mot `LinkedIn` (variations `linkedin`, `Linked in`,
    `linked in` acceptées via patterns insensitive). Pas de matching
    sur `LI` ou `linked` seul (trop ambigus).
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return False

    for pattern in _INTENT_PATTERNS_FR + _INTENT_PATTERNS_EN:
        if pattern.search(user_message):
            return True
    return False


def detect_rich_content_linkedin(user_message: str, assistant_text: str) -> dict | None:
    """Point d'entrée — détection intent-based exclusive.

    Pas de scan body autonome : un texte avec des hashtags pourrait
    être un blog post ou un tweet. On exige l'intent explicite pour
    distinguer LinkedIn (= post professionnel long, 3000 chars) de
    Tweet (= 280 chars court) et de Email (= structure subject+body).

    Si intent OK :
      - Cap body à 3000 chars (cohérent schéma + limite LinkedIn 2026)
      - Wrapping dans `RichContentPayload.linkedin_post`
    """
    if not detect_linkedin_intent(user_message):
        return None

    if not isinstance(assistant_text, str):
        return None
    text = assistant_text.strip()
    if len(text) < 30:
        # Post LinkedIn trop court pour être crédible
        return None

    # Cap body à 3000 chars, tronque proprement sur dernier saut de ligne.
    body = text[:_LINKEDIN_BODY_MAX_CHARS]
    if len(text) > _LINKEDIN_BODY_MAX_CHARS:
        last_newline = body.rfind("\n")
        if last_newline > int(_LINKEDIN_BODY_MAX_CHARS * 0.9):
            body = body[:last_newline]

    try:
        structured = RichContentPayload.linkedin_post(body=body)
    except Exception:  # noqa: BLE001
        return None

    return structured.model_dump()
