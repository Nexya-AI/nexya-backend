"""Tests pour la matrice de résolution des Model Pills (GEEK/LOTH/JUSTO).

Couverture :
- Les 33 cellules (11 experts × 3 pills) résolvent vers le couple
  (model_name, disable_thinking) attendu selon la spec validée Ivan
  (2026-05-23) avec préservation des invariants safety-critical
  (medicine = thinking always on, legal = thinking on sur GEEK+LOTH)
  et G2 V8 (cooking = disable_thinking partout).
- Edge cases : pill None, pill inconnue, expert inconnu, studio
  (image-only, mapping vide), case-insensitive.
- Schéma Pydantic `ChatStreamRequest.model_pill` accepte les 3 valeurs
  Literal et rejette les autres.

Aucune dépendance pytest spécifique — tests purement statiques sur
le registre + le helper `resolve_model_for_pill`.
"""

from __future__ import annotations

import pytest

from app.ai.experts import (
    EXPERT_REGISTRY,
    ModelPillConfig,
    resolve_model_for_pill,
)
from app.features.chat.schemas import ChatStreamRequest


# ─────────────────────────────────────────────────────────────────────
# Matrice canonique 33 cellules (spec Ivan 2026-05-23)
# ─────────────────────────────────────────────────────────────────────
# Format : {(expert_id, pill): (model_name, disable_thinking)}
#
# Patterns :
# - DEFAULT (7 experts conversationnels) : GEEK=pro+thinking,
#   LOTH=pro sans thinking, JUSTO=flash sans thinking
# - COOKING (G2 V8 preserve) : disable_thinking=True partout
# - MEDICINE (safety-critical MAX) : Pro+thinking sur les 3 pills
# - LEGAL (safety-critical) : Pro+thinking sur GEEK+LOTH, JUSTO sans
# - STUDIO (image-only) : mapping vide → (None, None)
EXPECTED_MATRIX = {
    # ─── Default (7 experts) ──────────────────────────────────────
    ("general", "geek"): ("gemini-2.5-pro", False),
    ("general", "loth"): ("gemini-2.5-pro", True),
    ("general", "justo"): ("gemini-2.5-flash", True),
    ("computer", "geek"): ("gemini-2.5-pro", False),
    ("computer", "loth"): ("gemini-2.5-pro", True),
    ("computer", "justo"): ("gemini-2.5-flash", True),
    ("science", "geek"): ("gemini-2.5-pro", False),
    ("science", "loth"): ("gemini-2.5-pro", True),
    ("science", "justo"): ("gemini-2.5-flash", True),
    ("finance", "geek"): ("gemini-2.5-pro", False),
    ("finance", "loth"): ("gemini-2.5-pro", True),
    ("finance", "justo"): ("gemini-2.5-flash", True),
    ("language", "geek"): ("gemini-2.5-pro", False),
    ("language", "loth"): ("gemini-2.5-pro", True),
    ("language", "justo"): ("gemini-2.5-flash", True),
    ("engineering", "geek"): ("gemini-2.5-pro", False),
    ("engineering", "loth"): ("gemini-2.5-pro", True),
    ("engineering", "justo"): ("gemini-2.5-flash", True),
    ("productivity", "geek"): ("gemini-2.5-pro", False),
    ("productivity", "loth"): ("gemini-2.5-pro", True),
    ("productivity", "justo"): ("gemini-2.5-flash", True),
    # ─── Cooking (G2 V8 preserve disable_thinking partout) ────────
    ("cooking", "geek"): ("gemini-2.5-pro", True),
    ("cooking", "loth"): ("gemini-2.5-flash", True),
    ("cooking", "justo"): ("gemini-2.5-flash", True),
    # ─── Medicine (safety-critical MAX, thinking partout) ─────────
    ("medicine", "geek"): ("gemini-2.5-pro", False),
    ("medicine", "loth"): ("gemini-2.5-pro", False),
    ("medicine", "justo"): ("gemini-2.5-pro", False),
    # ─── Legal (safety-critical, thinking GEEK+LOTH, JUSTO off) ──
    ("legal", "geek"): ("gemini-2.5-pro", False),
    ("legal", "loth"): ("gemini-2.5-pro", False),
    ("legal", "justo"): ("gemini-2.5-pro", True),
    # ─── Studio (image-only, mapping vide) ────────────────────────
    ("studio", "geek"): (None, None),
    ("studio", "loth"): (None, None),
    ("studio", "justo"): (None, None),
}


