"""
Détecteur de brouillon d'email (C4.4).

Heuristique conservatrice — préfère le faux négatif (texte qui ressemble
à un email mais qu'on ne flag pas → l'user voit un bloc texte normal)
au faux positif (texte qui n'est pas un email mais qu'on flag → l'user
voit une carte qui n'a aucun sens).

Pipeline cascade :
  1. `detect_email_intent(user_message)` — scan keywords FR + EN sur le
     message user qui a déclenché le tour. Boolean rapide (≤ 1 ms).
  2. `detect_email_body(assistant_text)` — scan markers structurés
     (subject line, greeting, closing) sur la réponse assistante.
     Retourne `(is_email, payload)` avec score 4+ requis.
  3. Combinaison :
     - `intent_match ∧ body_match` → confiance HAUTE → toujours flag
     - `body_match ∧ ¬intent_match ∧ subject extracted` → confiance
       MOYENNE → flag (l'extraction de subject est un signal très fort
       qu'on a un vrai email même sans intent explicite)
     - sinon → None
"""

from __future__ import annotations

import re
from typing import Pattern

from app.features.rich_content.schemas import RichContentPayload

# ── INTENT — message user upstream ────────────────────────────────────

_INTENT_PATTERNS_FR: tuple[Pattern[str], ...] = (
    # « rédige un mail » / « écrire un email à » / « génère un courriel »
    re.compile(
        r"\b(rédige|redige|écris|ecris|écrire|ecrire|rédiger|rediger|prépare|prepare|"
        r"crée|cree|génère|genere|tape|tapes)\b[^.\n]{0,60}?"
        r"\b(mail|email|courriel|courrier électronique|courrier electronique)\b",
        re.IGNORECASE,
    ),
    # « email à mon fournisseur » / « mail pour Marie »
    re.compile(
        r"\b(mail|email|courriel)\b\s+(à|a|au|pour|destiné|destine)\b",
        re.IGNORECASE,
    ),
    # « mail de relance » / « email de remerciement »
    re.compile(
        r"\b(mail|email|courriel)\b\s+de\s+"
        r"(relance|remerciement|remercîment|excuse|demande|confirmation|invitation)\b",
        re.IGNORECASE,
    ),
)

_INTENT_PATTERNS_EN: tuple[Pattern[str], ...] = (
    # "write an email" / "draft a mail" / "compose an email to"
    re.compile(
        r"\b(write|draft|compose|prepare|create|send)\b[^.\n]{0,60}?"
        r"\b(email|e-mail|mail)\b",
        re.IGNORECASE,
    ),
    # "email to my supplier" / "email for Marie"
    re.compile(
        r"\b(email|e-mail|mail)\s+(to|for)\b",
        re.IGNORECASE,
    ),
)

# ── BODY MARKERS — réponse assistante ─────────────────────────────────

# Subject line — souvent en début de ligne après normalisation
_SUBJECT_PATTERN_FR = re.compile(
    r"^\s*(?:\*\*)?(Objet|Sujet)(?:\*\*)?\s*:\s*(.+?)$",
    re.IGNORECASE | re.MULTILINE,
)
_SUBJECT_PATTERN_EN = re.compile(
    r"^\s*(?:\*\*)?Subject(?:\*\*)?\s*:\s*(.+?)$",
    re.IGNORECASE | re.MULTILINE,
)

# Greeting — début de paragraphe
_GREETING_PATTERN_FR = re.compile(
    r"\b(Bonjour|Cher|Chère|Madame|Monsieur|Salut|Coucou)\b"
    r"(\s+[A-ZÀ-Ý][\w\s\-]{0,40})?\s*[,\n]",
)
_GREETING_PATTERN_EN = re.compile(
    r"\b(Dear|Hello|Hi|Greetings)\b(\s+[A-Z][\w\s\-]{0,30})?\s*[,\n]",
)

# Closing — phrases de fin
_CLOSING_PATTERN_FR = re.compile(
    r"\b(Cordialement|Bien à vous|Bien a vous|Bien cordialement|"
    r"Sincères salutations|Sinceres salutations|Salutations distinguées|"
    r"Salutations distinguees|Salutations|Respectueusement|Très cordialement|"
    r"Tres cordialement|À bientôt|A bientot|Bien sincèrement|Bien sincerement)\b",
    re.IGNORECASE,
)
_CLOSING_PATTERN_EN = re.compile(
    r"\b(Best regards|Sincerely|Kind regards|Yours sincerely|Best wishes|"
    r"Yours truly|Regards|Cheers|Looking forward)\b",
    re.IGNORECASE,
)


