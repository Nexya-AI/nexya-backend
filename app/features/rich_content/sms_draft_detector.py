"""
Détecteur de brouillon SMS (C4.5).

SMS = canal de communication pro/perso Afrique francophone qui résiste
quand WhatsApp ne marche pas (réseau 2G, opérateur Orange CM/MTN CM avec
data coupée, destinataire sans WhatsApp installé). Use case Africa-first.

Différences vs email :
  - **Body très court** : cap 1600 chars (10 segments SMS de 160).
  - **Pas de subject** : SMS n'a pas de sujet.
  - **Markers structurels faibles** : impossible de détecter
    sur le body seul → on s'appuie majoritairement sur l'intent user.

Pipeline aligné WhatsApp (C4.4) :
  1. `detect_sms_intent(user_message)` — scan keywords FR + EN.
     Si False → return None (pas de détection sans intent explicite).
  2. Si intent True → take assistant text comme body (cap 1600 chars).

Conservateur strict : on transforme un texte en carte SMS UNIQUEMENT
sur intent explicite. Risque accepté : faux négatif (texte que l'user
voulait envoyer en SMS mais qu'on n'a pas flag) → l'user peut toujours
copier-coller manuellement. Risque refusé : faux positif (carte SMS
sur un texte inadapté) → friction UX.
"""

from __future__ import annotations

import re
from typing import Pattern

from app.features.rich_content.schemas import RichContentPayload

# Cap body SMS aligné cap schéma SmsDraftData (1600 chars = 10 segments).
_SMS_BODY_MAX_CHARS = 1_600

# ── INTENT — message user upstream ────────────────────────────────────

_INTENT_PATTERNS_FR: tuple[Pattern[str], ...] = (
    # « rédige un SMS » / « écris un texto » / « envoie un message texte »
    re.compile(
        r"\b(rédige|redige|écris|ecris|écrire|ecrire|rédiger|rediger|prépare|prepare|"
        r"crée|cree|génère|genere|tape|tapes|envoie|envoyer)\b[^.\n]{0,60}?"
        r"\b(sms|texto|message\s+texte)\b",
        re.IGNORECASE,
    ),
    # « SMS à mon client » / « texto pour Marie »
    re.compile(
        r"\b(sms|texto)\b\s+(à|a|au|pour|destiné|destine)\b",
        re.IGNORECASE,
    ),
    # « SMS de relance/rappel/remerciement »
    re.compile(
        r"\b(sms|texto|message\s+texte)\b\s+de\s+"
        r"(relance|remerciement|remercîment|excuse|demande|"
        r"confirmation|invitation|rappel)\b",
        re.IGNORECASE,
    ),
)

_INTENT_PATTERNS_EN: tuple[Pattern[str], ...] = (
    # "write an SMS" / "send a text message" / "draft a text"
    re.compile(
        r"\b(write|draft|compose|prepare|create|send)\b[^.\n]{0,60}?"
        r"\b(sms|text\s+message|text)\b",
        re.IGNORECASE,
    ),
    # "SMS to my client" / "text message for Marie"
    re.compile(
        r"\b(sms|text\s+message)\s+(to|for)\b",
        re.IGNORECASE,
    ),
)


def detect_sms_intent(user_message: str) -> bool:
    """Scan keywords FR + EN. True si l'user demande clairement un SMS.

    Conservateur strict : exige un mot-clé SMS (`sms`, `texto`,
    `message texte`, `text message`, `text` collocation) collé à un
    verbe d'action ou à une préposition de destination.

    Cas écartés volontairement (faux positifs probables) :
    - « j'ai reçu un SMS » (passé, pas de demande de rédaction)
    - « comment fonctionnent les SMS » (méta-question)
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return False

    for pattern in _INTENT_PATTERNS_FR + _INTENT_PATTERNS_EN:
        if pattern.search(user_message):
            return True
    return False


def detect_rich_content_sms(user_message: str, assistant_text: str) -> dict | None:
    """Point d'entrée — détection intent-based exclusive (aligné WhatsApp).

    Pas de scan body car les markers SMS sont trop faibles pour une
    détection autonome fiable. Si l'user n'a pas explicitement demandé
    un SMS, on ne propose pas de carte.

    Si intent OK :
      - Cap body à 1600 chars (cohérent schéma)
      - phone = None (l'user complète après tap « 📱 Envoyer SMS »)
      - Wrapping dans `RichContentPayload.sms` pour validation
        Pydantic stricte (cap, format)

    Retourne `dict` prêt pour `metadata_json["rich_content"]`, ou `None`.
    """
    if not detect_sms_intent(user_message):
        return None

    if not isinstance(assistant_text, str):
        return None
    text = assistant_text.strip()
    if len(text) < 10:
        # Message trop court pour être un vrai brouillon SMS
        return None

    # Cap body à 1600 chars (cohérent schéma), tronque sur dernier saut
    # de ligne sous le cap pour éviter de couper en plein milieu.
    body = text[:_SMS_BODY_MAX_CHARS]
    if len(text) > _SMS_BODY_MAX_CHARS:
        last_newline = body.rfind("\n")
        if last_newline > int(_SMS_BODY_MAX_CHARS * 0.9):
            body = body[:last_newline]

    try:
        structured = RichContentPayload.sms(phone=None, body=body)
    except Exception:  # noqa: BLE001
        # Validation Pydantic a échoué (très improbable car on a déjà
        # capé). Fail-safe → pas de carte plutôt qu'une exception qui
        # casserait la finalisation chat.
        return None

    return structured.model_dump()
