"""
Tests unitaires — Garde-fou de domaine sur les 9 experts spécialisés.

Vérifie que `_DOMAIN_GUARDRAIL_TEMPLATE` est injecté dans chaque prompt
expert via `_with_guardrail` (introduit en G2 2026-05-16).

Stratégie : tests prompt-level (pas de call LLM) — on garantit que :
1. Le bloc guardrail apparaît dans le `system_prompt` de chaque expert
   spécialisé (computer/science/finance/language/cooking/engineering/
   productivity/medicine/legal).
2. Le bloc N'apparaît PAS dans `general` (catch-all) ni `studio` (image-only).
3. Le helper `_with_guardrail` produit le bon format avec les substitutions
   (`domain_label`, `domain_description`, `suggested_mode`).
4. Les invariants sécurité ne sont pas cassés (disclaimer médecine/légal
   préservé, identité NEXYA conservée). NB : depuis planner-from-chat
   LOT 4, `medicine`/`legal` ont `tools_allowed=True` — le wrap guardrail
   doit préserver cet état (et non le ré-éteindre).
"""

from __future__ import annotations

import pytest

from app.ai.experts import (
    _DOMAIN_GUARDRAIL_TEMPLATE,
    EXPERT_REGISTRY,
    _with_guardrail,
    get_expert_config,
)

# ══════════════════════════════════════════════════════════════
# Helper `_with_guardrail`
# ══════════════════════════════════════════════════════════════


def test_with_guardrail_appends_template_with_substitutions() -> None:
    base = "Identité NEXYA + rôle.\n"
    out = _with_guardrail(
        base,
        domain_label="Cuisine",
        domain_description="recettes camerounaises et internationales",
        suggested_mode="Général",
    )
    # Identité préservée en tête.
    assert out.startswith(base)
    # Bloc garde-fou présent en queue.
    assert "Garde-fou de domaine" in out
    assert "Cuisine" in out
    assert "recettes camerounaises et internationales" in out
    assert "mode Général" in out


def test_with_guardrail_default_suggested_mode_is_general() -> None:
    out = _with_guardrail("X", domain_label="Y", domain_description="Z")
    assert "Général" in out


def test_with_guardrail_template_has_meta_question_tolerance() -> None:
    """Le template doit autoriser les questions méta (« qui es-tu », etc.)."""
    assert (
        "questions méta" in _DOMAIN_GUARDRAIL_TEMPLATE.lower()
        or "qui es-tu" in _DOMAIN_GUARDRAIL_TEMPLATE.lower()
    )


def test_with_guardrail_template_has_no_fabulation_clause() -> None:
    """Le template doit interdire la fabulation hors-domaine."""
    assert (
        "fabule" in _DOMAIN_GUARDRAIL_TEMPLATE.lower()
        or "ne fabule" in _DOMAIN_GUARDRAIL_TEMPLATE.lower()
    )


# ══════════════════════════════════════════════════════════════
# Présence du guardrail dans les 9 experts spécialisés
# ══════════════════════════════════════════════════════════════


_SPECIALIZED_EXPERTS = (
    "computer",
    "science",
    "finance",
    "language",
    "cooking",
    "engineering",
    "productivity",
    "medicine",
    "legal",
)


@pytest.mark.parametrize("expert_id", _SPECIALIZED_EXPERTS)
def test_specialized_expert_has_guardrail(expert_id: str) -> None:
    """Tous les 9 experts spécialisés doivent embarquer le garde-fou."""
    cfg = get_expert_config(expert_id)
    assert "Garde-fou de domaine" in cfg.system_prompt, (
        f"Expert '{expert_id}' n'embarque pas le garde-fou domaine (introduit G2 2026-05-16)."
    )


@pytest.mark.parametrize("expert_id", _SPECIALIZED_EXPERTS)
def test_specialized_expert_guardrail_redirects_to_general(expert_id: str) -> None:
    """Tous les 9 experts spécialisés doivent rediriger vers Général."""
    cfg = get_expert_config(expert_id)
    assert "mode Général" in cfg.system_prompt, (
        f"Expert '{expert_id}' ne redirige pas vers le mode Général."
    )


