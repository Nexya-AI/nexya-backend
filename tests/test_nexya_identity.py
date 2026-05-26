"""
Tests Session A1 + enrichissement 2026-05-26 — `app/ai/nexya_identity.py`.

Garantit que :
- Les 4 paliers progressifs du fondateur sont présents (palier 1 base
  avec nom Loth Ivan visible + invitation à l'action chaleureuse,
  palier 2 profil hybride teaser, palier 3 bio 3-axes IA/Full-Stack/
  Design avec Python+R+Power BI+Flutter+Adobe, palier 4 mission sans
  mention « francophone »).
- **Règle 5** — cohérence dans la même conversation (pas de re-déballage
  de la bio à chaque mention du fondateur).
- **Règle 6** — réponse anti-superlatifs sur le fondateur (« génie »,
  « visionnaire », etc.) via procédure 3-temps (biais épistémique →
  faits objectifs → renvoi au jugement utilisateur).
- Anti-hallucination biographique stricte (aucune info inventée comme
  Stanford/MIT/Harvard, pas d'âge, pas de diplôme spécifique).
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
# Validation Ivan 2026-05-19 + enrichissement 2026-05-26 : « tu rédiges
# une bio enrichie de ma personne uniquement avec des infos vraies,
# donc absolument rien sur moi qui ne soit pas une réalité ». Faits
# autorisés UNIQUEMENT :
#   - nom complet : Loth Ivan Ngassa Yimga
#   - profil hybride 3-axes :
#       * IA & Big Data (Python, R, Power BI, architectures IA)
#       * Full-Stack & Mobile (UML, ingénierie logicielle, Flutter)
#       * Design Graphique & UI/UX (Photoshop, Illustrator, Adobe XD)
#   - pays : Cameroun / camerounais
#   - rôle : fondateur Nexyalabs
#   - création : NEXYA AI
#
# Note 2026-05-26 — les superlatifs « génie », « visionnaire »,
# « le plus grand », etc. APPARAISSENT désormais dans la règle 6
# comme exemples de mots-clés à désamorcer (procédure 3-temps).
# Ils sont donc PRÉSENTS dans le bloc mais en tant que liste
# d'hyperboles à refuser, jamais comme affirmation positive. Les
# tests anti-hallucination ne les interdisent plus.


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
    """Flutter doit apparaître dans le palier 3 (axe Full-Stack & Mobile)."""
    story = get_founder_story("fr")
    assert "Flutter" in story


# ─── HALLUCINATIONS INTERDITES (test négatif strict) ─────────


_FR_FORBIDDEN_HALLUCINATIONS = [
    "Stanford",  # université inventée
    "MIT",  # université inventée
    "Harvard",
    "diplôme",  # parcours inventé
    "milliardaire",
    "fortune personnelle",  # spec : pas d'invention richesse
    "milliards",
]


@pytest.mark.parametrize("hallucination", _FR_FORBIDDEN_HALLUCINATIONS)
def test_fr_founder_no_hallucinated_facts(hallucination: str) -> None:
    """Anti-hallucination biographique : ces termes sont interdits dans
    la bio fondateur car non documentés dans les mémoires Ivan.

    Note : « génie », « visionnaire », « le plus grand » sont autorisés
    car ils apparaissent en règle 6 comme exemples de mots-clés à
    désamorcer (procédure 3-temps). Voir docstring du module."""
    story = get_founder_story("fr")
    assert hallucination.lower() not in story.lower(), (
        f"Terme hallucination potentielle détecté : {hallucination!r}"
    )


# ══════════════════════════════════════════════════════════════
# 2.bis Palier 1 — nom fondateur visible + invitation chaleureuse
# ══════════════════════════════════════════════════════════════
#
# Décision Ivan 2026-05-26 : le nom Loth Ivan Ngassa Yimga apparaît
# dès le palier 1 au lancement (signal de différenciation Africa-first).
# Sera retiré du palier 1 après traction (cf. mémoire
# project_nexya_brand_palier1_founder_visibility.md). Le palier 1
# autorise EXPLICITEMENT « Comment puis-je t'être utile aujourd'hui ? »
# (nuance tone #2 : invitation à l'action ≠ formule creuse).


def test_fr_palier_1_contains_founder_name() -> None:
    """Palier 1 doit mentionner Loth Ivan Ngassa Yimga (décision marketing 2026-05-26)."""
    story = get_founder_story("fr")
    # Le palier 1 contient l'instruction de réponse avec le nom du fondateur.
    palier_1_start = story.index("**Palier 1")
    palier_2_start = story.index("**Palier 2")
    palier_1_block = story[palier_1_start:palier_2_start]
    assert "Loth Ivan Ngassa Yimga" in palier_1_block


def test_en_tier_1_contains_founder_name() -> None:
    """Tier 1 EN doit mentionner Loth Ivan Ngassa Yimga (parité stricte)."""
    story = get_founder_story("en")
    tier_1_start = story.index("**Tier 1")
    tier_2_start = story.index("**Tier 2")
    tier_1_block = story[tier_1_start:tier_2_start]
    assert "Loth Ivan Ngassa Yimga" in tier_1_block


def test_fr_palier_1_authorizes_action_invitation() -> None:
    """Palier 1 autorise « Comment puis-je t'être utile » ou équivalent."""
    story = get_founder_story("fr")
    palier_1_start = story.index("**Palier 1")
    palier_2_start = story.index("**Palier 2")
    palier_1_block = story[palier_1_start:palier_2_start]
    # Le palier 1 doit décrire une invitation chaleureuse à l'action
    has_invitation = any(
        marker in palier_1_block
        for marker in [
            "Comment puis-je t'être utile",
            "En quoi puis-je t'aider",
            "invitation",
        ]
    )
    assert has_invitation, "Palier 1 doit autoriser une invitation à l'action chaleureuse"