def detect_email_intent(user_message: str) -> bool:
    """Scan keywords FR + EN. Retourne True si l'user demande clairement un email.

    Conservateur : exige un verbe d'action (`rédige`, `écris`, `write`,
    `draft`, etc.) suivi d'un mot-clé email à ≤ 60 chars, OU la collocation
    directe `email à <X>` / `email to <X>`, OU un nominal type `email de
    relance`.

    Cas écartés volontairement (faux positifs probables) :
    - « j'ai reçu un email » (passé, pas de demande de rédaction)
    - « comment fonctionnent les emails » (méta-question)
    - « explique-moi le protocole SMTP » (technique, pas rédactionnel)
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return False

    for pattern in _INTENT_PATTERNS_FR + _INTENT_PATTERNS_EN:
        if pattern.search(user_message):
            return True
    return False


def _extract_subject(text: str) -> str | None:
    """Cherche une ligne `Sujet: ...` ou `Subject: ...` et extrait la valeur.

    Strip markdown bold/italique sur le label, accepte `Objet`/`Sujet` (FR)
    et `Subject` (EN). Tronque la valeur à 300 chars (cap schema).
    """
    for pattern in (_SUBJECT_PATTERN_FR, _SUBJECT_PATTERN_EN):
        m = pattern.search(text)
        if m is None:
            continue
        # FR pattern has 2 groups (label + value), EN has 1 (value only)
        # value is always the last group
        value = m.group(m.lastindex or 0).strip()
        # Strip markdown bold around the value
        value = re.sub(r"^\*+|\*+$", "", value).strip()
        if value:
            return value[:300]
    return None


def detect_email_body(assistant_text: str) -> tuple[bool, dict | None]:
    """Scan la réponse assistante pour les 3 markers (subject, greeting, closing).

    Score :
      - subject extrait : +2
      - greeting trouvé : +2
      - closing trouvé : +2

    Seuil : score ≥ 4 → email détecté.

    Retourne `(True, {"subject": str | None, "body": full_text, "to": None})`
    si détecté, sinon `(False, None)`.

    `body` = le texte assistant complet (l'user édite ensuite dans l'UI).
    Pour V1 on ne tente PAS de « parser » le body pour en retirer le
    subject ou la signature — l'expérience utilisateur est de voir TOUT
    le contenu rédigé par le LLM, prêt à l'usage.
    """
    if not isinstance(assistant_text, str):
        return False, None
    text = assistant_text.strip()
    if len(text) < 50:
        # Email trop court pour être crédible (greeting + closing minimum)
        return False, None

    score = 0
    subject = _extract_subject(text)
    if subject:
        score += 2

    if _GREETING_PATTERN_FR.search(text) or _GREETING_PATTERN_EN.search(text):
        score += 2

    if _CLOSING_PATTERN_FR.search(text) or _CLOSING_PATTERN_EN.search(text):
        score += 2

    if score >= 4:
        # Cap body à 10k chars (cohérent avec schema), tronque proprement
        # sur le dernier saut de ligne sous le cap pour éviter de couper
        # un mot en plein milieu.
        body = text[:10_000]
        if len(text) > 10_000:
            last_newline = body.rfind("\n")
            if last_newline > 9_000:
                body = body[:last_newline]
        return True, {"subject": subject, "body": body, "to": None}

    return False, None


def detect_rich_content_email(user_message: str, assistant_text: str) -> dict | None:
    """Point d'entrée — combine intent + body detection.

    Retourne un dict conforme à `RichContentPayload` (sérialisé), ou None.

    Confidence levels (V1) :
      - `intent_match ∧ body_match` → flag (cas standard)
      - `body_match ∧ ¬intent_match ∧ subject` → flag (auto-détection
        sur un message user ambigu type « peux-tu m'aider ? »)
      - `body_match ∧ ¬intent_match ∧ ¬subject` → SKIP (trop ambigu —
        une réponse avec greeting + closing mais sans subject peut être
        une lettre formelle, un discours, un faire-part. On évite le
        faux positif au prix d'un faux négatif).
      - `¬body_match` → SKIP (toujours)

    Le retour est un `dict` prêt pour `metadata_json["rich_content"]`.
    """
    body_match, payload = detect_email_body(assistant_text)
    if not body_match or payload is None:
        return None

    intent_match = detect_email_intent(user_message)
    has_subject = payload.get("subject") is not None

    if intent_match or has_subject:
        # Validation Pydantic stricte (cap, format) avant retour
        try:
            structured = RichContentPayload.email(
                subject=payload.get("subject"),
                body=payload["body"],
                to=payload.get("to"),
            )
        except Exception:  # noqa: BLE001
            # Validation Pydantic a échoué (très improbable car on a
            # déjà capé côté détecteur). Fail-safe → pas de carte plutôt
            # qu'une exception qui casserait la finalisation chat.
            return None
        return structured.model_dump()

    return None
