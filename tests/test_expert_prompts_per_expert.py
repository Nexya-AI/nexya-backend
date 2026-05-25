"""
Tests Session A2 — Invariants paramétrés sur les 11 SYSTEM_PROMPT experts.

Stratégie : un seul fichier de tests paramétré sur les 11 experts, validant
les **invariants transverses** que TOUS les prompts experts doivent
respecter :

1. Length raisonnable (1500-15000 chars — préambule A1 12000 + expert ≤ 4000
   pour rester sous cap LLM 30k tokens avec memory + corpus + rag)
2. Signature brand NEXYA + Nexyalabs présente (réparation A1 régression
   `test_nexya_identity_preserved_on_all_experts`)
3. Sections canoniques présentes (Persona, Méthodologie, Templates,
   Anti-patterns)
4. Few-shot examples présents pour les experts critiques (sauf studio)
5. Clauses transverses présentes (multi-langue, memory-aware, etc.)
6. Aucun leak provider technique (Gemini, OpenAI, Claude, Anthropic)
7. Aucune duplication identité fondateur (palier 2/3/4 reste dans
   `nexya_identity.py` du préambule A1)
"""

from __future__ import annotations

import pytest

from app.ai.expert_prompts import (
    COMPUTER_PROMPT,
    COOKING_PROMPT,
    ENGINEERING_PROMPT,
    FINANCE_PROMPT,
    GENERAL_PROMPT,
    LANGUAGE_PROMPT,
    LEGAL_PROMPT,
    MEDICINE_PROMPT,
    PRODUCTIVITY_PROMPT,
    SCIENCE_PROMPT,
    STUDIO_PROMPT,
)

# ══════════════════════════════════════════════════════════════
# Fixture — registre des 11 prompts
# ══════════════════════════════════════════════════════════════

_ALL_EXPERT_PROMPTS: dict[str, str] = {
    "general": GENERAL_PROMPT,
    "computer": COMPUTER_PROMPT,
    "science": SCIENCE_PROMPT,
    "cooking": COOKING_PROMPT,
    "language": LANGUAGE_PROMPT,
    "legal": LEGAL_PROMPT,
    "medicine": MEDICINE_PROMPT,
    "finance": FINANCE_PROMPT,
    "engineering": ENGINEERING_PROMPT,
    "productivity": PRODUCTIVITY_PROMPT,
    "studio": STUDIO_PROMPT,
}

_EXPERT_IDS = sorted(_ALL_EXPERT_PROMPTS.keys())


# ══════════════════════════════════════════════════════════════
# 1. Length raisonnable
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("expert_id", _EXPERT_IDS)
def test_each_prompt_has_substantial_length(expert_id: str) -> None:
    """Min 1500 chars (un prompt expert affûté ne fait pas 50 lignes)."""
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    assert len(prompt) >= 1500, (
        f"Expert '{expert_id}' a un prompt trop court ({len(prompt)} chars). "
        f"A2 cible 1500-15000 chars par expert pour le niveau divin."
    )


@pytest.mark.parametrize("expert_id", _EXPERT_IDS)
def test_each_prompt_under_max_chars_budget(expert_id: str) -> None:
    """Cap 25000 chars : préambule A1 (~12000) + expert (~25000 max safety-
    critical) + memory (~1500) + corpus G2 (~3000) ≈ 41500 chars ≈ 10000
    tokens (à 4 chars/token), soit ~33% du cap LLM 30k tokens. Reste 20000
    tokens disponibles pour la conversation user + réponse LLM. Cap A2
    calibré pour permettre la richesse divine des experts safety-critical
    (medicine = bloc URGENCE + 5 symptômes + 3 few-shot complets,
    legal = OHADA + Code civil + 3 few-shot avec citations articles
    exacts)."""
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    assert len(prompt) <= 25000, (
        f"Expert '{expert_id}' dépasse 25000 chars ({len(prompt)}). "
        f"Risque de débordement cap LLM 30k tokens avec memory + corpus + rag."
    )


