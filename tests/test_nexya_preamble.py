"""
Tests Session A1 — `app/ai/nexya_preamble.py`.

Garantit que :
- `build_nexya_preamble(expert_id)` retourne un bloc non-vide quand le
  kill-switch est ON et que tout fonctionne.
- Le kill-switch `settings.nexya_preamble_enabled=False` retourne None.
- Le cap `settings.nexya_preamble_max_chars` est respecté strictement.
- Troncature ajoute un marqueur lisible LLM en fin de bloc.
- Fail-safe absolue : exception interne (ex: tone/identity/routing qui
  raise) → log warning + None retourné.
- Idempotence : 2 appels identiques retournent exactement la même
  string (pas de random, timestamp, I/O).
- Locale FR / EN supportée.
"""

from __future__ import annotations

import pytest

from app.ai import nexya_preamble as preamble_module
from app.ai.nexya_preamble import build_nexya_preamble
from app.config import settings

# ══════════════════════════════════════════════════════════════
# Fixtures — toujours réactiver le preamble avant chaque test
# ══════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _reset_preamble_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avant chaque test, on garantit l'état par défaut du preamble :
    enabled=True, max_chars=12000, locale=fr. Les tests qui veulent
    autre chose le monkeypatchent localement."""
    monkeypatch.setattr(settings, "nexya_preamble_enabled", True, raising=False)
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 12000, raising=False)
    monkeypatch.setattr(settings, "nexya_preamble_default_locale", "fr", raising=False)


# ══════════════════════════════════════════════════════════════
# 1. Happy paths
# ══════════════════════════════════════════════════════════════


def test_build_preamble_general_returns_non_empty() -> None:
    result = build_nexya_preamble("general")
    assert result is not None
    assert len(result) > 100


def test_build_preamble_cooking_includes_cooking_label() -> None:
    """Le routing guidance doit nommer l'expert actif (cooking)."""
    result = build_nexya_preamble("cooking")
    assert result is not None
    assert "Expert Cuisine" in result


def test_build_preamble_includes_tone_section() -> None:
    """Le bloc tone (10 commandements) doit être inclus."""
    result = build_nexya_preamble("general")
    assert result is not None
    assert "[Ton conversationnel NEXYA]" in result
    assert "1. **Tutoiement systématique.**" in result


def test_build_preamble_includes_identity_section() -> None:
    """Le bloc identité (4 paliers fondateur + brand security + produit + features)."""
    result = build_nexya_preamble("general")
    assert result is not None
    assert "[Identité NEXYA]" in result
    assert "[Sécurité Brand NEXYA]" in result


def test_build_preamble_includes_routing_section_by_default() -> None:
    """Le routing guidance est inclus par défaut."""
    result = build_nexya_preamble("general")
    assert result is not None
    assert "[Routing intelligent cross-expert]" in result


def test_build_preamble_can_omit_routing_section() -> None:
    """`include_routing=False` omet le bloc routing."""
    result = build_nexya_preamble("general", include_routing=False)
    assert result is not None
    assert "[Routing intelligent cross-expert]" not in result
    # Mais tone + identité restent.
    assert "[Ton conversationnel NEXYA]" in result
    assert "[Identité NEXYA]" in result


# ══════════════════════════════════════════════════════════════
# 2. Kill-switch
# ══════════════════════════════════════════════════════════════


def test_build_preamble_kill_switch_off_returns_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "nexya_preamble_enabled", False, raising=False)
    assert build_nexya_preamble("general") is None
    assert build_nexya_preamble("cooking") is None
    assert build_nexya_preamble(None) is None


# ══════════════════════════════════════════════════════════════
# 3. Cap chars + troncature
# ══════════════════════════════════════════════════════════════


def test_build_preamble_respects_max_chars_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 1500, raising=False)
    result = build_nexya_preamble("general")
    assert result is not None
    assert len(result) <= 1500


def test_build_preamble_truncation_adds_marker_fr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Quand troncature, marqueur explicite ajouté en fin."""
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 1500, raising=False)
    result = build_nexya_preamble("general", locale="fr")
    assert result is not None
    assert "préambule tronqué" in result


def test_build_preamble_truncation_adds_marker_en(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 1500, raising=False)
    result = build_nexya_preamble("general", locale="en")
    assert result is not None
    assert "preamble truncated" in result


def test_build_preamble_no_truncation_when_under_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cap très large = pas de marqueur de troncature."""
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 50_000, raising=False)
    result = build_nexya_preamble("general")
    assert result is not None
    assert "préambule tronqué" not in result
    assert "preamble truncated" not in result


# ══════════════════════════════════════════════════════════════
# 4. Locale handling
# ══════════════════════════════════════════════════════════════


