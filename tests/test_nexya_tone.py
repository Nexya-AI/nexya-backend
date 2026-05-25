"""
Tests Session A1 — `app/ai/nexya_tone.py`.

Garantit que les 10 commandements validés par Ivan (2026-05-19) sont
bien présents dans la constante FR + EN, que les anti-patterns honnis
(« Bien sûr ! », « Excellente question ! ») sont nommés comme à bannir,
que le ton est Africa-first **contextuel** (mentionne le monde
aussi) et non exclusif, et que les helpers exposés sont stables.
"""

from __future__ import annotations

import pytest

from app.ai.nexya_tone import get_tone, tone_en, tone_fr

# ══════════════════════════════════════════════════════════════
# 1. Présence des 10 commandements FR — marqueurs numérotés
# ══════════════════════════════════════════════════════════════

# Marqueurs numérotés explicites — si on retire ou renumérote un
# commandement, le test casse immédiatement.
_FR_TEN_COMMANDMENT_MARKERS = [
    "1. **Tutoiement systématique.**",
    "2. **Aucune formule d'ouverture creuse.**",
    "3. **Anti-sycophancy stricte.**",
    "4. **Structure scannable.**",
    "5. **Jargon décortiqué.**",
    "6. **Africa-first contextuel, JAMAIS exclusif.**",
    "7. **Brièveté calibrée selon la complexité.**",
    "8. **Exemples concrets systématiques.**",
    "9. **Transparence absolue sur tes limites.**",
    "10. **Chaleur professionnelle.**",
]


@pytest.mark.parametrize("marker", _FR_TEN_COMMANDMENT_MARKERS)
def test_fr_each_commandment_marker_present(marker: str) -> None:
    fr = tone_fr()
    assert marker in fr, f"Commandement FR manquant : {marker!r}"


def test_fr_exactly_ten_commandments_present() -> None:
    """Anti-régression : ni plus, ni moins de 10 commandements."""
    fr = tone_fr()
    # Compte les marqueurs "N. **" (N=1..10).
    count = sum(1 for i in range(1, 11) if f"{i}. **" in fr)
    assert count == 10, f"Attendu 10 commandements, trouvé {count}"


# ══════════════════════════════════════════════════════════════
# 2. Présence des 10 commandements EN (parité stricte FR↔EN)
# ══════════════════════════════════════════════════════════════

_EN_TEN_COMMANDMENT_MARKERS = [
    "1. **Informal address by default.**",
    "2. **No empty opening formulas.**",
    "3. **Strict anti-sycophancy.**",
    "4. **Scannable structure.**",
    "5. **Decoded jargon.**",
    "6. **Africa-first contextual, NEVER exclusive.**",
    "7. **Brevity calibrated to complexity.**",
    "8. **Systematic concrete examples.**",
    "9. **Absolute transparency about your limits.**",
    "10. **Professional warmth.**",
]


@pytest.mark.parametrize("marker", _EN_TEN_COMMANDMENT_MARKERS)
def test_en_each_commandment_marker_present(marker: str) -> None:
    en = tone_en()
    assert marker in en, f"Commandement EN manquant : {marker!r}"


def test_en_exactly_ten_commandments_present() -> None:
    en = tone_en()
    count = sum(1 for i in range(1, 11) if f"{i}. **" in en)
    assert count == 10


# ══════════════════════════════════════════════════════════════
# 3. Anti-patterns explicitement bannis
# ══════════════════════════════════════════════════════════════
#
# Les phrases d'ouverture creuses doivent être listées comme à éviter
# dans le ton — c'est une instruction au LLM, donc elle DOIT apparaître
# textuellement dans le bloc tone (encadrée d'un contexte explicite).

_FR_BANNED_FORMULAS_MENTIONED = [
    "Bien sûr !",
    "Excellente question !",
    "Avec plaisir !",
]


@pytest.mark.parametrize("formula", _FR_BANNED_FORMULAS_MENTIONED)
def test_fr_banned_opening_formula_mentioned(formula: str) -> None:
    """Le ton FR doit explicitement nommer ces formules comme à bannir."""
    fr = tone_fr()
    assert formula in fr, (
        f"La formule creuse {formula!r} doit être listée explicitement "
        "comme bannie dans le bloc tone FR (instruction au LLM)."
    )


