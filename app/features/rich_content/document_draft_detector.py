"""
Détecteur de brouillon document long (C4.5).

Cas d'usage majeurs :
  - **Lettre formelle** : « rédige-moi une lettre au maire de Yaoundé
    pour demander un acte de naissance »
  - **Rapport** : « rédige un rapport de réunion », « compte-rendu de
    stage »
  - **Cours / mémo long** : « génère un cours sur les boucles for en
    Python » (10+ paragraphes structurés)
  - **Discours** : « écris-moi un discours pour mon mariage »

Cap body 50 000 chars (~10 pages PDF A4 dense). Coût Africa-first :
50 000 chars → PDF ~500 KB raisonnable 2G/3G. Au-delà = friction
partage WhatsApp/Email.

Pipeline cascade (le plus complexe des 6 détecteurs) :
  1. `detect_document_intent(user_message)` — scan keywords FR + EN
     (`rédige une lettre`, `génère un cours`, `write a report`).
  2. `detect_formal_letter_body(assistant_text)` — markers structurels
     formels (`Madame,` / `Monsieur,` + `Veuillez agréer` / `Cordialement`
     + entête formel optionnel).
  3. Combinaison :
     - `intent_match ∧ body_match` → flag confiance HAUTE
     - `body_match ∧ recipient extracted` → flag confiance MOYENNE
     - sinon → None (un texte long sans markers formels = blog/article,
       pas un document actionnable PDF)

Le cap est REQUIS : pour les cours longs (50 paragraphes) le LLM peut
facilement générer 30k+ chars. Le détecteur cape proprement à 50k pour
le PDF.
"""

from __future__ import annotations

import re
from typing import Pattern

from app.features.rich_content.schemas import RichContentPayload

# Cap body Document aligné cap schéma DocumentDraftData (50000).
_DOCUMENT_BODY_MAX_CHARS = 50_000

# Seuil minimum body pour qu'une carte document soit crédible.
# Une lettre formelle courte fait ~300-500 chars (entête + corps + politesse),
# au-dessous = trop fragmentaire pour mériter une carte actionnable PDF.
_DOCUMENT_BODY_MIN_CHARS = 200

# Patterns méta-questions FR+EN — l'user pose une question SUR un type
# de document, pas une demande de rédaction.
_META_QUESTION_PATTERNS = (
    re.compile(r"^\s*(comment|comment\s+(?:écrire|rédiger|faire))\b", re.IGNORECASE),
    re.compile(r"^\s*(qu'?est-ce\s+que|qu'?est\s+ce\s+que|c'?est\s+quoi)\b", re.IGNORECASE),
    re.compile(r"^\s*(pourquoi|à\s+quoi\s+sert)\b", re.IGNORECASE),
    re.compile(r"^\s*(how\s+(?:do|to|can|does)|how\s+would)\b", re.IGNORECASE),
    re.compile(r"^\s*(what\s+(?:is|are|does|do))\b", re.IGNORECASE),
    re.compile(r"^\s*(why\s+(?:do|does|is|are))\b", re.IGNORECASE),
)


def _is_meta_question(user_message: str) -> bool:
    """True si l'user pose une question MÉTA (comment / qu'est-ce que / how).

    Sert à filtrer « Comment écrire une lettre formelle ? » qui matcherait
    sinon le pattern intent (`écrire` + `lettre`) alors que l'user demande
    une explication, pas une rédaction.
    """
    if not isinstance(user_message, str):
        return False
    text = user_message.strip()
    if not text:
        return False
    for pattern in _META_QUESTION_PATTERNS:
        if pattern.search(text):
            return True
    return False

# ── INTENT — message user upstream ────────────────────────────────────

_INTENT_PATTERNS_FR: tuple[Pattern[str], ...] = (
    # « rédige-moi une lettre » / « écris-moi un courrier officiel »
    re.compile(
        r"\b(rédige|redige|écris|ecris|écrire|ecrire|rédiger|rediger|prépare|prepare|"
        r"crée|cree|génère|genere|tape|tapes|produis|produire)\b[^.\n]{0,80}?"
        r"\b(lettre|courrier(?:\s+officiel)?|courrier\s+formel|"
        r"rapport|compte[\s-]?rendu|mémo|memo|note\s+(?:de\s+service|interne)|"
        r"discours|cours\s+(?:sur|de|détaillé)|tutoriel|guide\s+(?:complet|détaillé)|"
        r"document(?:\s+officiel)?|pdf|article\s+(?:de\s+fond|détaillé))\b",
        re.IGNORECASE,
    ),
    # « génère-moi un PDF » / « produis un document PDF »
    re.compile(
        r"\b(génère|genere|produis|produire|crée|cree|fais|fais-moi|fais-en)\b[^.\n]{0,40}?"
        r"\b(pdf|document\s+(?:long|complet|formel)|fichier\s+(?:texte|pdf))\b",
        re.IGNORECASE,
    ),
    # « lettre à mon employeur » / « courrier au maire »
    re.compile(
        r"\b(lettre|courrier|courrier\s+officiel)\b\s+(à|a|au|aux|pour|destiné|destine)\b",
        re.IGNORECASE,
    ),
)

