"""
Tests N2 — `app.ai.experts` : intégrité du registre 11 experts.

Ces tests garantissent que :
1. Les **11 expert_id slugs** (contrat API stable avec Flutter) ne dérivent pas.
2. Chaque `ExpertConfig` a les champs essentiels non-vides (display_name,
   system_prompt, primary_provider, primary_model).
3. `get_expert_config()` est permissif : `None` ou expert_id inconnu
   retombe sur "general" sans lever.
4. Les invariants safety-critical sont préservés : `medicine` et `legal`
   ont un disclaimer non-vide + une température basse ; `studio` a une
   chaîne de fallback vide (image-only). NB : depuis planner-from-chat
   LOT 4, ces deux experts ont `tools_allowed=True` (rappels Planner
   autorisés depuis le chat).
5. La `full_chain` commence par le primaire et liste ensuite les fallbacks.

Aucun appel LLM. Pure introspection du registre.
"""

from __future__ import annotations

import pytest

from app.ai.experts import (
    EXPERT_REGISTRY,
    ExpertConfig,
    all_expert_ids,
    get_expert_config,
)

# ══════════════════════════════════════════════════════════════
# Slugs attendus — figés comme contrat API avec Flutter (ExpertDomain.name)
# ══════════════════════════════════════════════════════════════

EXPECTED_EXPERT_IDS: frozenset[str] = frozenset(
    {
        "general",
        "computer",
        "science",
        "finance",
        "language",
        "cooking",
        "studio",
        "engineering",
        "productivity",
        "medicine",
        "legal",
    }
)

SAFETY_CRITICAL_IDS: frozenset[str] = frozenset({"medicine", "legal"})


# ══════════════════════════════════════════════════════════════
# 1. Intégrité du set d'expert_id — contrat API
# ══════════════════════════════════════════════════════════════


def test_registry_contains_exactly_eleven_experts() -> None:
    assert len(EXPERT_REGISTRY) == 11


def test_registry_keys_match_expected_slugs_strict() -> None:
    """Slug != display_name. Le slug est le contrat API ; un renommage
    casse le frontend Flutter (`ExpertDomain.name`)."""
    assert set(EXPERT_REGISTRY.keys()) == EXPECTED_EXPERT_IDS


def test_all_expert_ids_returns_eleven_items_general_first() -> None:
    ids = all_expert_ids()
    assert len(ids) == 11
    assert ids[0] == "general"  # general en tête (cf. docstring)
    assert set(ids) == EXPECTED_EXPERT_IDS


def test_each_expert_id_matches_its_config_field() -> None:
    """Le mapping registre `key → ExpertConfig` doit être cohérent —
    `EXPERT_REGISTRY['science'].expert_id` doit valoir `'science'`."""
    for key, config in EXPERT_REGISTRY.items():
        assert config.expert_id == key, f"Mismatch: {key!r} → {config.expert_id!r}"


# ══════════════════════════════════════════════════════════════
# 2. Champs essentiels non-vides
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("expert_id", sorted(EXPECTED_EXPERT_IDS))
def test_each_config_has_non_empty_essential_fields(expert_id: str) -> None:
    config = EXPERT_REGISTRY[expert_id]
    assert config.display_name and len(config.display_name) >= 2
    assert config.system_prompt and len(config.system_prompt) > 50
    assert config.primary_provider, f"{expert_id} sans primary_provider"
    assert config.primary_model, f"{expert_id} sans primary_model"
    assert config.tier in {"flash", "pro", "image"}


# ══════════════════════════════════════════════════════════════
# 3. get_expert_config() — permissive
# ══════════════════════════════════════════════════════════════


def test_get_expert_config_none_returns_general() -> None:
    assert get_expert_config(None) is EXPERT_REGISTRY["general"]


def test_get_expert_config_empty_string_returns_general() -> None:
    """`""` est falsy : doit aussi retomber sur general (`if not expert_id`)."""
    assert get_expert_config("") is EXPERT_REGISTRY["general"]


