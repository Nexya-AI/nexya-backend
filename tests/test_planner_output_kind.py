"""Tests — détection `output_kind` d'une tâche planifiée (LOT B2)."""

from __future__ import annotations

import pytest

from app.features.planner import output_kind as ok_mod
from app.features.planner.output_kind import detect_task_output_kind, extract_output_kind


# ══════════════════════════════════════════════════════════════
# detect_task_output_kind — inputs réels
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "prompt",
    [
        "rappelle-moi d'étudier mon code Python ce soir",
        "rappelle moi de boire de l'eau",
        "remind me to call my mother",
        "préviens-moi avant la réunion",
        "crée un rappel pour prendre mes médicaments",
    ],
)
def test_reminder_intents_detected(prompt: str) -> None:
    assert detect_task_output_kind(prompt) == "reminder"


@pytest.mark.parametrize(
    "prompt",
    [
        "génère un cours détaillé sur les boucles for en Python",
        "rédige un rapport hebdomadaire sur l'avancement du projet",
        "écris-moi une lettre formelle au maire de Yaoundé",
    ],
)
def test_document_intents_detected(prompt: str) -> None:
    assert detect_task_output_kind(prompt) == "document"


@pytest.mark.parametrize(
    "prompt",
    [
        "résume l'actualité technologique chaque matin",
        "donne-moi trois idées de recettes véganes",
        "quelle est la météo prévue cette semaine",
    ],
)
def test_generation_is_default(prompt: str) -> None:
    assert detect_task_output_kind(prompt) == "generation"


@pytest.mark.parametrize("prompt", ["", "   ", "\n\t"])
def test_empty_prompt_falls_back_to_generation(prompt: str) -> None:
    assert detect_task_output_kind(prompt) == "generation"


def test_none_prompt_falls_back_to_generation() -> None:
    # Robustesse défensive : input non-str (drift, appel buggé) → generation.
    assert detect_task_output_kind(None) == "generation"  # type: ignore[arg-type]


def test_meta_question_is_not_a_reminder() -> None:
    # « comment créer un rappel ? » est une question, pas une demande de
    # planification (vetoed par les meta-markers des 2 détecteurs).
    assert detect_task_output_kind("comment créer un rappel sur NEXYA ?") == "generation"


# ══════════════════════════════════════════════════════════════
# detect_task_output_kind — priorité document > reminder > generation
# (monkeypatch déterministe, indépendant des internals des détecteurs)
# ══════════════════════════════════════════════════════════════


def test_priority_document_wins_when_both_match(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ok_mod, "detect_document_intent", lambda _p: True)
    monkeypatch.setattr(ok_mod, "detect_planning_intent", lambda _p: True)
    assert detect_task_output_kind("rappelle-moi de rédiger un rapport") == "document"


def test_priority_reminder_when_only_planning_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ok_mod, "detect_document_intent", lambda _p: False)
    monkeypatch.setattr(ok_mod, "detect_planning_intent", lambda _p: True)
    assert detect_task_output_kind("rappelle-moi un truc") == "reminder"


def test_priority_generation_when_nothing_matches(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ok_mod, "detect_document_intent", lambda _p: False)
    monkeypatch.setattr(ok_mod, "detect_planning_intent", lambda _p: False)
    assert detect_task_output_kind("blabla neutre") == "generation"


# ══════════════════════════════════════════════════════════════
# extract_output_kind — lecture tolérante depuis metadata_json
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    ("meta", "expected"),
    [
        ({"output_kind": "reminder"}, "reminder"),
        ({"output_kind": "generation"}, "generation"),
        ({"output_kind": "document"}, "document"),
        ({"output_kind": "bogus"}, "generation"),  # valeur inconnue → défaut
        ({"output_kind": 42}, "generation"),  # non-str → défaut
        ({"other": "x"}, "generation"),  # clé absente → défaut
        ({}, "generation"),  # dict vide → défaut
        (None, "generation"),  # tâche pré-feature → défaut
        ("not-a-dict", "generation"),  # input corrompu → défaut
    ],
)
def test_extract_output_kind(meta, expected: str) -> None:
    assert extract_output_kind(meta) == expected