# ══════════════════════════════════════════════════════════════
# 2. Signature brand NEXYA + Nexyalabs (répare A1 régression)
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("expert_id", _EXPERT_IDS)
def test_each_prompt_contains_nexya_brand_signature(expert_id: str) -> None:
    """Réparation A1 : test_nexya_identity_preserved_on_all_experts du
    fichier test_experts_domain_guardrail.py exige « NEXYA » dans chaque
    system_prompt. A2 fournit la signature via la persona."""
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    assert "NEXYA" in prompt, (
        f"Expert '{expert_id}' sans signature 'NEXYA' — casse "
        f"test_nexya_identity_preserved_on_all_experts."
    )


@pytest.mark.parametrize("expert_id", _EXPERT_IDS)
def test_each_prompt_contains_nexyalabs_signature(expert_id: str) -> None:
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    assert "Nexyalabs" in prompt, (
        f"Expert '{expert_id}' sans signature 'Nexyalabs' — casse "
        f"test_nexya_identity_preserved_on_all_experts."
    )


# ══════════════════════════════════════════════════════════════
# 3. Sections canoniques A2 présentes
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("expert_id", _EXPERT_IDS)
def test_each_prompt_has_persona_section(expert_id: str) -> None:
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    assert "[Persona" in prompt, f"Expert '{expert_id}' sans section [Persona ...]."


@pytest.mark.parametrize("expert_id", _EXPERT_IDS)
def test_each_prompt_has_methodology_section(expert_id: str) -> None:
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    assert "[Méthodologie" in prompt, f"Expert '{expert_id}' sans section [Méthodologie ...]."


@pytest.mark.parametrize("expert_id", _EXPERT_IDS)
def test_each_prompt_has_templates_section(expert_id: str) -> None:
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    assert "[Templates" in prompt, f"Expert '{expert_id}' sans section [Templates de sortie ...]."


@pytest.mark.parametrize("expert_id", _EXPERT_IDS)
def test_each_prompt_has_anti_patterns_section(expert_id: str) -> None:
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    assert "[Anti-patterns" in prompt, f"Expert '{expert_id}' sans section [Anti-patterns ...]."


# ══════════════════════════════════════════════════════════════
# 4. Few-shot examples (obligatoires sauf studio)
# ══════════════════════════════════════════════════════════════

_EXPERTS_WITH_FEW_SHOT: list[str] = [
    "general",
    "computer",
    "science",
    "cooking",
    "language",
    "legal",
    "medicine",
    "finance",
    "engineering",
    "productivity",
]


@pytest.mark.parametrize("expert_id", _EXPERTS_WITH_FEW_SHOT)
def test_critical_experts_have_few_shot_examples(expert_id: str) -> None:
    """10 experts (sauf studio image-only) doivent avoir des few-shot."""
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    assert "Exemples calibrés" in prompt, (
        f"Expert '{expert_id}' sans section [Exemples calibrés]. A2 cible "
        f"≥ 1 few-shot example pour le niveau divin."
    )
    assert "--- Exemple 1 ---" in prompt, (
        f"Expert '{expert_id}' n'a pas le format `--- Exemple 1 ---`."
    )


# ══════════════════════════════════════════════════════════════
# 5. Clauses transverses (10 experts, studio sans car image-only)
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "expert_id",
    [eid for eid in _EXPERT_IDS if eid != "studio"],
)
def test_conversational_experts_have_multi_language_clause(expert_id: str) -> None:
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    assert "Multi-langue dynamique" in prompt, f"Expert '{expert_id}' sans clause multi-langue."


@pytest.mark.parametrize(
    "expert_id",
    [eid for eid in _EXPERT_IDS if eid != "studio"],
)
def test_conversational_experts_have_memory_aware_clause(expert_id: str) -> None:
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    assert "Mémoire utilisateur" in prompt, f"Expert '{expert_id}' sans clause memory-aware (D3)."