def test_get_expert_config_unknown_id_falls_back_to_general() -> None:
    """Le contrat est explicitement permissif — un expert_id inconnu (Flutter
    en avance sur le backend) ne lève pas, retombe sur general."""
    cfg = get_expert_config("nonexistent_expert_id_xyz")
    assert cfg.expert_id == "general"


def test_get_expert_config_known_id_returns_exact_config() -> None:
    cfg = get_expert_config("computer")
    assert cfg.expert_id == "computer"
    assert cfg is EXPERT_REGISTRY["computer"]


# ══════════════════════════════════════════════════════════════
# 4. Invariants safety-critical
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize("expert_id", sorted(SAFETY_CRITICAL_IDS))
def test_safety_critical_experts_have_tools_enabled(expert_id: str) -> None:
    """[planner-from-chat LOT 4, 2026-05-22] `medicine` et `legal` ont
    désormais le function calling ACTIVÉ (décision produit Ivan). F2.5 les
    avait exclus par prudence ; mais les 4 tools Planner sont bénins (ils
    posent des rappels, ne prescrivent ni ne rédigent aucun acte), et
    planifier « prendre mes médicaments » depuis le mode Médecine est un
    cas d'usage légitime — voir `ExpertConfig.tools_allowed` dans
    `experts.py`."""
    config = EXPERT_REGISTRY[expert_id]
    assert config.tools_allowed is True, (
        f"{expert_id} doit avoir tools_allowed=True (LOT 4 planner-from-chat)"
    )


@pytest.mark.parametrize("expert_id", sorted(SAFETY_CRITICAL_IDS))
def test_safety_critical_experts_have_disclaimer(expert_id: str) -> None:
    config = EXPERT_REGISTRY[expert_id]
    assert config.disclaimer is not None
    assert len(config.disclaimer) > 30


def test_studio_has_empty_fallback_chain_image_only() -> None:
    """Studio est image-only : pas de chaîne chat, le router lève si on
    tente `resolve()` dessus mais doit pouvoir `resolve_image()`."""
    studio = EXPERT_REGISTRY["studio"]
    assert studio.fallback_chain == ()
    assert studio.tier == "image"
    assert studio.primary_provider == "gemini-imagen"


def test_safety_critical_experts_have_low_temperature() -> None:
    """Médecine et légal doivent avoir une température très basse (zéro
    créativité — l'erreur coûte cher)."""
    assert EXPERT_REGISTRY["medicine"].temperature <= 0.2
    assert EXPERT_REGISTRY["legal"].temperature <= 0.2


# ══════════════════════════════════════════════════════════════
# 5. ExpertConfig.full_chain — primaire + fallbacks
# ══════════════════════════════════════════════════════════════


def test_full_chain_starts_with_primary() -> None:
    cfg = EXPERT_REGISTRY["general"]
    chain = cfg.full_chain
    assert chain[0] == (cfg.primary_provider, cfg.primary_model)


def test_full_chain_includes_all_fallbacks() -> None:
    cfg = EXPERT_REGISTRY["general"]
    chain = cfg.full_chain
    # general a primary + 2 fallbacks (Pro Gemini + OpenRouter Sonnet)
    assert len(chain) == 1 + len(cfg.fallback_chain)
    for fallback in cfg.fallback_chain:
        assert fallback in chain


def test_full_chain_for_studio_is_just_primary() -> None:
    cfg = EXPERT_REGISTRY["studio"]
    assert cfg.full_chain == (("gemini-imagen", "imagen-3.0-generate-002"),)


# ══════════════════════════════════════════════════════════════
# 6. Cohérence corpus_enabled (post-G1 cleanup)
# ══════════════════════════════════════════════════════════════


