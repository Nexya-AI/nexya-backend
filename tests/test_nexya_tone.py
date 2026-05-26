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
# Mise à jour 2026-05-26 :
# - #2 reformulé « Pas de bruit social vide » (interdit feedback social
#   creux MAIS autorise invitations chaleureuses à l'action).
# - #7 reformulé « Profondeur calibrée selon la complexité » (cohérent
#   avec le routing #1 sur les questions hors-domaine).
_FR_TEN_COMMANDMENT_MARKERS = [
    "1. **Tutoiement systématique.**",
    "2. **Pas de bruit social vide.**",
    "3. **Anti-sycophancy stricte.**",
    "4. **Structure scannable.**",
    "5. **Jargon décortiqué.**",
    "6. **Africa-first contextuel, JAMAIS exclusif.**",
    "7. **Profondeur calibrée selon la complexité, pas selon une règle absolue.**",
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
    "2. **No empty social noise.**",
    "3. **Strict anti-sycophancy.**",
    "4. **Scannable structure.**",
    "5. **Decoded jargon.**",
    "6. **Africa-first contextual, NEVER exclusive.**",
    "7. **Depth calibrated to complexity, not to an absolute rule.**",
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
# 3.bis Nuance tone #2 — autorise invitations à l'action (2026-05-26)
# ══════════════════════════════════════════════════════════════
#
# Décision Ivan 2026-05-26 : le tone #2 interdit les formules creuses
# (« Bien sûr ! ») mais AUTORISE explicitement les invitations
# chaleureuses à l'action (« Comment puis-je t'aider aujourd'hui ? »).
# Distinction : feedback social vide RÉAGIT au user en sycophancy,
# invitation à l'action OUVRE la conversation utilement.


_FR_AUTHORIZED_ACTION_INVITATIONS = [
    "Comment puis-je t'aider",
    "En quoi puis-je t'être utile",
]


@pytest.mark.parametrize("invitation", _FR_AUTHORIZED_ACTION_INVITATIONS)
def test_fr_tone_authorizes_action_invitation(invitation: str) -> None:
    """Le ton FR doit explicitement autoriser les invitations chaleureuses à l'action."""
    fr = tone_fr()
    assert invitation in fr, (
        f"L'invitation à l'action {invitation!r} doit être explicitement "
        "autorisée dans le tone #2 FR (instruction au LLM)."
    )


def test_fr_tone_distinguishes_feedback_vs_invitation() -> None:
    """Le tone #2 FR doit documenter la distinction entre :
    - feedback social vide (banni)
    - invitation à l'action (autorisée)"""
    fr = tone_fr()
    # Marqueurs sémantiques attendus dans la nuance
    assert "réagit" in fr.lower() or "RÉAGIT" in fr
    assert "ouvre" in fr.lower() or "OUVRE" in fr


_EN_AUTHORIZED_ACTION_INVITATIONS = [
    "How can I help you today",
    "What can I do for you",
]


@pytest.mark.parametrize("invitation", _EN_AUTHORIZED_ACTION_INVITATIONS)
def test_en_tone_authorizes_action_invitation(invitation: str) -> None:
    """Parité EN : invitations chaleureuses à l'action autorisées."""
    en = tone_en()
    assert invitation in en


def test_en_tone_distinguishes_feedback_vs_invitation() -> None:
    """Parité EN : distinction feedback vide vs invitation action."""
    en = tone_en()
    assert "reacts" in en.lower() or "REACTS" in en
    assert "opens" in en.lower() or "OPENS" in en


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
    """Le ton FR doit tenir sous 4500 chars.

    Note 2026-05-26 (Two-Tier) — cap relevé de 3500 → 4500 chars suite
    à la nuance #2 (autorisation invitation action) et #7 (profondeur
    calibrée), enrichissements validés Ivan. Le preamble cap global
    25000 chars laisse largement la place à tone + identity + routing
    + safety."""
    assert len(tone_fr()) < 4500


def test_en_tone_size_under_budget() -> None:
    """Parité EN : tone EN ≤ 4500 chars (post-Two-Tier 2026-05-26)."""
    assert len(tone_en()) < 4500


def test_fr_and_en_tone_idempotent() -> None:
    """Deux appels successifs retournent exactement le même string."""
    assert tone_fr() == tone_fr()
    assert tone_en() == tone_en()
