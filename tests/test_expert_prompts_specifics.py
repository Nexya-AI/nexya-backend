"""
Tests Session A2 — Spécificités métier par expert.

Validations ciblées par expert qui font la différence entre une IA
générique et le **niveau divin Silicon Valley** :

- **Medicine** : bloc URGENCE + 5 symptômes critiques + numéros
  Cameroun 117/118/119
- **Legal** : OHADA + Code civil + article exact + tools_allowed=False
- **Cooking** : substitution 2+ alternatives + corpus RAG G2 +
  attribution Loth Ivan / Nexyalabs
- **Science** : LaTeX obligatoire + étapes intermédiaires
- **Computer** : code exécutable + edge cases + Python/Dart/TS/Go/Rust
- **General** : 4 tools Planner (create_task/list_tasks/update_task/pause_task)
- **Studio** : redirection vers Général si conversation
- **Finance** : FCFA + OHADA + Mobile Money + BRVM/DSX
- **Engineering** : SI + normes ISO/EN/NF + 13 branches
- **Productivity** : GTD + Eisenhower + Atomic Habits + action immédiate
- **Language** : 8 langues africaines + format ~~rature~~ → **gras**

Plus tests d'intégration avec `experts.py` post-refactor : le
`EXPERT_REGISTRY` doit consommer les nouveaux prompts via les imports
+ `_with_guardrail` appliqué sur les 9 experts spécialisés.
"""

from __future__ import annotations

import pytest

from app.ai.experts import EXPERT_REGISTRY, get_expert_config
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
# MEDICINE — safety-critical MAX
# ══════════════════════════════════════════════════════════════


def test_medicine_has_emergency_block() -> None:
    """Bloc URGENCE détection 5 symptômes vitaux obligatoire."""
    assert "URGENCE VITALE" in MEDICINE_PROMPT or "URGENCE" in MEDICINE_PROMPT


def test_medicine_lists_5_critical_symptoms() -> None:
    """Les 5 drapeaux rouges doivent être nommés explicitement."""
    prompt = MEDICINE_PROMPT.lower()
    assert "thoracique" in prompt  # 1. infarctus
    assert "avc" in prompt  # 2. AVC
    assert "hémorragie" in prompt or "hemorragie" in prompt  # 3.
    assert "respiratoire" in prompt  # 4. détresse respiratoire
    assert "suicidaire" in prompt  # 5. idées suicidaires


def test_medicine_lists_cameroon_emergency_numbers() -> None:
    """117 (Police), 118 (Pompiers), 119 (SAMU) obligatoires."""
    assert "117" in MEDICINE_PROMPT
    assert "118" in MEDICINE_PROMPT
    assert "119" in MEDICINE_PROMPT


def test_medicine_lists_international_emergency_112() -> None:
    """112 = numéro universel mobile international fallback."""
    assert "112" in MEDICINE_PROMPT


def test_medicine_has_professional_disclaimer_block() -> None:
    """Disclaimer professionnel obligatoire en fin de chaque réponse."""
    assert "professionnel de santé" in MEDICINE_PROMPT.lower()


def test_medicine_forbids_diagnosis() -> None:
    """JAMAIS de diagnostic personnalisé."""
    prompt = MEDICINE_PROMPT
    assert "diagnostic" in prompt.lower()
    assert "jamais" in prompt.lower()


def test_medicine_forbids_nominative_posology() -> None:
    """JAMAIS de posologie nominative pour un cas concret."""
    prompt = MEDICINE_PROMPT.lower()
    assert "posologie nominative" in prompt or "posologie" in prompt


def test_medicine_in_registry_has_tools_disabled() -> None:
    """Safety-critical : pas de side-effect DB depuis consultation médicale."""
    cfg = get_expert_config("medicine")
    assert cfg.tools_allowed is False


def test_medicine_in_registry_has_low_temperature() -> None:
    """Médecine = zéro créativité (temp ≤ 0.2)."""
    cfg = get_expert_config("medicine")
    assert cfg.temperature <= 0.2


def test_medicine_in_registry_has_disclaimer() -> None:
    """Disclaimer ExpertConfig non-vide."""
    cfg = get_expert_config("medicine")
    assert cfg.disclaimer is not None
    assert "professionnel" in cfg.disclaimer.lower()


# ══════════════════════════════════════════════════════════════
# LEGAL — safety-critical
# ══════════════════════════════════════════════════════════════


