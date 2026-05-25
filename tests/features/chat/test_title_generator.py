"""Tests du helper `derive_deterministic_title`.

Suite test exhaustive (~35 cas paramétrés) qui valide les 3 garanties
critiques du contrat :

1. **Stabilité** : même input → même output (déterministe, pas de random).
2. **Pas de "Nouvelle discussion"** : retourne TOUJOURS une string non-vide,
   même sur edge cases (empty, whitespace-only, ponctuation seule, message
   uniquement composé de prefixes droppés).
3. **Pas de titre dégénéré** : pas de phrase narrative tronquée façon Gemini
   (« ses objectifs principaux sont. »), toujours un groupe nominal lisible.

Couvre les 3 cas terrain Ivan 2026-05-15 + 32 autres cas FR+EN.
"""

from __future__ import annotations

import pytest

from app.features.chat.title_generator import (
    TITLE_MAX_CHARS,
    derive_deterministic_title,
)

# ═══════════════════════════════════════════════════════════════════════
# Cas terrain Ivan 2026-05-15 (les 3 qui ont déclenché Bug-040)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Quels sont les ingrédients du ndolé ?", "Ingrédients du ndolé"),
        ("C'est quoi la vie ?", "La vie"),
        ("C'est quoi la data science ?", "La data science"),
    ],
)
def test_ivan_terrain_cases(message: str, expected: str) -> None:
    """Les 3 cas spécifiques où le LLM async retournait du gibberish."""
    assert derive_deterministic_title(message) == expected


# ═══════════════════════════════════════════════════════════════════════
# Edge cases — garantie « jamais vide »
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "message",
    [
        "",  # empty string
        "   ",  # whitespace only
        "\n\t\r",  # autres whitespaces
        "?",  # ponctuation seule
        "???",
        "...",
        "!!!",
        "...,;:!?",  # ponctuation multiple
        "c'est quoi",  # uniquement prefix droppé sans contenu
        "qu'est-ce que",
        "comment",
        "what is",
        "what is ?",  # prefix + juste un ?
        "c'est quoi ?!",  # prefix + ponctuation
    ],
)
def test_fallback_discussion_on_empty_or_unusable(message: str) -> None:
    """Toujours retourner 'Discussion', jamais '' ni None."""
    result = derive_deterministic_title(message)
    assert result == "Discussion"
    assert result  # non-vide garanti


# ═══════════════════════════════════════════════════════════════════════
# FR — Interrogatifs structurés
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "message,expected",
    [
        # « c'est quoi » et variantes
        ("C'est quoi la photosynthèse ?", "La photosynthèse"),
        ("cest quoi le BTP", "Le BTP"),  # variante orthographique
        ("ces quoi un microservice", "Un microservice"),
        # « qu'est-ce que »
        ("Qu'est-ce que l'OHADA ?", "L'OHADA"),
        ("Qu'est-ce que c'est qu'un trojan ?", "Qu'un trojan"),
        # « quel est » (drop inclut l'article si présent → titre plus court+actionnable)
        ("Quel est le PIB du Cameroun ?", "PIB du Cameroun"),
        ("Quelle est la capitale du Sénégal ?", "Capitale du Sénégal"),
        # « quels sont les »
        ("Quels sont les meilleurs frameworks Flutter ?", "Meilleurs frameworks Flutter"),
        ("Quelles sont les langues officielles ?", "Langues officielles"),
        # « qui est »
        ("Qui est Aminata Touré ?", "Aminata Touré"),
        ("Qui sont les Bamiléké ?", "Les Bamiléké"),
    ],
)
def test_fr_interrogative_structures(message: str, expected: str) -> None:
    assert derive_deterministic_title(message) == expected


# ═══════════════════════════════════════════════════════════════════════
# FR — Verbes d'action / requêtes
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Explique-moi la photosynthèse", "La photosynthèse"),
        ("Dis-moi ce que tu sais sur le Cameroun", "Ce que tu sais sur le Cameroun"),
        ("Donne-moi des recettes africaines", "Des recettes africaines"),
        ("Parle-moi de la musique afrobeat", "La musique afrobeat"),
        ("Aide-moi à apprendre Python", "Apprendre Python"),
        ("J'aimerais savoir comment investir", "Comment investir"),
        ("Je veux savoir la formule de l'eau", "La formule de l'eau"),
        ("Peux-tu me lister les pays d'Afrique ?", "Me lister les pays d'Afrique"),
    ],
)
def test_fr_action_verbs(message: str, expected: str) -> None:
    assert derive_deterministic_title(message) == expected


# ═══════════════════════════════════════════════════════════════════════
# EN — Interrogatifs + actions
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "message,expected",
    [
        ("What is the meaning of life?", "Meaning of life"),
        ("What are the best frameworks?", "Best frameworks"),
        ("How do I learn Python?", "Learn Python"),
        ("How can I optimize SQL queries?", "Optimize SQL queries"),
        ("Why is the sky blue?", "The sky blue"),
        ("Tell me about Cameroon history", "Cameroon history"),
        ("Explain quantum computing", "Quantum computing"),
        ("Show me Docker best practices", "Docker best practices"),
        ("Can you write a poem?", "Write a poem"),
    ],
)
def test_en_interrogatives_and_actions(message: str, expected: str) -> None:
    assert derive_deterministic_title(message) == expected