# ══════════════════════════════════════════════════════════════
# 2.ter Palier 3 — bio 3-axes IA / Full-Stack / Design
# ══════════════════════════════════════════════════════════════
#
# Mise à jour 2026-05-26 : palier 3 enrichi avec profil hybride
# 3-axes au lieu de la mention simple « dev Flutter ». Les 3 axes
# doivent tous être présents avec leurs technologies clés.


_FR_PALIER_3_AXIS_MARKERS = [
    "Intelligence Artificielle & Big Data",
    "Développement Full-Stack & Mobile",
    "Design Graphique & UI/UX",
]


@pytest.mark.parametrize("axis", _FR_PALIER_3_AXIS_MARKERS)
def test_fr_palier_3_includes_3_axes(axis: str) -> None:
    """Les 3 axes du profil hybride doivent être présents au palier 3."""
    story = get_founder_story("fr")
    assert axis in story, f"Axe profil hybride manquant : {axis!r}"


_FR_PALIER_3_TECH_KEYWORDS = [
    "Python",
    "Power BI",
    "Flutter",
    "Photoshop",
    "Illustrator",
    "Adobe XD",
]


@pytest.mark.parametrize("tech", _FR_PALIER_3_TECH_KEYWORDS)
def test_fr_palier_3_includes_tech_stack(tech: str) -> None:
    """Les technologies clés du profil hybride doivent être présentes."""
    story = get_founder_story("fr")
    assert tech in story, f"Technologie clé manquante au palier 3 : {tech!r}"


def test_fr_palier_3_includes_r_language() -> None:
    """R (langage de data science) doit être présent — utilise pattern
    avec mot-frontière pour éviter le faux match sur 'R' isolé partout."""
    story = get_founder_story("fr")
    # « Python et R » est la formulation canonique
    assert "Python et R" in story or "R pour" in story, (
        "Le langage R doit être mentionné dans l'axe IA & Big Data"
    )


_EN_TIER_3_AXIS_MARKERS = [
    "Artificial Intelligence & Big Data",
    "Full-Stack & Mobile Development",
    "Graphic Design & UI/UX",
]


@pytest.mark.parametrize("axis", _EN_TIER_3_AXIS_MARKERS)
def test_en_tier_3_includes_3_axes(axis: str) -> None:
    """Parité stricte : les 3 axes du profil hybride doivent être en EN."""
    story = get_founder_story("en")
    assert axis in story, f"Axis missing in EN tier 3: {axis!r}"


@pytest.mark.parametrize("tech", _FR_PALIER_3_TECH_KEYWORDS)
def test_en_tier_3_includes_tech_stack(tech: str) -> None:
    """Parité stricte : technologies clés en EN aussi."""
    story = get_founder_story("en")
    assert tech in story, f"Tech keyword missing in EN tier 3: {tech!r}"


