"""
Tests unitaires — `_is_sensitive` (Session D2).

Filtre défensif contre les faits RGPD Article 9 (santé / finance /
religion / politique / orientation / syndicat). Conservateur : recall >
precision — peut produire des faux positifs, c'est le compromis voulu.
"""

from __future__ import annotations

import pytest

from workers.memory_tasks import _is_sensitive

# ══════════════════════════════════════════════════════════════
# 1. Vrais positifs — doivent matcher chaque catégorie
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "fact",
    [
        # Santé FR
        "L'utilisateur a un diagnostic de dépression",
        "L'utilisateur prend un médicament contre l'anxiété",
        "L'utilisateur souffre de cancer",
        "L'utilisateur a le diabète",
        # Santé EN
        "The user has a medication prescription",
        "The user was diagnosed with a disorder",
        # Finances privées
        "L'utilisateur a un découvert bancaire important",
        "The user is paying off debt",
        # Religion
        "L'utilisateur est musulman pratiquant",
        "The user is christian",
        # Politique
        "L'utilisateur est socialiste convaincu",
        # Orientation
        "L'utilisateur est homosexuel",
        "The user identifies as bisexual",
        # Syndicat
        "L'utilisateur est membre d'un syndicat",
    ],
)
def test_is_sensitive_detects_protected_categories(fact: str) -> None:
    assert _is_sensitive(fact) is True


# ══════════════════════════════════════════════════════════════
# 2. Vrais négatifs — doivent passer
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "fact",
    [
        "L'utilisateur est développeur Flutter",
        "L'utilisateur habite au Cameroun",
        "L'utilisateur travaille sur un projet NEXYA",
        "The user loves cooking camerounian food",
        "L'utilisateur parle français et anglais",
        "L'utilisateur a 5 ans d'expérience en code",
    ],
)
def test_is_sensitive_passes_neutral_facts(fact: str) -> None:
    assert _is_sensitive(fact) is False


# ══════════════════════════════════════════════════════════════
# 3. Case-insensitive
# ══════════════════════════════════════════════════════════════


def test_is_sensitive_is_case_insensitive() -> None:
    assert _is_sensitive("L'utilisateur souffre de DÉPRESSION") is True
    assert _is_sensitive("The User Has A Disease") is True


# ══════════════════════════════════════════════════════════════
# 4. Limite connue : faux positifs sur substring
# ══════════════════════════════════════════════════════════════


def test_is_sensitive_false_positive_substring_documented() -> None:
    """Le filtre est substring-based, conservateur. Un mot innocent qui
    contient un mot-clé sensible (ex: « traitement de texte ») sera
    filtré. C'est VOULU — recall > precision pour le RGPD."""
    assert _is_sensitive("L'utilisateur aime les traitements de texte") is True
    # Conservation de ce test = documentation du compromis accepté.


# ══════════════════════════════════════════════════════════════
# 5. Empty / whitespace
# ══════════════════════════════════════════════════════════════


def test_is_sensitive_empty_string_returns_false() -> None:
    assert _is_sensitive("") is False
    assert _is_sensitive("   ") is False