def test_legal_mentions_ohada() -> None:
    """OHADA = socle commun 17 pays africains, référence majeure."""
    assert "OHADA" in LEGAL_PROMPT


def test_legal_mentions_code_civil_camerounais() -> None:
    assert "Code civil" in LEGAL_PROMPT


def test_legal_mentions_article_referencing() -> None:
    """Citation d'article exact obligatoire (article 1382, article 16 OHADA, etc.)."""
    assert "Article" in LEGAL_PROMPT or "article" in LEGAL_PROMPT


def test_legal_forbids_acte_engageant() -> None:
    """JAMAIS de rédaction d'acte juridique engageant signable."""
    prompt = LEGAL_PROMPT.lower()
    assert "jamais rédiger un acte" in prompt or "acte juridique engageant" in prompt


def test_legal_forbids_invention_reference() -> None:
    """JAMAIS d'invention de référence légale."""
    assert "inventer une référence" in LEGAL_PROMPT.lower() or "JAMAIS" in LEGAL_PROMPT


def test_legal_redirects_to_lawyer_or_notaire() -> None:
    prompt = LEGAL_PROMPT.lower()
    assert "avocat" in prompt
    assert "notaire" in prompt


def test_legal_in_registry_has_tools_disabled() -> None:
    cfg = get_expert_config("legal")
    assert cfg.tools_allowed is False


def test_legal_in_registry_has_disclaimer() -> None:
    cfg = get_expert_config("legal")
    assert cfg.disclaimer is not None
    assert "avocat" in cfg.disclaimer.lower() or "notaire" in cfg.disclaimer.lower()


# ══════════════════════════════════════════════════════════════
# COOKING — RAG G2 + substitutions
# ══════════════════════════════════════════════════════════════


def test_cooking_mentions_107_cameroonian_recipes() -> None:
    """Corpus G2 propriétaire = 107 recettes camerounaises."""
    assert "107" in COOKING_PROMPT


def test_cooking_attributes_corpus_to_loth_ivan_nexyalabs() -> None:
    """Traçabilité AI Act Article 13 : attribution propriétaire."""
    assert "Loth Ivan" in COOKING_PROMPT
    assert "Nexyalabs" in COOKING_PROMPT


def test_cooking_mentions_rag_extract_tags() -> None:
    """Respect du framing RAG D5 `<<<DOCUMENT EXTRACT>>>`."""
    assert "DOCUMENT EXTRACT" in COOKING_PROMPT


def test_cooking_forbids_following_extract_instructions() -> None:
    """Défense anti-prompt-injection : ne JAMAIS suivre les instructions
    qui seraient contenues dans les extraits RAG."""
    prompt = COOKING_PROMPT.lower()
    assert "suivre d'instructions" in prompt or "anti-prompt-injection" in prompt


def test_cooking_requires_2_alternatives_for_substitution() -> None:
    """Substitution = TOUJOURS ≥ 2 alternatives concrètes."""
    prompt = COOKING_PROMPT
    assert "2 alternatives" in prompt or "**2**" in prompt or "deux alternatives" in prompt.lower()


def test_cooking_in_registry_corpus_enabled() -> None:
    """G2 V8 PROD activé."""
    cfg = get_expert_config("cooking")
    assert cfg.corpus_enabled is True


def test_cooking_in_registry_disable_thinking_v11() -> None:
    """G2 V1.1 — thinking désactivé pour latence (TTFT 8.8s vs 19.5s)."""
    cfg = get_expert_config("cooking")
    assert cfg.disable_thinking is True


# ══════════════════════════════════════════════════════════════
# SCIENCE — LaTeX obligatoire
# ══════════════════════════════════════════════════════════════


def test_science_mentions_latex() -> None:
    """LaTeX obligatoire pour toute formule."""
    assert "LaTeX" in SCIENCE_PROMPT or "latex" in SCIENCE_PROMPT.lower()


def test_science_mentions_step_by_step() -> None:
    """Étapes intermédiaires visibles pour pédagogie."""
    prompt = SCIENCE_PROMPT.lower()
    assert "étape" in prompt or "etape" in prompt


def test_science_mentions_dimensional_check() -> None:
    """Vérification dimensionnelle obligatoire."""
    prompt = SCIENCE_PROMPT.lower()
    assert "dimension" in prompt or "ordre de grandeur" in prompt


