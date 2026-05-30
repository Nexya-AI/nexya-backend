"""
Détecteur de brouillon Tweet / X post (C4.5).

Use case : « rédige-moi un tweet pour réagir à cette news », « écris
un tweet drôle sur Flutter », « génère un tweet de support pour mon
ami qui lance sa startup ».

Pas de destinataire : un tweet est publié sur le profil de l'user.

Cap STRICT 280 chars (limite officielle Twitter/X 2026, hors abonnés
Premium qui ont 25000 chars — cas marginal non supporté V1).

Pipeline :
  1. `detect_tweet_intent(user_message)` — scan keywords FR + EN
     (`tweet`, `tweeter`, `tweet`, `X`, `poster sur X`).
     **Cas piège** : le mot « X » est ambigu (lettre alphabet, variable
     math). Patterns exigent collocation forte :
     - `un X` / `sur X` / `poster sur X` / `publier sur X` autorisé
     - `X` isolé sans collocation REFUSÉ (faux positif probable)
  2. Si intent True → take assistant text comme body avec cap 280.

Note importante sur le cap : un tweet de 281+ chars est REFUSÉ par le
compose natif Twitter/X. On cape côté backend pour éviter de proposer
une carte qui produira un échec côté UI compose.
"""

from __future__ import annotations

import re
from typing import Pattern

from app.features.rich_content.schemas import RichContentPayload

# Cap body Tweet aligné cap schéma TweetDraftData (280 limite Twitter/X).
_TWEET_BODY_MAX_CHARS = 280

# ── INTENT — message user upstream ────────────────────────────────────

_INTENT_PATTERNS_FR: tuple[Pattern[str], ...] = (
    # « rédige un tweet » / « écris un tweet »
    re.compile(
        r"\b(rédige|redige|écris|ecris|écrire|ecrire|rédiger|rediger|prépare|prepare|"
        r"crée|cree|génère|genere|tape|tapes|poste|poster|publie|publier)\b"
        r"[^.\n]{0,60}?"
        r"\b(tweet)\b",
        re.IGNORECASE,
    ),
    # « tweete pour mes followers » — verbe tweete/tweeter standalone (la
    # forme verbale française inclut déjà la sémantique « écrire un tweet »).
    re.compile(
        r"\b(tweete|tweeter|tweetez|tweetes)\b",
        re.IGNORECASE,
    ),
    # « tweet sur Flutter » / « tweet pour célébrer » / « tweet de réaction »
    re.compile(
        r"\btweet\b\s+(sur|pour|de|d')\b",
        re.IGNORECASE,
    ),
    # « poste sur X » / « publie sur X » — collocation forte avec verbe + sur + X.
    # Verbe insensible à la casse via inline flag `(?i:...)`, X UPPERCASE
    # strict pour éviter faux positif sur « x = 5 » (variable / lettre).
    re.compile(
        r"\b(?i:poste|poster|publie|publier|partage|partager)\s+sur\s+X\b",
    ),
)

_INTENT_PATTERNS_EN: tuple[Pattern[str], ...] = (
    # "write a tweet" / "tweet about X" / "post a tweet"
    re.compile(
        r"\b(write|draft|compose|prepare|create|publish|post|tweet)\b[^.\n]{0,60}?"
        r"\btweet\b",
        re.IGNORECASE,
    ),
    # "tweet about Flutter" / "tweet for celebrating"
    re.compile(
        r"\btweet\s+(about|for|on)\b",
        re.IGNORECASE,
    ),
    # "post on X" — verbe insensible à la casse, X UPPERCASE strict.
    re.compile(
        r"\b(?i:post|publish|share)\s+on\s+X\b",
    ),
)


def detect_tweet_intent(user_message: str) -> bool:
    """Scan keywords FR + EN. True si l'user demande clairement un tweet.

    Strict sur la lettre `X` : exige un verbe d'action collé (`poste sur X`,
    `publish on X`) pour éviter le faux positif sur « équation à 3
    inconnues x, y, z » ou « le X dans cet algorithme ».

    `tweet`/`tweeter` reconnus directement (mots-clés non ambigus).
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return False

    for pattern in _INTENT_PATTERNS_FR + _INTENT_PATTERNS_EN:
        if pattern.search(user_message):
            return True
    return False


def detect_rich_content_tweet(user_message: str, assistant_text: str) -> dict | None:
    """Point d'entrée — détection intent-based exclusive.

    Si intent OK :
      - Cap body à 280 chars (limite officielle Twitter/X)
      - Si le LLM sur-génère (> 280), on cape proprement sur le dernier
        espace sous le cap pour ne pas couper un mot. Pas de troncature
        sur newline car un tweet n'est généralement pas multi-ligne.
      - Wrapping dans `RichContentPayload.tweet`

    Note : un tweet sous le cap = OK, à exactement 280 = OK,
    au-dessus = tronqué proprement.
    """
    if not detect_tweet_intent(user_message):
        return None

    if not isinstance(assistant_text, str):
        return None
    text = assistant_text.strip()
    if len(text) < 5:
        # Tweet trop court pour être crédible
        return None

    # Cap body à 280 chars. Si > 280, tronque sur dernier espace sous le cap
    # pour ne pas couper en plein milieu d'un mot. Un tweet est rarement
    # multi-ligne donc on cherche un espace plutôt qu'un \n.
    body = text[:_TWEET_BODY_MAX_CHARS]
    if len(text) > _TWEET_BODY_MAX_CHARS:
        last_space = body.rfind(" ")
        if last_space > int(_TWEET_BODY_MAX_CHARS * 0.85):
            body = body[:last_space]

    try:
        structured = RichContentPayload.tweet(body=body)
    except Exception:  # noqa: BLE001
        return None

    return structured.model_dump()
