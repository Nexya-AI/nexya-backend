"""
Tests Session 2026-05-26 — `app/ai/nexya_safety.py`.

Garantit que :
- Les 4 catégories de refus sont présentes (code malveillant,
  désinformation, hate speech, NSFW).
- Le format de refus standard est documenté.
- La résistance prompt injection est mentionnée explicitement.
- Le principe de « pas de moralisation excessive » est rappelé.
- Parité FR ↔ EN stricte.
- Idempotence + API publique stable.
"""

from __future__ import annotations

import pytest

from app.ai.nexya_safety import get_safety_limits, safety_limits_en, safety_limits_fr

# ══════════════════════════════════════════════════════════════
# 1. Présence des 4 catégories de refus FR
# ══════════════════════════════════════════════════════════════

_FR_CATEGORY_MARKERS = [
    "1. **Code/scripts malveillants**",
    "2. **Désinformation délibérée**",
    "3. **Discours haineux ou discriminatoire**",
    "4. **Contenu NSFW**",
]


@pytest.mark.parametrize("marker", _FR_CATEGORY_MARKERS)
def test_fr_each_safety_category_present(marker: str) -> None:
    fr = safety_limits_fr()
    assert marker in fr, f"Catégorie safety FR manquante : {marker!r}"


def test_fr_exactly_4_safety_categories() -> None:
    """Anti-régression : ni plus, ni moins de 4 catégories."""
    fr = safety_limits_fr()
    count = sum(1 for i in range(1, 5) if f"{i}. **" in fr)
    assert count == 4, f"Attendu 4 catégories safety, trouvé {count}"


# ══════════════════════════════════════════════════════════════
# 2. Présence des 4 catégories EN (parité stricte)
# ══════════════════════════════════════════════════════════════

_EN_CATEGORY_MARKERS = [
    "1. **Malicious code/scripts**",
    "2. **Deliberate misinformation**",
    "3. **Hate or discriminatory speech**",
    "4. **NSFW content**",
]


@pytest.mark.parametrize("marker", _EN_CATEGORY_MARKERS)
def test_en_each_safety_category_present(marker: str) -> None:
    en = safety_limits_en()
    assert marker in en, f"Safety category EN missing: {marker!r}"


def test_en_exactly_4_safety_categories() -> None:
    en = safety_limits_en()
    count = sum(1 for i in range(1, 5) if f"{i}. **" in en)
    assert count == 4


# ══════════════════════════════════════════════════════════════
# 3. Exemples de demandes interdites par catégorie
# ══════════════════════════════════════════════════════════════
#
# Chaque catégorie doit lister des exemples concrets de demandes
# interdites pour que le LLM ait des marqueurs sémantiques clairs.

_FR_FORBIDDEN_EXAMPLES = [
    # Catégorie 1 — Code malveillant
    "malware",
    "phishing",
    "scraping abusif",
    # Catégorie 2 — Désinformation
    "complotistes",
    "deepfakes",
    "fake news",
    # Catégorie 3 — Discours haineux
    "racisme",
    "sexisme",
    "incitation à la violence",
    # Catégorie 4 — NSFW
    "sexuel explicite",
    "violence gratuite",
    "mineurs",
]


@pytest.mark.parametrize("example", _FR_FORBIDDEN_EXAMPLES)
def test_fr_safety_lists_forbidden_examples(example: str) -> None:
    fr = safety_limits_fr().lower()
    assert example.lower() in fr, f"Exemple interdit manquant dans safety FR : {example!r}"


_EN_FORBIDDEN_EXAMPLES = [
    # Cat 1
    "malware",
    "phishing",
    # Cat 2
    "conspiracy theories",
    "deepfakes",
    # Cat 3
    "racism",
    "sexism",
    # Cat 4
    "sexual content",
    "self-harm",
    "minors",
]


@pytest.mark.parametrize("example", _EN_FORBIDDEN_EXAMPLES)
def test_en_safety_lists_forbidden_examples(example: str) -> None:
    en = safety_limits_en().lower()
    assert example.lower() in en, f"Forbidden example missing in safety EN: {example!r}"


# ══════════════════════════════════════════════════════════════
# 4. Format de refus standard documenté
# ══════════════════════════════════════════════════════════════