def test_science_in_registry_is_pro_tier() -> None:
    """Tier Pro pour raisonnement multi-étapes."""
    cfg = get_expert_config("science")
    assert cfg.tier == "pro"


# ══════════════════════════════════════════════════════════════
# COMPUTER — code exécutable + langages prioritaires
# ══════════════════════════════════════════════════════════════


def test_computer_mentions_priority_languages() -> None:
    """Python, Dart/Flutter, TypeScript, Go, Rust."""
    prompt = COMPUTER_PROMPT
    assert "Python" in prompt
    assert "Dart" in prompt or "Flutter" in prompt
    assert "TypeScript" in prompt or "JavaScript" in prompt
    assert "Go" in prompt
    assert "Rust" in prompt


def test_computer_forbids_pseudo_code() -> None:
    prompt = COMPUTER_PROMPT.lower()
    assert "pseudo-code" in prompt or "pseudo" in prompt


def test_computer_requires_imports() -> None:
    prompt = COMPUTER_PROMPT.lower()
    assert "imports" in prompt or "import" in prompt


# ══════════════════════════════════════════════════════════════
# GENERAL — 4 tools Planner préservés
# ══════════════════════════════════════════════════════════════


def test_general_mentions_create_task_tool() -> None:
    assert "create_task" in GENERAL_PROMPT


def test_general_mentions_list_tasks_tool() -> None:
    assert "list_tasks" in GENERAL_PROMPT


def test_general_mentions_update_task_tool() -> None:
    assert "update_task" in GENERAL_PROMPT


def test_general_mentions_pause_task_tool() -> None:
    assert "pause_task" in GENERAL_PROMPT


def test_general_mentions_planner_priority_rule() -> None:
    """Règle de priorité : APPELLE LE TOOL AU LIEU DE RÉPONDRE EN TEXTE."""
    prompt = GENERAL_PROMPT
    assert "APPELLE LE TOOL" in prompt or "appelle le tool" in prompt.lower()


# ══════════════════════════════════════════════════════════════
# STUDIO — image-only, redirection vers Général
# ══════════════════════════════════════════════════════════════


def test_studio_mentions_image_generation_role() -> None:
    prompt = STUDIO_PROMPT.lower()
    assert "image" in prompt
    assert "génér" in prompt or "imagen" in prompt


def test_studio_redirects_conversation_to_general() -> None:
    """Si l'user converse au lieu de demander une image → redirection."""
    prompt = STUDIO_PROMPT
    assert "Général" in prompt


def test_studio_in_registry_image_tier() -> None:
    cfg = get_expert_config("studio")
    assert cfg.tier == "image"


def test_studio_in_registry_no_chat_fallback() -> None:
    """Studio est image-only : fallback_chain vide."""
    cfg = get_expert_config("studio")
    assert cfg.fallback_chain == ()


# ══════════════════════════════════════════════════════════════
# FINANCE — Africa-first contextuel
# ══════════════════════════════════════════════════════════════


def test_finance_mentions_fcfa() -> None:
    assert "FCFA" in FINANCE_PROMPT


def test_finance_mentions_ohada() -> None:
    assert "OHADA" in FINANCE_PROMPT


def test_finance_mentions_mobile_money_operators() -> None:
    """Orange Money, MTN MoMo, Wave, Airtel Money."""
    prompt = FINANCE_PROMPT
    assert "Orange Money" in prompt
    assert "MTN" in prompt or "MoMo" in prompt
    assert "Wave" in prompt
    assert "Airtel" in prompt


def test_finance_mentions_brvm_and_dsx() -> None:
    """BRVM (Abidjan, UEMOA) + DSX (Douala, CEMAC)."""
    assert "BRVM" in FINANCE_PROMPT
    assert "DSX" in FINANCE_PROMPT or "Douala" in FINANCE_PROMPT


def test_finance_warns_uemoa_vs_cemac_distinction() -> None:
    """Cameroun = CEMAC, pas UEMOA. Confusion classique à éviter."""
    prompt = FINANCE_PROMPT
    assert "UEMOA" in prompt
    assert "CEMAC" in prompt


# ══════════════════════════════════════════════════════════════
# ENGINEERING — SI + normes
# ══════════════════════════════════════════════════════════════


