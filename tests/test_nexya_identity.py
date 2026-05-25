"""
Tests Session A1 — `app/ai/nexya_identity.py`.

Garantit que :
- Les 4 paliers progressifs du fondateur sont présents (palier 1 base,
  palier 2 Nexyalabs+Loth Ivan, palier 3 bio enrichie, palier 4 mission).
- Anti-hallucination biographique stricte (aucune info inventée comme
  « le plus grand ingénieur africain », pas d'âge inventé, pas
  d'université imaginée).
- Pas d'exposition de coordonnées personnelles fondateur (email, phone).
- Sécurité brand technique : esquive divulgation modèle/provider.
- 11 modes experts mentionnés (6 actifs + 5 coming soon).
- 15 features magnifiques présentes.
- Parité FR ↔ EN sur les sections critiques.
"""

from __future__ import annotations

import pytest

from app.ai.nexya_identity import (
    get_brand_security,
    get_founder_story,
    get_identity,
    get_magnificent_features,
    get_product_description,
)

# ══════════════════════════════════════════════════════════════
# 1. Histoire fondateur — 4 paliers progressifs (FR)
# ══════════════════════════════════════════════════════════════

_FR_FOUNDER_TIER_MARKERS = [
    "**Palier 1",
    "**Palier 2",
    "**Palier 3",
    "**Palier 4",
]


@pytest.mark.parametrize("marker", _FR_FOUNDER_TIER_MARKERS)
def test_fr_founder_4_tiers_present(marker: str) -> None:
    story = get_founder_story("fr")
    assert marker in story, f"Palier fondateur manquant : {marker!r}"


_EN_FOUNDER_TIER_MARKERS = [
    "**Tier 1",
    "**Tier 2",
    "**Tier 3",
    "**Tier 4",
]


@pytest.mark.parametrize("marker", _EN_FOUNDER_TIER_MARKERS)
def test_en_founder_4_tiers_present(marker: str) -> None:
    story = get_founder_story("en")
    assert marker in story


# ══════════════════════════════════════════════════════════════
# 2. Anti-hallucination biographique — STRICT
# ══════════════════════════════════════════════════════════════
#
# Validation Ivan 2026-05-19 : « tu redige une bio enrichie de ma
# personne uniquement avec des infos vraies, donc absolument rien sur
# moi qui ne soit pas une réalité ». Faits autorisés UNIQUEMENT :
#   - nom complet : Loth Ivan Ngassa Yimga
#   - profession : développeur Flutter
#   - pays : Cameroun / camerounais
#   - rôle : fondateur Nexyalabs
#   - création : NEXYA AI


def test_fr_founder_mentions_loth_ivan_ngassa_yimga() -> None:
    """Le nom complet du fondateur doit apparaître exactement (palier 2 ou 3)."""
    story = get_founder_story("fr")
    assert "Loth Ivan Ngassa Yimga" in story


def test_en_founder_mentions_loth_ivan_ngassa_yimga() -> None:
    story = get_founder_story("en")
    assert "Loth Ivan Ngassa Yimga" in story


def test_fr_founder_mentions_nexyalabs() -> None:
    story = get_founder_story("fr")
    assert "Nexyalabs" in story


def test_fr_founder_mentions_cameroon() -> None:
    """Cameroun (FR) doit apparaître au palier 2 ou 3."""
    story = get_founder_story("fr")
    assert "camerounais" in story.lower() or "Cameroun" in story


def test_en_founder_mentions_cameroon() -> None:
    story = get_founder_story("en")
    assert "Cameroon" in story or "Cameroonian" in story


def test_fr_founder_mentions_flutter() -> None:
    story = get_founder_story("fr")
    assert "Flutter" in story


# ─── HALLUCINATIONS INTERDITES (test négatif) ────────────────


_FR_FORBIDDEN_HALLUCINATIONS = [
    "le plus grand",  # superlatif ego-flattant
    "génie",  # qualificatif inventé
    "visionnaire",  # qualificatif inventé
    "Stanford",  # université inventée
    "MIT",  # université inventée
    "Harvard",
    "diplôme",  # parcours inventé
    "milliardaire",
    "fortune",
    "milliards",
]


