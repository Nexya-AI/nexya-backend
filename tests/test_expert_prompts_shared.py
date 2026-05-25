"""
Tests Session A2 — `app/ai/expert_prompts/_shared.py`.

Valide les helpers transverses (constantes brand, urgences, clauses,
FewShotExample dataclass, format_few_shot_examples, build_system_prompt).
"""

from __future__ import annotations

import pytest

from app.ai.expert_prompts._shared import (
    EMERGENCY_NUMBERS_CAMEROON,
    EMERGENCY_NUMBERS_INTERNATIONAL,
    NEXYA_BRAND_SIGNATURE,
    NEXYALABS_SIGNATURE,
    FewShotExample,
    build_system_prompt,
    conversational_continuity_clause,
    format_few_shot_examples,
    markdown_format_clause,
    memory_aware_clause,
    multi_language_clause,
    progressive_disclosure_clause,
    source_attribution_clause,
)

# ══════════════════════════════════════════════════════════════
# Constantes brand & urgences
# ══════════════════════════════════════════════════════════════


def test_brand_signature_is_nexya_ai() -> None:
    assert NEXYA_BRAND_SIGNATURE == "NEXYA AI"


def test_nexyalabs_signature() -> None:
    assert NEXYALABS_SIGNATURE == "Nexyalabs"


def test_emergency_numbers_cameroon_contains_3_critical_numbers() -> None:
    """117 (Police), 118 (Pompiers), 119 (SAMU) — vie/mort."""
    assert "117" in EMERGENCY_NUMBERS_CAMEROON
    assert "118" in EMERGENCY_NUMBERS_CAMEROON
    assert "119" in EMERGENCY_NUMBERS_CAMEROON


def test_emergency_numbers_international_includes_112() -> None:
    """112 = numéro universel mobile international."""
    assert "112" in EMERGENCY_NUMBERS_INTERNATIONAL


# ══════════════════════════════════════════════════════════════
# Clauses transverses
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "clause_fn",
    [
        multi_language_clause,
        memory_aware_clause,
        progressive_disclosure_clause,
        conversational_continuity_clause,
        markdown_format_clause,
        source_attribution_clause,
    ],
)
def test_each_clause_returns_non_empty_string(clause_fn) -> None:
    result = clause_fn()
    assert isinstance(result, str)
    assert len(result) > 100


def test_multi_language_clause_mentions_user_language_detection() -> None:
    clause = multi_language_clause().lower()
    assert "langue" in clause
    assert "détecte" in clause or "detecte" in clause


def test_memory_aware_clause_mentions_d3_context_block() -> None:
    clause = memory_aware_clause()
    assert "Contexte sur l'utilisateur" in clause or "mémoire" in clause.lower()


def test_progressive_disclosure_clause_mentions_level_one() -> None:
    clause = progressive_disclosure_clause().lower()
    assert "niveau 1" in clause or "approfondir" in clause


def test_conversational_continuity_clause_forbids_re_presentation() -> None:
    """Le préambule est déjà injecté en amont — l'expert ne se re-présente pas."""
    clause = conversational_continuity_clause().lower()
    assert "re-présente" in clause or "préambule" in clause


def test_markdown_format_clause_mentions_tables_and_latex() -> None:
    clause = markdown_format_clause()
    assert "tableau" in clause.lower() or "Tableaux" in clause
    assert "LaTeX" in clause or "latex" in clause.lower()


def test_source_attribution_clause_forbids_fabrication() -> None:
    clause = source_attribution_clause().lower()
    assert "invention" in clause or "fabulation" in clause or "jamais" in clause


# ══════════════════════════════════════════════════════════════
# FewShotExample dataclass
# ══════════════════════════════════════════════════════════════


def test_few_shot_example_is_frozen() -> None:
    """frozen=True empêche les mutations runtime accidentelles."""
    example = FewShotExample(user_question="Q", nexya_response="R")
    with pytest.raises((AttributeError, Exception)):
        example.user_question = "X"  # type: ignore[misc]


def test_few_shot_example_why_is_optional() -> None:
    example = FewShotExample(user_question="Q", nexya_response="R")
    assert example.why_this_is_good is None


def test_few_shot_example_with_why_explanation() -> None:
    example = FewShotExample(
        user_question="Q",
        nexya_response="R",
        why_this_is_good="Pattern X ancre Y",
    )
    assert example.why_this_is_good == "Pattern X ancre Y"