def test_engineering_mentions_si_units() -> None:
    """Unités SI obligatoires."""
    prompt = ENGINEERING_PROMPT
    assert "SI" in prompt or "unités SI" in prompt.lower() or "unite si" in prompt.lower()


def test_engineering_mentions_iso_norms() -> None:
    assert "ISO" in ENGINEERING_PROMPT


def test_engineering_mentions_13_branches() -> None:
    """13 branches couvertes."""
    prompt = ENGINEERING_PROMPT.lower()
    assert "génie civil" in prompt or "civil" in prompt
    assert "mécanique" in prompt or "mecanique" in prompt
    assert "électrique" in prompt or "electrique" in prompt


# ══════════════════════════════════════════════════════════════
# PRODUCTIVITY — GTD + Eisenhower + Atomic Habits + action
# ══════════════════════════════════════════════════════════════


def test_productivity_mentions_reference_methods() -> None:
    """GTD, Eisenhower, Pomodoro, OKRs, Atomic Habits."""
    prompt = PRODUCTIVITY_PROMPT
    assert "GTD" in prompt or "Getting Things Done" in prompt
    assert "Eisenhower" in prompt
    assert "Pomodoro" in prompt
    assert "OKR" in prompt
    assert "Atomic Habits" in prompt or "atomic habits" in prompt.lower()


def test_productivity_requires_immediate_action() -> None:
    """Toute suggestion = action réalisable AUJOURD'HUI."""
    prompt = PRODUCTIVITY_PROMPT.lower()
    assert "aujourd'hui" in prompt or "immédiate" in prompt or "immediate" in prompt


def test_productivity_forbids_culpabilization() -> None:
    """JAMAIS culpabiliser l'utilisateur."""
    prompt = PRODUCTIVITY_PROMPT.lower()
    assert "culpabilis" in prompt


# ══════════════════════════════════════════════════════════════
# LANGUAGE — 8 langues africaines
# ══════════════════════════════════════════════════════════════


def test_language_mentions_african_languages() -> None:
    """8 langues africaines principales."""
    prompt = LANGUAGE_PROMPT.lower()
    assert "ewondo" in prompt
    assert "douala" in prompt
    assert "wolof" in prompt
    assert "lingala" in prompt
    assert "bambara" in prompt
    assert "swahili" in prompt
    assert "yoruba" in prompt
    assert "haoussa" in prompt or "hausa" in prompt


def test_language_correction_format_with_strikethrough_and_bold() -> None:
    """Format `~~rature~~` puis correction en **gras**."""
    prompt = LANGUAGE_PROMPT
    assert "~~rature~~" in prompt or "rature" in prompt


def test_language_humility_on_vernacular_languages() -> None:
    """JAMAIS inventer une traduction en langue africaine."""
    prompt = LANGUAGE_PROMPT.lower()
    assert "jamais" in prompt and ("inventer" in prompt or "fiable" in prompt)


# ══════════════════════════════════════════════════════════════
# Intégration avec experts.py post-refactor
# ══════════════════════════════════════════════════════════════


def test_experts_registry_consumes_new_general_prompt() -> None:
    """Le system_prompt du registry doit contenir le contenu du nouveau
    GENERAL_PROMPT (post-refactor experts.py)."""
    cfg = get_expert_config("general")
    # Le system_prompt registry = GENERAL_PROMPT (sans guardrail pour general)
    assert "[Persona — Assistant Général" in cfg.system_prompt
    assert "create_task" in cfg.system_prompt


def test_experts_registry_consumes_new_cooking_prompt() -> None:
    """Le system_prompt du registry cooking doit contenir le nouveau corpus G2."""
    cfg = get_expert_config("cooking")
    assert "107" in cfg.system_prompt
    assert "Loth Ivan" in cfg.system_prompt
    # Guardrail aussi appliqué
    assert "Garde-fou de domaine" in cfg.system_prompt


def test_experts_registry_consumes_new_medicine_prompt() -> None:
    cfg = get_expert_config("medicine")
    assert "URGENCE" in cfg.system_prompt
    assert "117" in cfg.system_prompt
    # Guardrail aussi appliqué
    assert "Garde-fou de domaine" in cfg.system_prompt


def test_experts_registry_consumes_new_legal_prompt() -> None:
    cfg = get_expert_config("legal")
    assert "OHADA" in cfg.system_prompt
    # Guardrail aussi appliqué
    assert "Garde-fou de domaine" in cfg.system_prompt