def test_only_cooking_has_corpus_enabled_post_g2() -> None:
    """Après G2 (2026-05-16), seul l'expert `cooking` a `corpus_enabled=True`
    (recettes camerounaises propriétaires Loth Ivan / Nexyalabs).
    G1 `language` reste désactivé après échec blind test du 2026-04-24.
    G4 ingénierie / G6 informatique / G7 sciences resteront désactivés
    jusqu'à leurs sessions d'activation dédiées."""
    expected_enabled = {"cooking"}
    actual_enabled = {expert_id for expert_id, cfg in EXPERT_REGISTRY.items() if cfg.corpus_enabled}
    assert actual_enabled == expected_enabled, (
        f"Mismatch corpus_enabled : attendu {expected_enabled}, obtenu {actual_enabled}"
    )


# ══════════════════════════════════════════════════════════════
# 7. ExpertConfig est immutable (frozen=True, slots=True)
# ══════════════════════════════════════════════════════════════


def test_expert_config_is_frozen_dataclass() -> None:
    """Empêche les mutations runtime accidentelles du registre."""
    cfg = EXPERT_REGISTRY["general"]
    with pytest.raises((AttributeError, Exception)):
        cfg.expert_id = "hacked"  # type: ignore[misc]


def test_expert_config_dataclass_attrs_are_frozen() -> None:
    """Vérifie l'introspection dataclass — frozen=True imposé."""
    import dataclasses

    assert dataclasses.is_dataclass(ExpertConfig)
    params = ExpertConfig.__dataclass_params__  # type: ignore[attr-defined]
    assert params.frozen is True


# ══════════════════════════════════════════════════════════════
# 8. Cap max_tokens — anti-runaway facture (audit 2026-05-01 finding S1)
# ══════════════════════════════════════════════════════════════


def test_all_experts_have_explicit_max_tokens_cap() -> None:
    """Chaque ExpertConfig doit poser max_tokens explicitement.

    Sans cap, Gemini Pro peut générer 8000+ tokens × $5/1M = $0.04 par
    réponse runaway. À 950k users × 50 chats/jour × 1% runaway, la
    facture worst-case dépasse $19k/jour. Cap obligatoire par expert.
    """
    for expert_id, cfg in EXPERT_REGISTRY.items():
        assert cfg.max_tokens is not None, (
            f"Expert '{expert_id}' n'a pas de max_tokens — risque output "
            f"runaway facture (Gemini Pro ~$0.04 par réponse non-bornée)"
        )
        assert cfg.max_tokens > 0, f"{expert_id}: max_tokens doit être positif"
        assert cfg.max_tokens <= 8192, (
            f"{expert_id}: max_tokens={cfg.max_tokens} suspect "
            f"(au-delà de 8192 = créativité débridée non justifiée)"
        )


def test_max_tokens_aligned_with_tier() -> None:
    """Le cap max_tokens doit être cohérent : tier=flash plus serré que tier=pro."""
    flash_caps = [c.max_tokens for c in EXPERT_REGISTRY.values() if c.tier == "flash"]
    pro_caps = [
        c.max_tokens
        for c in EXPERT_REGISTRY.values()
        if c.tier == "pro" and "safety-critical" not in c.tags
    ]

    assert flash_caps, "Au moins un expert tier=flash attendu"
    assert pro_caps, "Au moins un expert tier=pro non-safety attendu"

    # Tous les flash <= max des pro non-safety
    # (depuis 2026-05-22 : flash=4096, pro jusqu'à 8192 — caps relevés pour
    # supprimer la troncature des longues réponses, cf. CLAUDE.md §15)
    assert max(flash_caps) <= max(pro_caps), (
        f"Un expert tier=flash a max_tokens > tier=pro — incohérent. "
        f"flash_max={max(flash_caps)}, pro_max={max(pro_caps)}"
    )


def test_safety_critical_experts_have_capped_max_tokens() -> None:
    """`medicine` et `legal` doivent avoir un cap raisonnable (info structurée
    + disclaimers, pas de génération créative au-delà de 4096)."""
    for expert_id in SAFETY_CRITICAL_IDS:
        cfg = EXPERT_REGISTRY[expert_id]
        assert cfg.max_tokens is not None
        assert cfg.max_tokens <= 4096, (
            f"{expert_id} (safety-critical) max_tokens={cfg.max_tokens} > 4096 — "
            f"info médicale/juridique doit rester structurée et concise"
        )