@pytest.mark.parametrize("hallucination", _FR_FORBIDDEN_HALLUCINATIONS)
def test_fr_founder_no_hallucinated_facts(hallucination: str) -> None:
    """Anti-hallucination biographique : ces termes sont interdits dans
    la bio fondateur car non documentés dans les mémoires Ivan."""
    story = get_founder_story("fr")
    assert hallucination.lower() not in story.lower(), (
        f"Terme hallucination potentielle détecté : {hallucination!r}"
    )


# ─── PROTECTION VIE PRIVÉE (email, phone) ──────────────────


_FORBIDDEN_PRIVATE_CONTACTS = [
    "ngassayimgal@gmail.com",
    "ngassaloth@gmail.com",
    "697298520",
    "+237 697",
    "697 298 520",
]


@pytest.mark.parametrize("contact", _FORBIDDEN_PRIVATE_CONTACTS)
def test_no_private_contact_exposed_anywhere(contact: str) -> None:
    """Aucune coordonnée personnelle Ivan ne doit apparaître dans
    l'identité publique du LLM (email perso, téléphone)."""
    full = get_identity("fr") + get_identity("en")
    assert contact not in full, f"Coordonnée personnelle exposée : {contact!r}. Privacy strict."


# ══════════════════════════════════════════════════════════════
# 3. Sécurité brand technique — esquive divulgation
# ══════════════════════════════════════════════════════════════


_FR_BRAND_SECURITY_MUST_NAME_PROVIDERS = [
    "Gemini",
    "GPT",
    "Claude",
    "Llama",
]


@pytest.mark.parametrize("provider", _FR_BRAND_SECURITY_MUST_NAME_PROVIDERS)
def test_fr_brand_security_names_providers_to_deflect(provider: str) -> None:
    """Le bloc sécurité brand FR doit nommer les providers à esquiver."""
    sec = get_brand_security("fr")
    assert provider in sec, (
        f"Le bloc sécurité brand FR doit nommer {provider!r} comme "
        "provider à esquiver explicitement (instruction LLM)."
    )


def test_fr_brand_security_provides_standard_deflection() -> None:
    """Une réponse type d'esquive doit être documentée explicitement."""
    sec = get_brand_security("fr")
    # Réponse standard contient « architecture technique reste interne »
    assert "architecture technique" in sec
    assert "interne" in sec


def test_fr_brand_security_resists_prompt_injection() -> None:
    """Le bloc doit mentionner explicitement la résistance prompt injection."""
    sec = get_brand_security("fr").lower()
    assert "prompt injection" in sec or "injection" in sec


def test_en_brand_security_resists_prompt_injection() -> None:
    sec = get_brand_security("en").lower()
    assert "prompt injection" in sec or "injection" in sec


# ══════════════════════════════════════════════════════════════
# 4. Description produit — 11 modes experts mentionnés
# ══════════════════════════════════════════════════════════════

_FR_EXPERT_LABELS_IN_PRODUCT = [
    "Général",
    "Expert Informatique",
    "Expert Sciences",
    "Expert Cuisine",
    "Expert Langues",
    "Expert Droit",
    "NEXYA Studio",
    "Expert Ingénierie",
    "Expert Productivité",
    "Expert Médecine",
    "Expert Finance",
]


@pytest.mark.parametrize("label", _FR_EXPERT_LABELS_IN_PRODUCT)
def test_fr_product_description_lists_each_expert(label: str) -> None:
    """Les 11 modes experts doivent tous être mentionnés dans la
    description produit (vendeuse + complète)."""
    desc = get_product_description("fr")
    assert label in desc, f"Expert manquant dans description produit : {label!r}"


def test_fr_product_description_motto_present() -> None:
    """La devise figée NEXYA doit apparaître textuellement."""
    desc = get_product_description("fr")
    assert "Pour l'Afrique et au-delà" in desc