def test_build_preamble_fr_explicit_uses_fr_content() -> None:
    result = build_nexya_preamble("general", locale="fr")
    assert result is not None
    assert "Tutoiement" in result  # FR (tone)
    # Marker FR de la section identité founder (palier 2, jamais tronqué
    # car ordre composition tone → routing → identity garantit que
    # founder reste en début d'identity, tronquable en queue uniquement).
    assert "Loth Ivan Ngassa Yimga" in result  # FR (founder palier 2)


def test_build_preamble_en_explicit_uses_en_content() -> None:
    result = build_nexya_preamble("general", locale="en")
    assert result is not None
    assert "Informal address" in result  # EN (tone)
    # Marker EN de l'identity founder (palier 2, jamais tronqué).
    assert "Loth Ivan Ngassa Yimga" in result  # EN (founder tier 2)


def test_build_preamble_default_locale_from_settings_fr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "nexya_preamble_default_locale", "fr", raising=False)
    result = build_nexya_preamble("general")
    assert result is not None
    assert "Tutoiement" in result


def test_build_preamble_default_locale_from_settings_en(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "nexya_preamble_default_locale", "en", raising=False)
    result = build_nexya_preamble("general")
    assert result is not None
    assert "Informal address" in result


# ══════════════════════════════════════════════════════════════
# 5. Expert ID handling
# ══════════════════════════════════════════════════════════════


def test_build_preamble_none_expert_id_falls_back_to_general() -> None:
    """None expert_id → routing nomme 'Général'."""
    result = build_nexya_preamble(None)
    assert result is not None
    assert "Général" in result


def test_build_preamble_unknown_expert_id_falls_back_to_general() -> None:
    """Slug inconnu → routing nomme 'Général'."""
    result = build_nexya_preamble("unknown_slug_xyz")
    assert result is not None
    assert "Général" in result


@pytest.mark.parametrize(
    "expert_id",
    [
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
    ],
)
def test_build_preamble_works_for_each_canonical_slug(expert_id: str) -> None:
    """Les 11 slugs canoniques produisent un preamble non-vide."""
    result = build_nexya_preamble(expert_id)
    assert result is not None
    assert len(result) > 500


# ══════════════════════════════════════════════════════════════
# 6. Idempotence stricte
# ══════════════════════════════════════════════════════════════


def test_build_preamble_idempotent_two_calls_byte_for_byte() -> None:
    """Deux appels identiques retournent exactement la même string."""
    a = build_nexya_preamble("cooking", locale="fr")
    b = build_nexya_preamble("cooking", locale="fr")
    assert a == b


def test_build_preamble_idempotent_across_locales() -> None:
    fr_a = build_nexya_preamble("general", locale="fr")
    fr_b = build_nexya_preamble("general", locale="fr")
    en_a = build_nexya_preamble("general", locale="en")
    en_b = build_nexya_preamble("general", locale="en")
    assert fr_a == fr_b
    assert en_a == en_b
    # FR != EN (sanity)
    assert fr_a != en_a


# ══════════════════════════════════════════════════════════════
# 7. Fail-safe absolue
# ══════════════════════════════════════════════════════════════


def test_build_preamble_failsafe_on_tone_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si get_tone raise, on log + return None (pas de propagation)."""

    def exploding_tone(*args, **kwargs) -> str:
        raise RuntimeError("simulated tone failure")

    monkeypatch.setattr(preamble_module, "get_tone", exploding_tone)

    result = build_nexya_preamble("general")
    assert result is None


def test_build_preamble_failsafe_on_identity_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exploding_identity(*args, **kwargs) -> str:
        raise RuntimeError("simulated identity failure")

    monkeypatch.setattr(preamble_module, "get_identity", exploding_identity)

    result = build_nexya_preamble("general")
    assert result is None


def test_build_preamble_failsafe_on_routing_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def exploding_routing(*args, **kwargs) -> str:
        raise RuntimeError("simulated routing failure")

    monkeypatch.setattr(preamble_module, "get_routing_guidance", exploding_routing)

    result = build_nexya_preamble("general")
    assert result is None


# ══════════════════════════════════════════════════════════════
# 8. Min cap (défensif)
# ══════════════════════════════════════════════════════════════


def test_build_preamble_min_cap_500_enforced(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cap < 500 est ramené à 500 (garde-fou helpers privés)."""
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 100, raising=False)
    result = build_nexya_preamble("general")
    assert result is not None
    # Le résultat doit tenir dans 500 chars (cap minimum imposé).
    assert len(result) <= 500


def test_build_preamble_invalid_max_chars_type_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Valeur de settings corrompue (non-int) ne crash pas."""
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", "not-a-number", raising=False)
    result = build_nexya_preamble("general")
    # Soit None (fail-safe), soit un résultat valide avec fallback 4000.
    assert result is None or len(result) > 0