@pytest.mark.parametrize(
    "expert_id,pill,expected",
    [(k[0], k[1], v) for k, v in EXPECTED_MATRIX.items()],
    ids=[f"{k[0]}-{k[1]}" for k in EXPECTED_MATRIX.keys()],
)
def test_matrix_all_33_cells(
    expert_id: str,
    pill: str,
    expected: tuple[str | None, bool | None],
) -> None:
    """Chaque cellule de la matrice produit le couple (modèle, thinking) attendu."""
    result = resolve_model_for_pill(expert_id, pill)
    assert result == expected, (
        f"{expert_id}/{pill}: attendu {expected}, obtenu {result}"
    )


def test_matrix_total_cells_count() -> None:
    """11 experts × 3 pills = 33 cellules dans la spec — anti-régression."""
    assert len(EXPECTED_MATRIX) == 33
    expected_experts = set(EXPERT_REGISTRY.keys())
    matrix_experts = {k[0] for k in EXPECTED_MATRIX.keys()}
    assert matrix_experts == expected_experts


def test_safety_critical_medicine_thinking_always_on() -> None:
    """Garde-fou : aucune pill ne doit jamais désactiver le thinking sur medicine.

    Un patient en JUSTO express mérite la même rigueur clinique qu'en GEEK
    approfondi. Si quelqu'un override `_MEDICINE_PILL_MAPPING` un jour pour
    « gagner de la latence », ce test casse immédiatement.
    """
    for pill in ("geek", "loth", "justo"):
        model, disable_thinking = resolve_model_for_pill("medicine", pill)
        assert model == "gemini-2.5-pro", (
            f"medicine/{pill}: modèle doit être pro (Flash refusé safety)"
        )
        assert disable_thinking is False, (
            f"medicine/{pill}: thinking DOIT rester activé (safety-critical)"
        )


def test_safety_critical_legal_thinking_on_for_geek_and_loth() -> None:
    """Legal : GEEK+LOTH gardent thinking on (rigueur OHADA), JUSTO off."""
    for pill in ("geek", "loth"):
        model, disable_thinking = resolve_model_for_pill("legal", pill)
        assert model == "gemini-2.5-pro"
        assert disable_thinking is False, f"legal/{pill}: thinking ON"
    # JUSTO : thinking off pour rapidité sur cas factuels simples
    model, disable_thinking = resolve_model_for_pill("legal", "justo")
    assert model == "gemini-2.5-pro", "legal/justo: modèle reste Pro"
    assert disable_thinking is True, "legal/justo: thinking off (rapidité)"


def test_g2_v8_cooking_disable_thinking_on_all_pills() -> None:
    """G2 V8 preserve : cooking garde disable_thinking=True sur GEEK/LOTH/JUSTO.

    Le benchmark 2026-05-18 a prouvé que pour le format recette (RAG +
    structure ingrédients/étapes), Flash sans thinking est objectivement
    meilleur (2.2× plus rapide, 5× plus riche en sortie). Cet invariant
    doit survivre à toute évolution future de la matrice.
    """
    for pill in ("geek", "loth", "justo"):
        _, disable_thinking = resolve_model_for_pill("cooking", pill)
        assert disable_thinking is True, (
            f"cooking/{pill}: disable_thinking DOIT rester True (G2 V8 preserve)"
        )


def test_studio_image_only_returns_no_pill_resolution() -> None:
    """Studio est image-only — les pills n'ont pas de sens, mapping vide."""
    for pill in ("geek", "loth", "justo"):
        assert resolve_model_for_pill("studio", pill) == (None, None)


# ─────────────────────────────────────────────────────────────────────
# Edge cases — fail-safe
# ─────────────────────────────────────────────────────────────────────


def test_pill_none_returns_no_resolution() -> None:
    """`pill=None` → (None, None) → caller utilise config legacy."""
    assert resolve_model_for_pill("general", None) == (None, None)
    assert resolve_model_for_pill("cooking", None) == (None, None)