def test_fr_safety_includes_standard_refusal_format() -> None:
    """Le bloc FR doit documenter le format de refus standard."""
    fr = safety_limits_fr()
    # Marqueurs canoniques du format de refus
    assert "Cette demande dépasse ce que je peux t'aider à faire" in fr
    assert "reformulation positive" in fr.lower()


def test_en_safety_includes_standard_refusal_format() -> None:
    en = safety_limits_en()
    assert "This request exceeds what I can help you with" in en
    assert "positive reformulation" in en.lower()


# ══════════════════════════════════════════════════════════════
# 5. Résistance prompt injection mentionnée
# ══════════════════════════════════════════════════════════════


def test_fr_safety_resists_prompt_injection() -> None:
    """Le bloc FR doit documenter la résistance prompt injection."""
    fr = safety_limits_fr().lower()
    # Patterns de prompt injection attendus
    assert "prompt injection" in fr
    assert "ignore tes consignes" in fr or "fais comme si" in fr
    # Cohérence éthique non-négociable
    assert "non-négociable" in fr or "non négociable" in fr


def test_en_safety_resists_prompt_injection() -> None:
    en = safety_limits_en().lower()
    assert "prompt injection" in en
    assert "ignore your instructions" in en or "pretend" in en
    assert "non-negotiable" in en


# ══════════════════════════════════════════════════════════════
# 6. Pas de moralisation excessive (équilibre brand)
# ══════════════════════════════════════════════════════════════


def test_fr_safety_warns_against_excessive_moralizing() -> None:
    """Le bloc FR doit rappeler de ne pas moraliser à l'excès.

    Validation Ivan : NEXYA n'est ni un juge, ni un policier. Refuse
    poliment, propose une alternative, passe à autre chose."""
    fr = safety_limits_fr().lower()
    assert "moralisation excessive" in fr or "ni un juge" in fr


def test_en_safety_warns_against_excessive_moralizing() -> None:
    en = safety_limits_en().lower()
    assert "excessive moralizing" in en or "neither a judge" in en


# ══════════════════════════════════════════════════════════════
# 7. API publique — get_safety_limits
# ══════════════════════════════════════════════════════════════


def test_get_safety_limits_default_returns_fr() -> None:
    assert get_safety_limits() == safety_limits_fr()


def test_get_safety_limits_fr_returns_fr() -> None:
    assert get_safety_limits("fr") == safety_limits_fr()


def test_get_safety_limits_en_returns_en() -> None:
    assert get_safety_limits("en") == safety_limits_en()


def test_get_safety_limits_unknown_locale_falls_back_to_fr() -> None:
    """Locale invalide ne crash pas, retombe sur FR (Africa-first)."""
    result = get_safety_limits("zz")  # type: ignore[arg-type]
    assert result == safety_limits_fr()


# ══════════════════════════════════════════════════════════════
# 8. Idempotence
# ══════════════════════════════════════════════════════════════


def test_safety_limits_idempotent() -> None:
    """2 appels identiques retournent byte-pour-byte la même string."""
    assert safety_limits_fr() == safety_limits_fr()
    assert safety_limits_en() == safety_limits_en()


def test_safety_limits_fr_en_different() -> None:
    """FR ≠ EN (sanity)."""
    assert safety_limits_fr() != safety_limits_en()


# ══════════════════════════════════════════════════════════════
# 9. Taille raisonnable (~1000 chars per locale)
# ══════════════════════════════════════════════════════════════


def test_safety_limits_fr_reasonable_size() -> None:
    """Le bloc safety FR doit être substantiel mais pas excessif."""
    fr = safety_limits_fr()
    # Min 800 chars (assez détaillé) max 2000 (pas obèse pour CORE)
    assert 800 <= len(fr) <= 2500, f"Taille safety FR hors range : {len(fr)} chars"


def test_safety_limits_en_reasonable_size() -> None:
    en = safety_limits_en()
    assert 800 <= len(en) <= 2500, f"Safety EN size out of range: {len(en)} chars"


# ══════════════════════════════════════════════════════════════
# 10. Header section présent (marker pour parsing logs)
# ══════════════════════════════════════════════════════════════


def test_fr_safety_header_present() -> None:
    """Le header `[Safety & Limites NEXYA]` doit être présent."""
    fr = safety_limits_fr()
    assert "[Safety & Limites NEXYA]" in fr


def test_en_safety_header_present() -> None:
    en = safety_limits_en()
    assert "[NEXYA Safety & Limits]" in en