# ══════════════════════════════════════════════════════════════
# Absence du guardrail sur general / studio
# ══════════════════════════════════════════════════════════════


def test_general_expert_has_no_guardrail() -> None:
    """`general` est catch-all par définition — pas de redirection."""
    cfg = get_expert_config("general")
    assert "Garde-fou de domaine" not in cfg.system_prompt


def test_studio_expert_has_no_guardrail() -> None:
    """`studio` est image-only — sa redirection vers général est déjà dans son prompt natif."""
    cfg = get_expert_config("studio")
    assert "Garde-fou de domaine" not in cfg.system_prompt


# ══════════════════════════════════════════════════════════════
# Invariants sécurité préservés (medicine / legal disclaimers)
# ══════════════════════════════════════════════════════════════


def test_medicine_disclaimer_still_present_after_wrap() -> None:
    cfg = get_expert_config("medicine")
    assert cfg.disclaimer is not None
    assert "professionnel" in cfg.disclaimer.lower()


def test_medicine_tools_enabled_after_wrap() -> None:
    """[planner-from-chat LOT 4] `medicine` a `tools_allowed=True` depuis
    la décision produit Ivan ; le wrap guardrail ne doit pas le ré-éteindre."""
    cfg = get_expert_config("medicine")
    assert cfg.tools_allowed is True


def test_legal_disclaimer_still_present_after_wrap() -> None:
    cfg = get_expert_config("legal")
    assert cfg.disclaimer is not None
    assert "avocat" in cfg.disclaimer.lower() or "notaire" in cfg.disclaimer.lower()


def test_legal_tools_enabled_after_wrap() -> None:
    """[planner-from-chat LOT 4] `legal` a `tools_allowed=True` depuis la
    décision produit Ivan ; le wrap guardrail ne doit pas le ré-éteindre."""
    cfg = get_expert_config("legal")
    assert cfg.tools_allowed is True


def test_medicine_emergency_redirect_preserved_after_wrap() -> None:
    """Le bloc « urgences » du prompt médecine doit rester en place après wrap."""
    cfg = get_expert_config("medicine")
    assert "urgence" in cfg.system_prompt.lower()
    assert "urgences" in cfg.system_prompt.lower()


# ══════════════════════════════════════════════════════════════
# Identité NEXYA préservée sur tous les experts
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("expert_id", list(EXPERT_REGISTRY.keys()))
def test_nexya_identity_preserved_on_all_experts(expert_id: str) -> None:
    cfg = get_expert_config(expert_id)
    # Tous les experts (y compris general / studio) embarquent l'identité NEXYA.
    assert "NEXYA" in cfg.system_prompt
    assert "Nexyalabs" in cfg.system_prompt


# ══════════════════════════════════════════════════════════════
# Cohérence : domain_label dans chaque prompt cible le bon métier
# ══════════════════════════════════════════════════════════════


_DOMAIN_LABEL_KEYWORD = {
    "computer": "Informatique",
    "science": "Sciences",
    "finance": "Finance",
    "language": "Langues",
    "cooking": "Cuisine",
    "engineering": "Ingénierie",
    "productivity": "Productivité",
    "medicine": "Médecine",
    "legal": "Légal",
}


@pytest.mark.parametrize("expert_id,keyword", list(_DOMAIN_LABEL_KEYWORD.items()))
def test_guardrail_domain_label_matches_expert(expert_id: str, keyword: str) -> None:
    """Le `domain_label` substitué dans le guardrail doit correspondre au métier."""
    cfg = get_expert_config(expert_id)
    # Le domain_label apparaît au moins 1× dans le bloc guardrail.
    guardrail_idx = cfg.system_prompt.find("Garde-fou de domaine")
    assert guardrail_idx >= 0
    guardrail_block = cfg.system_prompt[guardrail_idx:]
    assert keyword in guardrail_block, (
        f"Expert '{expert_id}' : le bloc guardrail ne mentionne pas '{keyword}'"
    )