def test_pill_empty_string_returns_no_resolution() -> None:
    """`pill=""` traité comme None (champ optionnel non rempli)."""
    assert resolve_model_for_pill("general", "") == (None, None)


@pytest.mark.parametrize("pill", ["super-power", "ultra", "fast", "smart"])
def test_pill_unknown_returns_no_resolution(pill: str) -> None:
    """Pill inconnue → fail-safe (None, None), pas d'exception."""
    assert resolve_model_for_pill("general", pill) == (None, None)


def test_pill_case_insensitive() -> None:
    """Pill case-insensitive ('GEEK', 'Geek', 'geek' équivalents)."""
    expected = resolve_model_for_pill("general", "geek")
    assert resolve_model_for_pill("general", "GEEK") == expected
    assert resolve_model_for_pill("general", "Geek") == expected
    assert resolve_model_for_pill("general", "  geek  ") == expected


def test_unknown_expert_falls_back_to_general() -> None:
    """Expert inconnu → comportement aligné `get_expert_config` (fallback general).

    Si quelqu'un envoie `expert_id="unknown_v2"` (nouveau mode Flutter pas
    encore déployé backend), on ne casse pas le chat — on résout sur
    general (qui a le default mapping).
    """
    assert resolve_model_for_pill("unknown_expert", "geek") == ("gemini-2.5-pro", False)
    assert resolve_model_for_pill("brand-new-mode", "justo") == (
        "gemini-2.5-flash",
        True,
    )


def test_expert_id_none_falls_back_to_general() -> None:
    """`expert_id=None` → fallback general."""
    assert resolve_model_for_pill(None, "geek") == ("gemini-2.5-pro", False)


# ─────────────────────────────────────────────────────────────────────
# Schéma Pydantic — ChatStreamRequest.model_pill
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("pill", ["geek", "loth", "justo"])
def test_chat_stream_request_accepts_valid_pill(pill: str) -> None:
    """ChatStreamRequest accepte les 3 pills valides."""
    req = ChatStreamRequest(message="hello", model_pill=pill)
    assert req.model_pill == pill


def test_chat_stream_request_pill_optional() -> None:
    """`model_pill` est optionnel (default None)."""
    req = ChatStreamRequest(message="hello")
    assert req.model_pill is None


@pytest.mark.parametrize("invalid", ["super-power", "GEEK", "GROK", "ultra"])
def test_chat_stream_request_rejects_invalid_pill(invalid: str) -> None:
    """Pydantic rejette les pills hors Literal['geek','loth','justo']."""
    with pytest.raises(Exception):  # pydantic.ValidationError
        ChatStreamRequest(message="hello", model_pill=invalid)


# ─────────────────────────────────────────────────────────────────────
# ModelPillConfig invariants
# ─────────────────────────────────────────────────────────────────────


def test_model_pill_config_is_frozen() -> None:
    """`ModelPillConfig` est immutable (anti-mutation runtime)."""
    cfg = ModelPillConfig(model_tier="pro", disable_thinking=False)
    with pytest.raises(Exception):  # FrozenInstanceError
        cfg.model_tier = "flash"  # type: ignore[misc]


def test_all_experts_have_pill_mapping_field() -> None:
    """Chaque ExpertConfig porte un `model_pill_mapping` (dict, jamais None).

    Le champ peut être vide (studio) mais doit exister (le helper
    `resolve_model_for_pill` fait `config.model_pill_mapping.get(...)`
    qui crasherait sur None).
    """
    for expert_id, config in EXPERT_REGISTRY.items():
        assert isinstance(config.model_pill_mapping, dict), (
            f"{expert_id}: model_pill_mapping doit être dict (jamais None)"
        )


def test_non_studio_experts_have_3_pills() -> None:
    """Les 10 experts non-studio ont les 3 pills (geek/loth/justo) configurées."""
    for expert_id, config in EXPERT_REGISTRY.items():
        if expert_id == "studio":
            continue
        assert set(config.model_pill_mapping.keys()) == {"geek", "loth", "justo"}, (
            f"{expert_id}: doit avoir les 3 pills geek/loth/justo"
        )
