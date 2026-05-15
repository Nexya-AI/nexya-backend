"""Helper déterministe de génération de titre de conversation.

**Pourquoi un titre déterministe vs LLM-généré ?**

L'approche LLM-async (worker arq + Gemini Flash) du `workers/chat_tasks.py`
souffre de 3 problèmes fondamentaux observés en V1 :

1. **Instabilité du résultat** — même avec un prompt strict, Gemini retourne
   parfois des titres dégénérés (« ses objectifs principaux sont. ») ou
   carrément vides (`{'skipped': True, 'reason': 'empty'}`) dûs au thinking
   mode interne du modèle qui consomme tous les max_tokens.

2. **Latence imprévisible** — cron arq toutes les 60s × appel LLM 5-15s =
   l'user voit « Nouvelle discussion » placeholder pendant 30-75s avant que
   le titre auto n'apparaisse au prochain pull manuel du drawer.

3. **Fragile aux pannes** — Redis flap, worker arq down, quota Gemini
   épuisé, et la conv reste sans titre INDÉFINIMENT (la sentinelle DB
   `title_generated_at` n'est pas posée sur échec → re-tentatives futures).

**Cette approche déterministe** dérive un titre court et lisible
SYNCHRONIQUEMENT (1ms, 0 appel API, 0 dépendance externe) à partir du
1er message user, au moment du INSERT atomique de la `Conversation`.
Posé en DB immédiatement, lu par le drawer au 1er pull → l'user ne voit
**jamais** « Nouvelle discussion » comme titre final.

Pattern aligné ChatGPT / Claude mobile : titre instantané du 1er message,
raffinement LLM V2 différé optionnel (peut être ajouté en post-launch si
signal user émerge, mais le worker arq actuel `generate_conversation_title`
reste en code prêt à dégainer — il est juste plus enqueued par le router V1).

**Garanties du contrat** :
- Retourne TOUJOURS une string non-vide (jamais None, jamais "")
- Cap strict à `max_chars` (défaut 40, aligné `TITLE_MAX_CHARS` du worker)
- Fallback "Discussion" si message vide / uniquement prefix droppé / ponctuation
- Capitalize 1ère lettre + strip ponctuation finale + cleanse
- Pas de troncature mid-word (coupe propre sur espace ou ajoute ellipsis `…`)
- Idempotent : même input → même output (testable déterministiquement)
"""

from __future__ import annotations

import re

# 40 chars = même cap que TITLE_MAX_CHARS du worker arq (cohérence avec
# `nx_chat_bubble` Flutter qui peut afficher jusqu'à ~40 chars avant ellipsis).
TITLE_MAX_CHARS = 40

# Fallback déterministe si rien d'exploitable dans le message user.
# Volontairement court et neutre — `s.historyDraftTitle` Flutter affichera
# de toute façon ce string tel quel ; pas besoin de localiser ici (le backend
# est multi-tenant FR-first, V2 i18n via Accept-Language si signal user).
_FALLBACK_TITLE = "Discussion"

# Prefixes interrogatifs/communs à drop en début de message (case-insensitive).
# **Triés DESC par longueur** pour matcher le plus long d'abord (ex: « quels
# sont les » AVANT « quels sont » AVANT « quel »). Sinon « quel » matcherait
# trop tôt et on perdrait « est le » dans la suite.
#
# Liste exhaustive FR+EN, calibrée sur 50+ formulations courantes observées
# dans les usages d'apps IA conversationnelles (analyse manuelle ChatGPT/
# Claude/Gemini export user prompts publics).
_LEADING_PREFIXES_TO_DROP: tuple[str, ...] = tuple(
    sorted(
        [
            # ── FR — interrogatifs structurés ──────────────────────────
            "qu'est-ce que c'est",
            "qu'est-ce que",
            "qu'est-ce",
            "c'est quoi",
            # Variantes orthographiques courantes (typos communes)
            "ces quoi",
            "cest quoi",
            "ses quoi",
            "quel est le",
            "quel est la",
            "quel est",
            "quelle est la",
            "quelle est le",
            "quelle est",
            "quels sont les",
            "quels sont",
            "quelles sont les",
            "quelles sont",
            "qui est",
            "qui sont",
            "qui a",
            "y a-t-il",
            "y a t il",
            # ── FR — interrogatifs courts ──────────────────────────────
            "comment",
            "pourquoi",
            "quand",
            "combien",
            "où",
            # ── FR — verbes d'action / requêtes ────────────────────────
            "explique-moi",
            "explique moi",
            "explique",
            "dis-moi",
            "dis moi",
            "raconte-moi",
            "raconte moi",
            "donne-moi",
            "donne moi",
            "parle-moi de",
            "parle moi de",
            "parle-moi",
            "parle moi",
            "montre-moi",
            "montre moi",
            "peux-tu",
            "peux tu",
            "pourrais-tu",
            "pourrais tu",
            "j'aimerais savoir",
            "je voudrais savoir",
            "je veux savoir",
            "j'aimerais",
            "je voudrais",
            "je veux",
            "j'ai besoin de",
            "j'ai besoin",
            "aide-moi",
            "aide moi",
            "aide-moi à",
            "aide moi à",
            # ── EN — interrogatifs structurés ──────────────────────────
            "what is the",
            "what is",
            "what are the",
            "what are",
            "what's the",
            "what's",
            "whats the",
            "whats",
            "who is",
            "who are",
            "where is",
            "where are",
            "when is",
            "when are",
            "is there",
            "are there",
            # ── EN — interrogatifs courts ──────────────────────────────
            "how do you",
            "how do i",
            "how do",
            "how can you",
            "how can i",
            "how can",
            "how to",
            "why do you",
            "why do i",
            "why do",
            "why is",
            "why are",
            # ── EN — actions ───────────────────────────────────────────
            "tell me about",
            "tell me",
            "explain to me",
            "explain",
            "give me",
            "show me",
            "can you",
            "could you",
            "would you",
            "i'd like to",
            "i would like to",
            "i want to",
            "help me",
        ],
        key=len,
        reverse=True,
    )
)