# ══════════════════════════════════════════════════════════════
# format_few_shot_examples
# ══════════════════════════════════════════════════════════════


def test_format_few_shot_examples_empty_returns_empty_string() -> None:
    """Pas de section vide quand pas d'exemples (anti-pollution prompt)."""
    assert format_few_shot_examples(()) == ""


def test_format_few_shot_examples_single_example() -> None:
    example = FewShotExample(user_question="Q1", nexya_response="R1")
    result = format_few_shot_examples((example,))
    assert "Exemples calibrés" in result
    assert "--- Exemple 1 ---" in result
    assert "Q1" in result
    assert "R1" in result


def test_format_few_shot_examples_numbered_correctly() -> None:
    examples = tuple(
        FewShotExample(user_question=f"Q{i}", nexya_response=f"R{i}") for i in range(1, 4)
    )
    result = format_few_shot_examples(examples)
    assert "--- Exemple 1 ---" in result
    assert "--- Exemple 2 ---" in result
    assert "--- Exemple 3 ---" in result


def test_format_few_shot_examples_omits_why_this_is_good_from_prompt() -> None:
    """why_this_is_good est interne, ne doit PAS apparaître dans le prompt LLM."""
    example = FewShotExample(
        user_question="Q",
        nexya_response="R",
        why_this_is_good="SECRET_INTERNAL_NOTE",
    )
    result = format_few_shot_examples((example,))
    assert "SECRET_INTERNAL_NOTE" not in result


def test_format_few_shot_examples_custom_section_title() -> None:
    example = FewShotExample(user_question="Q", nexya_response="R")
    result = format_few_shot_examples((example,), section_title="Mes exemples")
    assert "[Mes exemples]" in result


# ══════════════════════════════════════════════════════════════
# build_system_prompt
# ══════════════════════════════════════════════════════════════


def test_build_system_prompt_minimal_assembly() -> None:
    """Sans few-shot ni extra blocks, assemble persona + meth + templates +
    anti-patterns + clauses transverses."""
    result = build_system_prompt(
        persona="PERSONA_BLOCK",
        methodology="METHOD_BLOCK",
        output_templates="TEMPLATES_BLOCK",
        anti_patterns="ANTI_BLOCK",
    )
    assert "PERSONA_BLOCK" in result
    assert "METHOD_BLOCK" in result
    assert "TEMPLATES_BLOCK" in result
    assert "ANTI_BLOCK" in result
    # Clauses transverses injectées par défaut
    assert "Multi-langue" in result or "langue" in result.lower()
    assert "Continuité" in result or "continuité" in result.lower()


def test_build_system_prompt_with_few_shot_examples() -> None:
    examples = (FewShotExample(user_question="QQ", nexya_response="RR"),)
    result = build_system_prompt(
        persona="P",
        methodology="M",
        output_templates="T",
        anti_patterns="A",
        few_shot_examples=examples,
    )
    assert "QQ" in result
    assert "RR" in result
    assert "--- Exemple 1 ---" in result


def test_build_system_prompt_without_transverse_clauses() -> None:
    """include_transverse_clauses=False pour mode image-only (studio)."""
    result = build_system_prompt(
        persona="P",
        methodology="M",
        output_templates="T",
        anti_patterns="A",
        include_transverse_clauses=False,
    )
    assert "P" in result
    # Pas de clauses transverses → pas de mention multi-langue / continuité.
    assert "Multi-langue dynamique" not in result
    assert "Continuité conversationnelle" not in result


def test_build_system_prompt_extra_blocks_injected_before_clauses() -> None:
    """Les extra_blocks (ex: bloc URGENCE medicine) viennent APRÈS
    anti_patterns mais AVANT les clauses transverses."""
    result = build_system_prompt(
        persona="P",
        methodology="M",
        output_templates="T",
        anti_patterns="A",
        extra_blocks=("EXTRA_URGENCE_BLOCK",),
    )
    assert "EXTRA_URGENCE_BLOCK" in result
    anti_idx = result.index("A")
    extra_idx = result.index("EXTRA_URGENCE_BLOCK")
    # Extra block après anti_patterns
    assert anti_idx < extra_idx


def test_build_system_prompt_idempotent() -> None:
    """2 appels avec mêmes args → string égal byte-à-byte."""
    a = build_system_prompt(persona="P", methodology="M", output_templates="T", anti_patterns="A")
    b = build_system_prompt(persona="P", methodology="M", output_templates="T", anti_patterns="A")
    assert a == b