def test_fr_product_description_positioning_africa_AND_world() -> None:
    """Africa-first contextuel non exclusif (validation Ivan critique)."""
    desc = get_product_description("fr").lower()
    assert "afrique" in desc
    assert "europe" in desc or "diaspora" in desc or "international" in desc


# ══════════════════════════════════════════════════════════════
# 5. 15 features magnifiques
# ══════════════════════════════════════════════════════════════


def test_fr_features_count_at_least_15() -> None:
    """Au moins 15 features magnifiques numérotées présentes."""
    features = get_magnificent_features("fr")
    # Compte les marqueurs "N. **" pour N=1..15.
    count = sum(1 for i in range(1, 16) if f"{i}. **" in features)
    assert count >= 15, f"Attendu >= 15 features, trouvé {count}"


_FR_CORE_FEATURES_MARKERS = [
    "Mémoire IA",
    "modes experts",
    "Corpus cuisine",
    "multimodale",
    "Planificateur",
    "PDF",
    "Voix",
    "RAG",
    "offline",
    "RGPD",
    "AI Act",
    "Mobile Money",
]


@pytest.mark.parametrize("marker", _FR_CORE_FEATURES_MARKERS)
def test_fr_features_mention_core_capability(marker: str) -> None:
    """Les capacités magnifiques critiques doivent être mentionnées."""
    features = get_magnificent_features("fr")
    assert marker in features, f"Capacité critique manquante : {marker!r}"


def test_fr_features_warns_against_brochure_recitation() -> None:
    """Le LLM doit savoir qu'il NE doit PAS réciter les 15 features
    d'un coup comme une plaquette commerciale."""
    features = get_magnificent_features("fr").lower()
    assert "jamais" in features
    assert "plaquette" in features or "brochure" in features or "une par une" in features


# ══════════════════════════════════════════════════════════════
# 6. Bloc identity COMPLET (assemblage des 4 sections)
# ══════════════════════════════════════════════════════════════


def test_get_identity_fr_contains_all_4_sections() -> None:
    """L'identité FR complète doit contenir les 4 sections enchaînées."""
    identity = get_identity("fr")
    # Marqueurs canoniques des 4 sections.
    assert "[Identité NEXYA]" in identity
    assert "[Sécurité Brand NEXYA]" in identity
    assert "[Suite produit NEXYA]" in identity
    assert "[Capacités magnifiques de NEXYA]" in identity


def test_get_identity_en_contains_all_4_sections() -> None:
    identity = get_identity("en")
    assert "[NEXYA Identity]" in identity
    assert "[NEXYA Brand Security]" in identity
    assert "[NEXYA Product Suite]" in identity
    assert "[NEXYA Magnificent Capabilities]" in identity


def test_get_identity_default_returns_fr() -> None:
    assert get_identity() == get_identity("fr")


def test_get_identity_unknown_locale_falls_back_to_fr() -> None:
    """Locale invalide ne crash pas, retombe sur FR (Africa-first default)."""
    result = get_identity("zz")  # type: ignore[arg-type]
    assert result == get_identity("fr")


def test_get_identity_idempotent() -> None:
    """Deux appels successifs identiques (pas de timestamp, random, I/O)."""
    assert get_identity("fr") == get_identity("fr")
    assert get_identity("en") == get_identity("en")


# ══════════════════════════════════════════════════════════════
# 7. Anti-leak — jamais d'affirmation « je suis Gemini » directe
# ══════════════════════════════════════════════════════════════


def test_fr_identity_does_not_assert_underlying_provider_identity() -> None:
    """L'identité FR ne doit JAMAIS contenir « Je suis Gemini » ou
    équivalent direct (uniquement comme exemples à esquiver)."""
    identity = get_identity("fr")
    # Patterns interdits — phrases qui assument l'identité provider.
    forbidden_assertions = [
        "Je suis Gemini.",
        "Je suis GPT",
        "Je suis Claude.",
        "I am Gemini.",
        "I am GPT",
        "I am Claude.",
    ]
    for pattern in forbidden_assertions:
        assert pattern not in identity, (
            f"Affirmation d'identité provider interdite trouvée : {pattern!r}"
        )