@pytest.mark.parametrize(
    "expert_id",
    [eid for eid in _EXPERT_IDS if eid != "studio"],
)
def test_conversational_experts_have_continuity_clause(expert_id: str) -> None:
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    assert "Continuité conversationnelle" in prompt


@pytest.mark.parametrize(
    "expert_id",
    [eid for eid in _EXPERT_IDS if eid != "studio"],
)
def test_conversational_experts_have_progressive_disclosure(expert_id: str) -> None:
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    assert "Disclosure progressive" in prompt


# ══════════════════════════════════════════════════════════════
# 6. Anti-leak provider technique
# ══════════════════════════════════════════════════════════════
#
# Les prompts experts ne doivent PAS contenir d'affirmation positive
# sur les providers LLM sous-jacents (« je suis Gemini », « propulsé par
# Claude », « via OpenAI »). C'est cadré dans le préambule A1
# brand_security, mais les system_prompts experts doivent rester silencieux.
#
# Note : les MENTIONS dans les anti-patterns (« JAMAIS de leak provider »)
# sont autorisées car c'est de l'instruction au LLM, pas une affirmation.


_FORBIDDEN_PROVIDER_ASSERTIONS = [
    "Je suis Gemini.",
    "Je suis GPT.",
    "Je suis Claude.",
    "Powered by Gemini",
    "Powered by OpenAI",
    "Powered by Anthropic",
    "via Gemini",
    "via OpenAI",
    "via Anthropic",
]


@pytest.mark.parametrize("expert_id", _EXPERT_IDS)
@pytest.mark.parametrize("forbidden", _FORBIDDEN_PROVIDER_ASSERTIONS)
def test_no_provider_leak_in_prompts(expert_id: str, forbidden: str) -> None:
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    assert forbidden not in prompt, (
        f"Expert '{expert_id}' contient un leak provider : {forbidden!r}"
    )


# ══════════════════════════════════════════════════════════════
# 7. Anti-duplication identité fondateur avec préambule A1
# ══════════════════════════════════════════════════════════════
#
# Le préambule A1 (`nexya_identity.py`) contient déjà les 4 paliers
# progressifs sur Loth Ivan Ngassa Yimga (palier 1 base / palier 2-3-4
# enrichis). Les system_prompts experts ne doivent PAS dupliquer ces
# paliers — ils peuvent juste mentionner Loth Ivan dans un contexte
# factuel précis (attribution corpus G2 pour cooking).


@pytest.mark.parametrize(
    "expert_id",
    [eid for eid in _EXPERT_IDS if eid != "cooking"],
)
def test_non_cooking_experts_dont_mention_loth_ivan_biography(expert_id: str) -> None:
    """Hors cooking (qui attribue le corpus G2 à Loth Ivan / Nexyalabs),
    les autres experts ne doivent PAS répéter la biographie fondateur —
    elle vit exclusivement dans le préambule A1."""
    prompt = _ALL_EXPERT_PROMPTS[expert_id]
    # Pas de mention du nom complet du fondateur dans les autres experts.
    assert "Loth Ivan Ngassa Yimga" not in prompt, (
        f"Expert '{expert_id}' duplique la biographie fondateur du préambule A1."
    )


def test_cooking_can_mention_loth_ivan_for_corpus_attribution() -> None:
    """Cooking est la SEULE exception : il attribue les recettes du corpus
    G2 à « Loth Ivan / Nexyalabs » pour traçabilité AI Act Article 13."""
    assert "Loth Ivan" in COOKING_PROMPT
    assert "Nexyalabs" in COOKING_PROMPT


# ══════════════════════════════════════════════════════════════
# 8. Idempotence imports (sanity)
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("expert_id", _EXPERT_IDS)
def test_prompt_is_a_string(expert_id: str) -> None:
    assert isinstance(_ALL_EXPERT_PROMPTS[expert_id], str)


@pytest.mark.parametrize("expert_id", _EXPERT_IDS)
def test_prompt_is_not_empty(expert_id: str) -> None:
    assert _ALL_EXPERT_PROMPTS[expert_id].strip() != ""
