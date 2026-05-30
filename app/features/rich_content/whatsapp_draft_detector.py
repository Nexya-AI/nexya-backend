"""
Détecteur de brouillon WhatsApp (C4.4).

WhatsApp = canal #1 communication pro Afrique francophone (use case
prioritaire Ivan). Différences vs email :

  - **Body court et informel** : pas de subject, pas de greeting formel
    obligatoire (« Slt frère », « Bonjour Madame Nkamga », « Hi »),
    pas de closing formel (« A+ », « Merci »).
  - **Markers structurels faibles** : impossible de détecter
    avec confiance sur le body seul → on s'appuie **majoritairement
    sur l'intent user upstream**.

Pipeline :
  1. `detect_whatsapp_intent(user_message)` — scan keywords FR + EN.
     Si False → return None (pas de détection sans intent explicite).
  2. Si intent True → take assistant text comme body (cap 10k chars).

Conservateur : le risque est de transformer en carte WhatsApp un texte
qui n'en est pas un (faux positif gênant car la carte propose
« Ouvrir WhatsApp » sur un texte inadapté). Donc intent strict obligatoire.
"""

from __future__ import annotations

import re
from typing import Pattern

from app.features.rich_content.schemas import RichContentPayload

# ── INTENT — message user upstream ────────────────────────────────────

_INTENT_PATTERNS_FR: tuple[Pattern[str], ...] = (
    # « rédige un message WhatsApp » / « écris un WhatsApp à Marie »
    re.compile(
        r"\b(rédige|redige|écris|ecris|écrire|ecrire|rédiger|rediger|prépare|prepare|"
        r"crée|cree|génère|genere|tape|tapes)\b[^.\n]{0,60}?"
        r"\b(message\s+)?WhatsApp\b",
        re.IGNORECASE,
    ),
    # « message WhatsApp à mon client » / « WhatsApp pour Marie »
    re.compile(
        r"\bWhatsApp\b\s+(à|a|au|pour)\b",
        re.IGNORECASE,
    ),
    # « message WhatsApp de relance/remerciement »
    re.compile(
        r"\bWhatsApp\b\s+de\s+"
        r"(relance|remerciement|remercîment|excuse|demande|confirmation|invitation)\b",
        re.IGNORECASE,
    ),
)

_INTENT_PATTERNS_EN: tuple[Pattern[str], ...] = (
    # "write a WhatsApp message" / "draft a WhatsApp to Marie"
    re.compile(
        r"\b(write|draft|compose|prepare|create|send)\b[^.\n]{0,60}?"
        r"\b(WhatsApp|whatsapp)(\s+message)?\b",
        re.IGNORECASE,
    ),
    # "WhatsApp to my client"
    re.compile(
        r"\bWhatsApp\b\s+(to|for)\b",
        re.IGNORECASE,
    ),
)


def detect_whatsapp_intent(user_message: str) -> bool:
    """Scan keywords FR + EN. Retourne True si l'user demande clairement un WhatsApp.

    Strict : exige le mot `WhatsApp` (sensible casse à priori mais on
    accepte la variation `whatsapp` en minuscules via les patterns).
    Pas de matching sur `WA`, `wa`, `whatsap` (trop ambigus, faux
    positifs probables).
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return False

    for pattern in _INTENT_PATTERNS_FR + _INTENT_PATTERNS_EN:
        if pattern.search(user_message):
            return True
    return False


def detect_rich_content_whatsapp(user_message: str, assistant_text: str) -> dict | None:
    """Point d'entrée — détection intent-based exclusive.

    Pas de scan body car les markers WhatsApp sont trop faibles pour
    une détection autonome fiable. Si l'user n'a pas explicitement
    demandé un WhatsApp, on ne propose pas de carte.

    Si intent OK :
      - Cap body à 10k chars
      - phone = None (l'user complète après tap "Ouvrir WhatsApp")
      - Wrapping dans `RichContentPayload.whatsapp` pour validation
        Pydantic stricte (cap, format)
    """
    if not detect_whatsapp_intent(user_message):
        return None

    if not isinstance(assistant_text, str):
        return None
    text = assistant_text.strip()
    if len(text) < 10:
        # Message trop court pour être un vrai brouillon
        return None

    # Cap body à 10k chars (cohérent schema), tronque sur dernier saut
    # de ligne sous le cap pour éviter de couper en plein milieu.
    body = text[:10_000]
    if len(text) > 10_000:
        last_newline = body.rfind("\n")
        if last_newline > 9_000:
            body = body[:last_newline]

    try:
        structured = RichContentPayload.whatsapp(phone=None, body=body)
    except Exception:  # noqa: BLE001
        # Validation Pydantic a échoué (très improbable car on a déjà
        # capé). Fail-safe → pas de carte plutôt qu'une exception qui
        # casserait la finalisation chat.
        return None

    return structured.model_dump()