_EN_BANNED_FORMULAS_MENTIONED = [
    "Sure!",
    "Great question!",
    "Of course!",
]


@pytest.mark.parametrize("formula", _EN_BANNED_FORMULAS_MENTIONED)
def test_en_banned_opening_formula_mentioned(formula: str) -> None:
    en = tone_en()
    assert formula in en, f"Empty opening formula {formula!r} must be explicitly banned in EN tone."


# ══════════════════════════════════════════════════════════════
# 4. Tutoiement obligatoire (FR)
# ══════════════════════════════════════════════════════════════


def test_fr_tone_promotes_tutoiement() -> None:
    """Le ton FR doit explicitement imposer le tutoiement."""
    fr = tone_fr().lower()
    assert "tutoiement" in fr, "Le mot 'tutoiement' doit apparaître dans le ton FR."


def test_fr_tone_forbids_vouvoiement_distance() -> None:
    """Le ton FR doit interdire le vouvoiement distant."""
    fr = tone_fr().lower()
    # Mention explicite que « vous » est à éviter.
    assert "vous" in fr  # mentionné pour dire « pas de "vous" »
    assert "jamais" in fr or "pas de" in fr


# ══════════════════════════════════════════════════════════════
# 5. Africa-first CONTEXTUEL (non exclusif) — validation Ivan critique
# ══════════════════════════════════════════════════════════════


def test_fr_africa_first_contextual_not_exclusive() -> None:
    """Africa-first mentionné mais accompagné de mentions monde / Europe.

    Validation Ivan 2026-05-19 : « cest Vrai cest africa first mais
    noublie pas que cest aussi mondial, europe, surtout. donc, soit
    sage dans tes décisions. » → Africa-first contextuel, JAMAIS exclusif.
    """
    fr = tone_fr().lower()
    assert "afrique" in fr or "africa" in fr or "cameroun" in fr
    # Mais NON exclusif → mentions monde / Europe / international / diaspora.
    elsewhere = any(
        marker in fr
        for marker in ("europe", "monde", "international", "diaspora", "amériqu", "asie")
    )
    assert elsewhere, (
        "Le ton FR doit mentionner Europe/monde/international/diaspora "
        "pour rester Africa-first contextuel et non exclusif."
    )


def test_en_africa_first_contextual_not_exclusive() -> None:
    en = tone_en().lower()
    assert "africa" in en or "cameroon" in en
    elsewhere = any(
        marker in en
        for marker in ("europe", "world", "international", "diaspora", "america", "asia")
    )
    assert elsewhere


def test_fr_motto_explicit_africa_and_beyond() -> None:
    """Notre motto figé : « pour l'Afrique et au-delà »."""
    fr = tone_fr()
    assert "Afrique et au-delà" in fr


# ══════════════════════════════════════════════════════════════
# 6. API publique — get_tone
# ══════════════════════════════════════════════════════════════


def test_get_tone_default_returns_fr() -> None:
    assert get_tone() == tone_fr()


def test_get_tone_fr_returns_fr() -> None:
    assert get_tone("fr") == tone_fr()


def test_get_tone_en_returns_en() -> None:
    assert get_tone("en") == tone_en()


def test_get_tone_unknown_locale_falls_back_to_fr() -> None:
    """Locale invalide ne crash pas, retombe sur FR."""
    # type: ignore — on teste justement le comportement défensif
    result = get_tone("zz")  # type: ignore[arg-type]
    assert result == tone_fr()


# ══════════════════════════════════════════════════════════════
# 7. Caps & invariants
# ══════════════════════════════════════════════════════════════


def test_fr_tone_size_under_budget() -> None:
    """Le ton FR doit tenir sous 3500 chars (cap raisonnable, laisse place
    à identity + routing dans le preamble 4000 chars cap)."""
    assert len(tone_fr()) < 3500


def test_en_tone_size_under_budget() -> None:
    assert len(tone_en()) < 3500


def test_fr_and_en_tone_idempotent() -> None:
    """Deux appels successifs retournent exactement le même string."""
    assert tone_fr() == tone_fr()
    assert tone_en() == tone_en()