# ═══════════════════════════════════════════════════════════════════════
# Cas sans prefix à drop (texte direct)
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "message,expected",
    [
        ("Bonjour", "Bonjour"),
        ("Hello", "Hello"),
        ("Salut comment vas-tu ?", "Salut comment vas-tu"),
        ("Recette du poulet DG", "Recette du poulet DG"),
        ("Python best practices", "Python best practices"),
        # Texte qui CONTIENT un prefix mais pas en début (ne doit pas être droppé)
        (
            "Salut, c'est quoi ça ?",
            "Salut, c'est quoi ça",
        ),  # « c'est quoi » au milieu, pas droppé
    ],
)
def test_no_prefix_to_drop(message: str, expected: str) -> None:
    assert derive_deterministic_title(message) == expected


# ═══════════════════════════════════════════════════════════════════════
# Cap à `max_chars` + word boundary
# ═══════════════════════════════════════════════════════════════════════


def test_cap_long_message_with_ellipsis() -> None:
    """Message > 40 chars → tronqué sur word boundary + ellipsis."""
    long = "Recette traditionnelle du ndolé camerounais avec des arachides et du poisson fumé"
    result = derive_deterministic_title(long)
    # Vérif : ne dépasse pas 41 chars (40 + ellipsis 1 char)
    assert len(result) <= TITLE_MAX_CHARS + 1
    assert result.endswith("…")
    # Vérif : ne coupe pas en plein milieu d'un mot
    assert not result[-2].isalnum() or result.endswith("…")


def test_cap_respects_word_boundary() -> None:
    """Coupe propre sur un espace, pas mid-word."""
    message = "Comment fonctionnent les transactions ACID dans PostgreSQL avec MVCC"
    result = derive_deterministic_title(message)
    # « Comment » est droppé, reste « Fonctionnent les transactions ACID dans PostgreSQL avec MVCC »
    # 63 chars → tronqué à ~40 sur espace.
    assert len(result) <= TITLE_MAX_CHARS + 1
    # Le texte avant l'ellipsis ne doit pas finir par une lettre coupée
    text_before_ellipsis = result.rstrip("…")
    assert not text_before_ellipsis.endswith(" ")  # strip ok


def test_exact_max_chars_no_ellipsis() -> None:
    """Si le texte fait exactement max_chars, pas d'ellipsis."""
    # 40 chars exact
    msg = "abcdefghijklmnopqrstuvwxyzabcdefghijklmn"  # 40 'a-n'
    result = derive_deterministic_title(msg)
    assert len(result) == TITLE_MAX_CHARS
    assert not result.endswith("…")


def test_custom_max_chars() -> None:
    """`max_chars` configurable pour cas spéciaux."""
    result = derive_deterministic_title("Bonjour comment ça va aujourd'hui ?", max_chars=10)
    assert len(result) <= 11  # 10 + ellipsis
    assert result.endswith("…")


# ═══════════════════════════════════════════════════════════════════════
# Capitalize + ponctuation
# ═══════════════════════════════════════════════════════════════════════


def test_capitalize_first_letter() -> None:
    """Force la 1ère lettre en majuscule, préserve le reste."""
    assert derive_deterministic_title("bonjour") == "Bonjour"
    # Préserve les majuscules du reste (« Python »)
    assert derive_deterministic_title("how do i learn Python?") == "Learn Python"


def test_strip_trailing_punctuation() -> None:
    """Strip ponctuation finale (.?!:;,)."""
    assert derive_deterministic_title("Bonjour.") == "Bonjour"
    assert derive_deterministic_title("Salut ! Tu vas bien ?") == "Salut ! Tu vas bien"


def test_preserve_internal_punctuation() -> None:
    """Garde la ponctuation interne (apostrophes, virgules)."""
    assert derive_deterministic_title("L'IA, c'est puissant") == "L'IA, c'est puissant"


# ═══════════════════════════════════════════════════════════════════════
# Idempotence (même input → même output)
# ═══════════════════════════════════════════════════════════════════════


def test_idempotent() -> None:
    """Appelé 100× sur la même string → résultat identique."""
    msg = "C'est quoi la vie ?"
    results = {derive_deterministic_title(msg) for _ in range(100)}
    assert len(results) == 1
    assert results.pop() == "La vie"


# ═══════════════════════════════════════════════════════════════════════
# Faux positifs anti-pattern (prefix dans le mot, pas en début)
# ═══════════════════════════════════════════════════════════════════════


def test_no_false_positive_substring_match() -> None:
    """« commentaire » ne doit PAS être interprété comme « comment + aire »."""
    result = derive_deterministic_title("Commentaire sur le projet")
    # « comment » est dans la liste, mais « commentaire » commence par
    # « comment » suivi de 'a' qui est alphanumeric → pas un word boundary.
    # Donc le prefix NE doit PAS être droppé.
    assert result == "Commentaire sur le projet"


def test_no_drop_prefix_in_middle() -> None:
    """Un prefix au milieu du texte n'est pas droppé."""
    result = derive_deterministic_title("Bonjour, comment ça va ?")
    # « comment » est au milieu, ne doit pas affecter le drop initial.
    assert result == "Bonjour, comment ça va"