_INTENT_PATTERNS_EN: tuple[Pattern[str], ...] = (
    # "write a formal letter" / "draft a report" / "generate a course"
    re.compile(
        r"\b(write|draft|compose|prepare|create|generate|produce)\b[^.\n]{0,80}?"
        r"\b(formal\s+letter|letter|official\s+(?:letter|document)|"
        r"report|memo|memorandum|speech|"
        r"detailed\s+(?:course|tutorial|guide)|long\s+document|pdf|document)\b",
        re.IGNORECASE,
    ),
    # "letter to my employer" / "report for the meeting"
    re.compile(
        r"\b(letter|formal\s+letter|report)\b\s+(to|for|addressed\s+to)\b",
        re.IGNORECASE,
    ),
)

# ── BODY MARKERS — réponse assistante (lettre formelle) ───────────────

# Entête formelle FR — « Madame, » / « Monsieur, » / « Madame, Monsieur, »
# / « Madame la Maire, »
_FORMAL_GREETING_FR = re.compile(
    r"^\s*(?:\*\*)?"
    r"(Madame|Monsieur|Madame[\s,]+Monsieur|Mesdames|Messieurs|"
    r"Madame\s+(?:la|le)\s+\w+|Monsieur\s+(?:le|la)\s+\w+|Cher|Chère|Chers)\b",
    re.IGNORECASE | re.MULTILINE,
)
_FORMAL_GREETING_EN = re.compile(
    r"^\s*(?:\*\*)?"
    r"(Dear\s+(?:Sir|Madam|Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Sir\s+or\s+Madam)|"
    r"To\s+Whom\s+It\s+May\s+Concern)\b",
    re.IGNORECASE | re.MULTILINE,
)

# Formule de politesse FR — « Veuillez agréer... » / « Je vous prie de... »
_FORMAL_CLOSING_FR = re.compile(
    r"\b(Veuillez\s+agréer|Je\s+vous\s+prie\s+(?:de\s+(?:bien\s+vouloir|croire)|d'agréer)|"
    r"Je\s+vous\s+prie\s+d'agréer|Recevez,\s+(?:Madame|Monsieur)|"
    r"Avec\s+(?:mes|nos)\s+(?:respectueuses|sincères|cordiales)\s+salutations|"
    r"Salutations\s+(?:distinguées|respectueuses)|"
    r"Dans\s+l'attente\s+(?:de\s+votre\s+(?:réponse|retour)|de\s+vous\s+lire))\b",
    re.IGNORECASE,
)
_FORMAL_CLOSING_EN = re.compile(
    r"\b(Yours\s+(?:sincerely|faithfully|truly)|Sincerely\s+yours|"
    r"Looking\s+forward\s+to\s+(?:your\s+(?:response|reply)|hearing\s+from\s+you)|"
    r"Respectfully\s+(?:yours)?|With\s+(?:kind|best)\s+regards)\b",
    re.IGNORECASE,
)

# Entête « Objet : » formel (typique lettre administrative FR)
_FORMAL_SUBJECT_FR = re.compile(
    r"^\s*(?:\*\*)?(Objet|Sujet)\s*:\s*(.+?)$",
    re.IGNORECASE | re.MULTILINE,
)