def test_fr_palier_4_no_francophone_keyword() -> None:
    """Palier 4 mise à jour 2026-05-26 : pas de « francophone »
    (Africa-first sans restriction linguistique)."""
    story = get_founder_story("fr")
    palier_4_start = story.index("**Palier 4")
    # Trouve la fin du palier 4 (avant la section "Règles absolues")
    rules_start = story.index("**Règles absolues")
    palier_4_block = story[palier_4_start:rules_start]
    assert "francophone" not in palier_4_block.lower(), (
        "Palier 4 ne doit plus contenir « francophone » (décision 2026-05-26)"
    )


def test_en_tier_4_no_francophone_keyword() -> None:
    """Parité stricte : tier 4 EN sans 'francophone'."""
    story = get_founder_story("en")
    tier_4_start = story.index("**Tier 4")
    rules_start = story.index("**Absolute rules")
    tier_4_block = story[tier_4_start:rules_start]
    assert "francophone" not in tier_4_block.lower()


# ══════════════════════════════════════════════════════════════
# 2.quater Règle 5 — cohérence dans la même conversation
# ══════════════════════════════════════════════════════════════


def test_fr_rule_5_session_consistency_present() -> None:
    """Règle 5 doit être présente : pas de re-déballage de la bio
    à chaque mention du fondateur dans la même session."""
    story = get_founder_story("fr")
    # Marqueur explicite
    assert "Cohérence dans la même conversation" in story
    # Logique sémantique attendue
    assert "re-déballe" in story or "JAMAIS deux fois" in story


def test_en_rule_5_session_consistency_present() -> None:
    """Parité EN : règle 5 doit être présente."""
    story = get_founder_story("en")
    assert "Consistency within the same conversation" in story
    assert "do NOT unpack" in story.lower() or "never recite" in story.lower()


# ══════════════════════════════════════════════════════════════
# 2.quinquies Règle 6 — réponse aux superlatifs sur le fondateur
# ══════════════════════════════════════════════════════════════


_FR_RULE_6_SUPERLATIVE_KEYWORDS = [
    "génie",
    "visionnaire",
    "le plus grand",
    "légende",
    "prodige",
]


@pytest.mark.parametrize("keyword", _FR_RULE_6_SUPERLATIVE_KEYWORDS)
def test_fr_rule_6_lists_superlative_triggers(keyword: str) -> None:
    """Règle 6 doit lister les mots-clés superlatifs qui déclenchent
    la procédure de désamorçage 3-temps."""
    story = get_founder_story("fr")
    assert keyword.lower() in story.lower(), (
        f"Mot-clé superlatif manquant dans règle 6 : {keyword!r}"
    )


def test_fr_rule_6_describes_3_step_procedure() -> None:
    """La règle 6 doit documenter la procédure 3-temps."""
    story = get_founder_story("fr")
    # Marqueurs de la procédure 3-temps
    assert "biais épistémique" in story
    assert "faits objectifs" in story
    assert "renvoie le jugement" in story.lower() or "Renvoie le jugement" in story


def test_fr_rule_6_includes_example_reply() -> None:
    """La règle 6 doit fournir un exemple de réponse complet."""
    story = get_founder_story("fr")
    # L'exemple type inclut ces phrases canoniques
    assert "Honnêtement" in story
    assert "juge et partie" in story
    assert "postérité" in story


_EN_RULE_6_SUPERLATIVE_KEYWORDS = [
    "genius",
    "visionary",
    "the greatest",
    "legend",
    "prodigy",
]


@pytest.mark.parametrize("keyword", _EN_RULE_6_SUPERLATIVE_KEYWORDS)
def test_en_rule_6_lists_superlative_triggers(keyword: str) -> None:
    """Parité EN : règle 6 doit lister les triggers superlatifs."""
    story = get_founder_story("en")
    assert keyword.lower() in story.lower(), (
        f"Superlative trigger missing in EN rule 6: {keyword!r}"
    )


def test_en_rule_6_describes_3_step_procedure() -> None:
    story = get_founder_story("en")
    assert "epistemic bias" in story.lower()
    assert "objective facts" in story.lower()
    assert "return the judgment" in story.lower()


def test_en_rule_6_includes_example_reply() -> None:
    story = get_founder_story("en")
    assert "Honestly" in story
    assert "judge and party" in story
    assert "posterity" in story.lower()


def test_fr_rule_6_forbids_validating_and_denying() -> None:
    """La règle 6 doit interdire à la fois valider ET nier frontalement."""
    story = get_founder_story("fr")
    # Marqueurs des deux interdictions
    assert "JAMAIS le superlatif" in story or "ne valides JAMAIS" in story.lower()
    assert "JAMAIS frontalement" in story or "ne nies JAMAIS" in story.lower()


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
