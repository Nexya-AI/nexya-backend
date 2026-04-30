"""
Tests — moderation_rules (brique B2).

Couverture adversariale :
1. Prescription nominative : refus sur diverses formulations FR.
2. Info médicale générale (pas de posologie) : accepté.
3. Rédaction d'acte juridique nominatif : refus.
4. Info juridique générale (articles de loi) : accepté.
5. Kill-switch `moderation_rules_enabled=False` : tout passe.
6. Texte vide / None : allow.
7. Whitelist vide au lancement B2 — medicine et legal refusent aussi.
"""

from __future__ import annotations

import pytest

from app.ai.moderation_rules import (
    ModerationRuleDecision,
    check_business_rules,
)

# ══════════════════════════════════════════════════════════════
# Catégorie 1 — Prescription nominative
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text",
    [
        "Prescris-moi 40 mg d'amoxicilline par jour.",
        "Quelle posologie de doliprane 500 mg pour un enfant ?",
        "Peux-tu me donner l'ordonnance pour mon fils ?",
        "Combien de mg d'ibuprofène devrais-je prendre ?",
        "Rédige une ordonnance pour ma fille.",
        "Quelle dose à prendre, 500 mg ou 1000 mg ?",
    ],
)
def test_prescription_nominative_is_rejected(text):
    decision = check_business_rules(text=text, expert_id="general")
    assert decision.allowed is False
    assert decision.reason == "prescription_nominative"
    assert decision.message is not None
    assert (
        "professionnel de santé" in decision.message.lower()
        or "médecin" in decision.message.lower()
    )


@pytest.mark.parametrize(
    "text",
    [
        "Quels sont les effets secondaires courants de l'amoxicilline ?",
        "Comment fonctionne le paracétamol dans le corps ?",
        "C'est quoi la tension artérielle normale ?",
        "Quels sont les symptômes de la grippe ?",
        "Pourquoi a-t-on de la fièvre quand on est malade ?",
    ],
)
def test_medical_general_info_is_allowed(text):
    decision = check_business_rules(text=text, expert_id="general")
    assert decision.allowed is True


def test_prescription_rejected_even_on_medicine_expert():
    """Whitelist vide au lancement B2 → même medicine refuse les prescriptions."""
    decision = check_business_rules(
        text="Prescris-moi 40 mg de doliprane.",
        expert_id="medicine",
    )
    assert decision.allowed is False
    assert decision.reason == "prescription_nominative"


# ══════════════════════════════════════════════════════════════
# Catégorie 2 — Rédaction d'acte juridique nominatif
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "text",
    [
        "Rédige un contrat de travail entre moi et ma société.",
        "Écris-moi une mise en demeure pour mon voisin.",
        "Prépare un bail commercial pour mon local.",
        "Produis-moi une lettre de licenciement.",
        "Génère un testament en ma faveur.",
        "Prépare les statuts de ma SARL.",
    ],
)
def test_legal_act_drafting_is_rejected(text):
    decision = check_business_rules(text=text, expert_id="general")
    assert decision.allowed is False
    assert decision.reason == "legal_act_drafting"
    assert "avocat" in decision.message.lower() or "notaire" in decision.message.lower()


@pytest.mark.parametrize(
    "text",
    [
        "Explique-moi l'article 1134 du code civil.",
        "Quelles sont les étapes d'une procédure de divorce ?",
        "C'est quoi un contrat de travail à durée indéterminée ?",
        "Quelles clauses trouve-t-on dans un bail commercial ?",
    ],
)
def test_legal_general_info_is_allowed(text):
    decision = check_business_rules(text=text, expert_id="general")
    assert decision.allowed is True


def test_legal_act_rejected_even_on_legal_expert():
    """Whitelist vide → même legal refuse la rédaction d'actes."""
    decision = check_business_rules(
        text="Rédige un contrat entre Monsieur Dupont et moi.",
        expert_id="legal",
    )
    assert decision.allowed is False
    assert decision.reason == "legal_act_drafting"


# ══════════════════════════════════════════════════════════════
# Kill-switch + cas limites
# ══════════════════════════════════════════════════════════════


def test_kill_switch_disables_all_rules(monkeypatch):
    from app.ai import moderation_rules as mr_module

    monkeypatch.setattr(mr_module.settings, "moderation_rules_enabled", False)
    decision = check_business_rules(
        text="Prescris-moi 40 mg d'amoxicilline.",
        expert_id="general",
    )
    assert decision.allowed is True


def test_empty_text_is_allowed():
    decision = check_business_rules(text="", expert_id="general")
    assert decision.allowed is True


def test_decision_default_allowed():
    """Un texte neutre passe sans info particulière."""
    decision = check_business_rules(
        text="Bonjour, comment ça va ?",
        expert_id="general",
    )
    assert decision.allowed is True
    assert decision.reason is None
    assert decision.message is None


# ══════════════════════════════════════════════════════════════
# Sérialisation de décision
# ══════════════════════════════════════════════════════════════


def test_decision_kind_defaults_to_input():
    decision = ModerationRuleDecision(allowed=True)
    assert decision.kind == "input"


def test_rejected_decision_preserves_kind():
    decision = check_business_rules(
        text="Rédige un contrat entre moi et Monsieur Dupont.",
        expert_id="general",
        kind="output",
    )
    assert decision.allowed is False
    assert decision.kind == "output"