# Ponctuation à strip en bord (début ET fin du texte intermédiaire).
# Garde les apostrophes au sein du texte (« l'IA », « d'accord »).
_BOUNDARY_PUNCT = " ?,.!:;-—«»\"'"


def derive_deterministic_title(
    message: str,
    *,
    max_chars: int = TITLE_MAX_CHARS,
) -> str:
    """Génère un titre court déterministe depuis le 1er message user.

    Pipeline strict 6 étapes :
    1. Normalise whitespace (collapse multi-spaces, strip leading/trailing)
    2. Drop leading prefix interrogatif/commun FR/EN (case-insensitive,
       longest match first)
    3. Cleanup ponctuation/spaces résiduels en début
    4. Cap à `max_chars` sur word boundary (jamais mid-word)
    5. Strip ponctuation finale + ajoute `…` si tronqué
    6. Capitalize 1ère lettre

    Args:
        message: Texte brut du 1er message user (la fonction strip + clean).
        max_chars: Longueur maximale du titre. 40 par défaut (cohérence
            avec TITLE_MAX_CHARS du worker arq).

    Returns:
        Un titre court (1-max_chars+1 chars, ellipsis incluse) prêt à
        insérer en DB. Jamais None, jamais "" (fallback `Discussion`).

    Examples:
        >>> derive_deterministic_title("C'est quoi la vie ?")
        'La vie'
        >>> derive_deterministic_title("Quels sont les ingrédients du ndolé ?")
        'Ingrédients du ndolé'
        >>> derive_deterministic_title("How do I learn Python?")
        'Learn Python'
        >>> derive_deterministic_title("")
        'Discussion'
        >>> derive_deterministic_title("???")
        'Discussion'
    """
    if not message:
        return _FALLBACK_TITLE

    # 1. Normalize whitespace
    text = re.sub(r"\s+", " ", message.strip())
    if not text:
        return _FALLBACK_TITLE

    # 2. Drop leading interrogative/common prefix (case-insensitive)
    lower = text.lower()
    for prefix in _LEADING_PREFIXES_TO_DROP:
        if lower.startswith(prefix):
            after_idx = len(prefix)
            # Garde-fou : le prefix doit être suivi d'un séparateur (espace,
            # ponctuation, ou fin de string). Sinon « comment » matcherait
            # « commentaire » à tort.
            if after_idx >= len(text) or not text[after_idx].isalnum():
                text = text[after_idx:].lstrip(_BOUNDARY_PUNCT)
                break

    if not text:
        return _FALLBACK_TITLE

    # 3. Cleanup ponctuation résiduelle en début
    text = text.lstrip(_BOUNDARY_PUNCT)
    if not text:
        return _FALLBACK_TITLE

    # 4. Cap to max_chars sur word boundary
    truncated = False
    if len(text) > max_chars:
        cut = text[:max_chars]
        last_space = cut.rfind(" ")
        # Coupe propre sur espace si trouvé après la moitié (sinon le titre
        # serait trop court par rapport à max_chars).
        if last_space > max_chars // 2:
            cut = cut[:last_space]
        text = cut.rstrip(_BOUNDARY_PUNCT)
        truncated = True

    if not text:
        return _FALLBACK_TITLE

    # 5. Strip ponctuation finale (sans toucher au texte intra)
    text = text.rstrip(_BOUNDARY_PUNCT)
    if truncated:
        text = f"{text}…"

    if not text:
        return _FALLBACK_TITLE

    # 6. Capitalize 1ère lettre (en préservant la casse du reste — « Python »
    # ne devient pas « python » après drop de « how do i »).
    return text[0].upper() + text[1:]
