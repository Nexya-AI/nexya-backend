"""
Tests planner-from-chat LOT 5 — `app.ai.intent_classifier`.

`detect_planning_intent` est un classifieur conservateur : il ne déclenche
que sur des tournures impératives non équivoques de planification, et se
désamorce dès qu'un marqueur de question explicative est présent.

Discipline de calibrage : les faux positifs (forcer un tool call à tort)
coûtent cher ; les faux négatifs (retomber en AUTO) sont bénins. Les tests
vérifient les deux directions.
"""

from __future__ import annotations

import pytest

from app.ai.intent_classifier import detect_planning_intent

# ══════════════════════════════════════════════════════════════
# Cas vides / dégénérés
# ══════════════════════════════════════════════════════════════


def test_empty_string_returns_false() -> None:
    assert detect_planning_intent("") is False


def test_whitespace_only_returns_false() -> None:
    assert detect_planning_intent("   \n\t ") is False


# ══════════════════════════════════════════════════════════════
# Vrais positifs — FR
# ══════════════════════════════════════════════════════════════

_FR_POSITIVES = [
    "rappelle-moi demain à 8h de prendre mes médicaments",
    "rappelle moi de payer le loyer le 25",
    "rappelez-moi l'échéance du contrat",
    "préviens-moi à 18h",
    "previens moi quand c'est l'heure",
    "alerte-moi tous les lundis",
    "fais-moi penser à appeler maman",
    "crée un rappel pour la réunion",
    "crée-moi un rappel quotidien",
    "ajoute un rappel à 9h",
    "mets un rappel pour ce soir",
    "ajoute une tâche récurrente",
    "planifie-moi une revue chaque vendredi",
    "n'oublie pas de me rappeler le rendez-vous",
]


@pytest.mark.parametrize("text", _FR_POSITIVES)
def test_fr_planning_phrases_detected(text: str) -> None:
    assert detect_planning_intent(text) is True, text


# ══════════════════════════════════════════════════════════════
# Vrais positifs — EN
# ══════════════════════════════════════════════════════════════

_EN_POSITIVES = [
    "remind me to call mom tomorrow",
    "set a reminder for 8am",
    "set me a reminder every monday",
    "create a reminder for the meeting",
    "add a reminder at noon",
    "schedule a daily summary",
    "alert me when it's time",
    "notify me to take my pills",
]


@pytest.mark.parametrize("text", _EN_POSITIVES)
def test_en_planning_phrases_detected(text: str) -> None:
    assert detect_planning_intent(text) is True, text


def test_detection_is_case_insensitive() -> None:
    assert detect_planning_intent("RAPPELLE-MOI DEMAIN 8H") is True
    assert detect_planning_intent("Remind Me Tomorrow") is True


# ══════════════════════════════════════════════════════════════
# Vrais négatifs — pas d'intention de planification
# ══════════════════════════════════════════════════════════════

_NEGATIVES = [
    "quelle est la capitale du Cameroun",
    "explique-moi async vs threads en Python",
    "merci beaucoup",
    "donne-moi la recette du ndolé",
    "traduis cette phrase en anglais",
    "comment vas-tu aujourd'hui",
]


@pytest.mark.parametrize("text", _NEGATIVES)
def test_non_planning_phrases_not_detected(text: str) -> None:
    assert detect_planning_intent(text) is False, text


def test_emergency_phrase_does_not_trigger() -> None:
    """Garde-fou medicine : une phrase d'urgence vitale ne contient aucune
    tournure de planification → AUTO préservé, le LLM répond URGENCE."""
    assert detect_planning_intent("j'ai mal à la poitrine depuis 1 heure") is False
    assert detect_planning_intent("je crois que je fais un AVC") is False


# ══════════════════════════════════════════════════════════════
# Marqueurs méta — désamorçage (faux positif évité)
# ══════════════════════════════════════════════════════════════

_META_BLOCKED = [
    "comment créer un rappel ?",
    "c'est quoi un rappel exactement",
    "comment fonctionne le planificateur",
    "comment ça marche les rappels",
    "à quoi sert un rappel",
    "explique-moi comment créer un rappel",
    "how do reminders work",
    "what is a reminder",
    "how to set a reminder",
]


@pytest.mark.parametrize("text", _META_BLOCKED)
def test_meta_questions_are_blocked(text: str) -> None:
    """Une question explicative qui contient pourtant une tournure de
    planification ne doit PAS déclencher (le user veut une explication)."""
    assert detect_planning_intent(text) is False, text