def detect_document_intent(user_message: str) -> bool:
    """Scan keywords FR + EN. True si l'user demande un document long.

    Conservateur strict : exige un verbe d'action (`rédige`, `écris`,
    `génère`, `produis`, `write`, `draft`, `generate`) collé à un type
    de document long (`lettre`, `rapport`, `cours détaillé`, `tutoriel`,
    `discours`, `PDF`, etc.).

    Cas écartés :
    - « explique-moi en quelques lignes... » (court, pas un document)
    - « résume ce texte... » (output court, pas un document à formater)
    - « comment fonctionne X » (méta-question, pas une demande de
      rédaction).
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return False

    # Filtre méta-questions AVANT pattern matching pour éviter les faux
    # positifs sur « Comment écrire une lettre formelle ? ».
    if _is_meta_question(user_message):
        return False

    for pattern in _INTENT_PATTERNS_FR + _INTENT_PATTERNS_EN:
        if pattern.search(user_message):
            return True
    return False


def _extract_formal_recipient(text: str) -> str | None:
    """Cherche un entête formel et extrait le destinataire si possible.

    Patterns :
    - « Madame la Maire de Yaoundé, » → recipient = « Madame la Maire de Yaoundé »
    - « Monsieur le Directeur, » → recipient = « Monsieur le Directeur »
    - « Dear Sir or Madam, » → recipient = « Dear Sir or Madam »

    Retourne `None` si pas d'entête formelle trouvée.
    """
    for pattern in (_FORMAL_GREETING_FR, _FORMAL_GREETING_EN):
        # Cherche l'entête + capture jusqu'à la virgule terminale.
        m = re.search(
            pattern.pattern + r"[^,\n]{0,80}?\s*,",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        if m is None:
            continue
        # On capture le segment complet jusqu'à la virgule.
        full_match = m.group(0).rstrip(",").strip()
        # Strip markdown bold résiduel.
        full_match = re.sub(r"^\*+|\*+$", "", full_match).strip()
        if full_match and len(full_match) <= 200:
            return full_match
    return None


def _extract_formal_subject(text: str) -> str | None:
    """Cherche une ligne `Objet : ...` et extrait la valeur.

    Retourne string trimmée 300 chars max, ou None.
    """
    m = _FORMAL_SUBJECT_FR.search(text)
    if m is None:
        return None
    value = m.group(m.lastindex or 0).strip()
    value = re.sub(r"^\*+|\*+$", "", value).strip()
    if value:
        return value[:300]
    return None


def detect_formal_letter_body(assistant_text: str) -> tuple[bool, dict | None]:
    """Scan la réponse pour markers formels (greeting + closing).

    Score :
      - greeting formel trouvé : +2
      - closing formel trouvé : +2
      - subject « Objet : » : +1

    Seuil : score ≥ 4 → document formel détecté.

    Retourne `(True, {title?, body, recipient?})` ou `(False, None)`.
    """
    if not isinstance(assistant_text, str):
        return False, None
    text = assistant_text.strip()
    if len(text) < _DOCUMENT_BODY_MIN_CHARS:
        # Document trop court pour être crédible (lettre formelle + cours)
        return False, None

    score = 0

    recipient = _extract_formal_recipient(text)
    if recipient:
        score += 2

    if _FORMAL_CLOSING_FR.search(text) or _FORMAL_CLOSING_EN.search(text):
        score += 2

    subject = _extract_formal_subject(text)
    if subject:
        score += 1

    if score >= 4:
        # Cap body à 50k chars (cohérent schema), tronque proprement
        # sur le dernier saut de ligne sous le cap.
        body = text[:_DOCUMENT_BODY_MAX_CHARS]
        if len(text) > _DOCUMENT_BODY_MAX_CHARS:
            last_newline = body.rfind("\n")
            if last_newline > int(_DOCUMENT_BODY_MAX_CHARS * 0.95):
                body = body[:last_newline]
        return True, {"title": subject, "body": body, "recipient": recipient}

    return False, None


def detect_rich_content_document(user_message: str, assistant_text: str) -> dict | None:
    """Point d'entrée — combine intent + body detection.

    Retourne un dict conforme à `RichContentPayload` (sérialisé), ou None.

    Confidence levels :
      - `intent_match ∧ body_match` (formel) → flag (cas standard)
      - `body_match ∧ ¬intent_match ∧ recipient extracted` → flag (cas
        où l'user dit « répond à cette lettre » et le LLM produit une
        lettre formelle structurée — le recipient extrait confirme)
      - `intent_match ∧ ¬body_match` → flag SANS markers formels MAIS
        avec cap body raisonnable (≥ 500 chars) — les cours/tutoriels
        n'ont pas de "Madame/Monsieur", uniquement structure markdown.
      - sinon → SKIP

    Le retour est un dict prêt pour `metadata_json["rich_content"]`.
    """
    if not isinstance(assistant_text, str):
        return None
    text = assistant_text.strip()
    if len(text) < _DOCUMENT_BODY_MIN_CHARS:
        # Sous le seuil minimal, le PDF serait cosmétiquement ridicule
        # (moins qu'1/4 de page A4). Les vraies lettres formelles font
        # 300-500 chars minimum (entête + corps + politesse).
        return None

    body_match, payload = detect_formal_letter_body(assistant_text)
    intent_match = detect_document_intent(user_message)

    # Cas 1 : intent + body markers → confiance haute (lettre formelle)
    # Cas 2 : body markers + recipient → confiance moyenne (réponse à lettre)
    # Cas 3 : intent SANS body markers → cours/rapport/tutoriel structuré
    if not intent_match and not body_match:
        return None

    if intent_match and not body_match:
        # Cours/rapport sans formules formelles. On prend le texte tel quel
        # sans recipient ni title extrait (l'user complétera dans la card).
        body = text[:_DOCUMENT_BODY_MAX_CHARS]
        if len(text) > _DOCUMENT_BODY_MAX_CHARS:
            last_newline = body.rfind("\n")
            if last_newline > int(_DOCUMENT_BODY_MAX_CHARS * 0.95):
                body = body[:last_newline]
        payload = {"title": None, "body": body, "recipient": None}
    elif body_match and not intent_match:
        # Body formel sans intent explicite : on exige recipient extrait
        # pour confirmer (un texte avec "Cordialement" SANS "Madame/Monsieur"
        # est trop ambigu = blog post).
        if payload is None or not payload.get("recipient"):
            return None
        # payload conservé tel quel
    # else : cas 1 (intent + body) — payload conservé

    if payload is None:
        return None

    try:
        structured = RichContentPayload.document(
            title=payload.get("title"),
            body=payload["body"],
            recipient=payload.get("recipient"),
        )
    except Exception:  # noqa: BLE001
        return None

    return structured.model_dump()
