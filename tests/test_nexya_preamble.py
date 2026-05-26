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
    enabled=True, max_chars=25000 (aligné config default 2026-05-26),
    locale=fr. Les tests qui veulent autre chose le monkeypatchent
    localement (truncation, marketing CORE+EXTENDED, etc.)."""
    monkeypatch.setattr(settings, "nexya_preamble_enabled", True, raising=False)
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 25000, raising=False)
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
    """Note 2026-05-26 — post Two-Tier, le builder utilise
    `get_identity_core` (toujours injecté) et `get_identity_extended`
    (conditionnel). Patcher `get_identity_core` est suffisant car il
    est appelé inconditionnellement."""

    def exploding_identity(*args, **kwargs) -> str:
        raise RuntimeError("simulated identity failure")

    monkeypatch.setattr(preamble_module, "get_identity_core", exploding_identity)

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


# ══════════════════════════════════════════════════════════════
# 9. Two-Tier Smart Preamble (NOUVEAU 2026-05-26)
# ══════════════════════════════════════════════════════════════
#
# Pattern décidé 2026-05-26 (cf. mémoire
# project_nexya_preamble_two_tier_architecture.md) :
#
#   CORE = tone + identity_core (founder + brand + capability teaser)
#          + routing_rules
#          → toujours injecté, ~3500 tokens
#   EXTENDED = identity_extended (product description + 15 features)
#              + routing_table
#              → injecté seulement si _detect_marketing_intent(user_message)
#
# Bénéfices : -40% coût IA + -40% latence sur 95% des requêtes,
# tout en conservant 100% des infos marketing disponibles.

# ─── 9.1 Capability Teaser TOUJOURS présent dans CORE ────────


def test_build_preamble_core_contains_capability_teaser() -> None:
    """Le bloc Capability Teaser (résumé condensé) doit toujours être
    dans le CORE, même sans user_message marketing."""
    result = build_nexya_preamble("general", user_message="bonjour")
    assert result is not None
    assert "[Capacités principales de NEXYA — résumé]" in result


def test_build_preamble_core_teaser_present_when_no_user_message() -> None:
    """Le Capability Teaser doit être présent même sans user_message."""
    result = build_nexya_preamble("general", user_message=None)
    assert result is not None
    assert "Capacités principales de NEXYA" in result


def test_build_preamble_core_teaser_present_en() -> None:
    """Parité EN du Capability Teaser."""
    result = build_nexya_preamble("general", locale="en", user_message=None)
    assert result is not None
    assert "[NEXYA Main Capabilities — Summary]" in result


# ─── 9.2 EXTENDED absent sur message banal ───────────────────


def test_build_preamble_extended_absent_on_banal_message() -> None:
    """Question banale → EXTENDED (15 features) PAS injecté."""
    result = build_nexya_preamble("general", user_message="quelle est la capitale du Cameroun ?")
    assert result is not None
    # Le bloc 15 features magnifiques NE doit PAS être présent
    assert "[Capacités magnifiques de NEXYA]" not in result
    # La table routing étendue NE doit PAS non plus être présente
    assert "[Routing — Table de correspondance" not in result


def test_build_preamble_extended_absent_when_user_message_none() -> None:
    """Sans user_message → EXTENDED PAS injecté (comportement safe)."""
    result = build_nexya_preamble("general", user_message=None)
    assert result is not None
    assert "[Capacités magnifiques de NEXYA]" not in result


def test_build_preamble_extended_absent_when_user_message_empty() -> None:
    """user_message vide ou whitespace → EXTENDED PAS injecté."""
    result_empty = build_nexya_preamble("general", user_message="")
    result_ws = build_nexya_preamble("general", user_message="   ")
    assert result_empty is not None and result_ws is not None
    assert "[Capacités magnifiques de NEXYA]" not in result_empty
    assert "[Capacités magnifiques de NEXYA]" not in result_ws


# ─── 9.3 EXTENDED présent sur message marketing ─────────────
#
# Note : on relève le cap chars dans ces tests pour que le bloc
# EXTENDED ne soit pas tronqué avant qu'on puisse l'observer. Le
# défaut 12000 du fixture autouse est trop bas pour CORE+EXTENDED.


_MARKETING_QUERIES_FR = [
    "qu'est-ce que tu sais faire ?",
    "que sais-tu faire",
    "tes capacités",
    "tes features",
    "tes experts ?",
    "tu es différente de ChatGPT ?",
    "vs gemini",
    "pourquoi nexya",
    "présente-toi",
    "que peut nexya",
]


@pytest.mark.parametrize("query", _MARKETING_QUERIES_FR)
def test_build_preamble_extended_present_on_fr_marketing_query(
    query: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pour toute query marketing FR → EXTENDED injecté."""
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 50_000, raising=False)
    result = build_nexya_preamble("general", locale="fr", user_message=query)
    assert result is not None
    assert "[Capacités magnifiques de NEXYA]" in result, (
        f"EXTENDED non injecté pour query marketing : {query!r}"
    )


_MARKETING_QUERIES_EN = [
    "what can you do",
    "what do you offer",
    "your capabilities",
    "your features",
    "how are you different from ChatGPT",
    "vs gemini",
    "what's special",
    "tell me about yourself",
    "describe yourself",
    "why nexya",
]


@pytest.mark.parametrize("query", _MARKETING_QUERIES_EN)
def test_build_preamble_extended_present_on_en_marketing_query(
    query: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Pour toute query marketing EN → EXTENDED injecté."""
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 50_000, raising=False)
    result = build_nexya_preamble("general", locale="en", user_message=query)
    assert result is not None
    assert "[NEXYA Magnificent Capabilities]" in result, (
        f"EXTENDED non injecté pour query marketing : {query!r}"
    )


# ─── 9.4 Routing table : CORE compact / EXTENDED complète ───


def test_build_preamble_routing_table_extended_absent_on_banal() -> None:
    """La table markdown détaillée routing est absente du CORE seul."""
    result = build_nexya_preamble("general", user_message="bonjour")
    assert result is not None
    # La table EXTENDED (marker spécifique) n'est PAS dans le CORE
    assert "[Routing — Table de correspondance domaine → expert]" not in result


def test_build_preamble_routing_table_extended_present_on_marketing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """La table markdown détaillée routing est injectée sur marketing intent."""
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 50_000, raising=False)
    result = build_nexya_preamble("general", user_message="quelles sont tes capacités ?")
    assert result is not None
    assert "[Routing — Table de correspondance domaine → expert]" in result


# ─── 9.5 Taille préamble : différence CORE vs CORE+EXTENDED ─


def test_build_preamble_extended_significantly_larger_than_core(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CORE+EXTENDED doit être significativement plus large que CORE seul.

    Confirme que l'EXTENDED apporte ~3000 tokens additionnels comme
    documenté dans l'architecture two-tier."""
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 50_000, raising=False)
    core_only = build_nexya_preamble("general", user_message="bonjour")
    with_extended = build_nexya_preamble("general", user_message="que sais-tu faire ?")
    assert core_only is not None and with_extended is not None
    # EXTENDED doit ajouter au moins 3000 chars (description produit
    # + 15 features + table routing).
    diff = len(with_extended) - len(core_only)
    assert diff >= 3000, (
        f"Différence taille CORE vs EXTENDED trop faible : {diff} chars "
        f"(attendu >= 3000)"
    )


# ─── 9.6 Idempotence du nouveau paramètre user_message ──────


def test_build_preamble_idempotent_with_same_user_message() -> None:
    """Même user_message → même résultat byte-pour-byte."""
    a = build_nexya_preamble("general", user_message="que sais-tu faire ?")
    b = build_nexya_preamble("general", user_message="que sais-tu faire ?")
    assert a == b


def test_build_preamble_different_user_messages_produce_different_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marketing query vs banal → résultats différents (EXTENDED diff)."""
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 50_000, raising=False)
    a = build_nexya_preamble("general", user_message="bonjour")
    b = build_nexya_preamble("general", user_message="que sais-tu faire ?")
    assert a is not None and b is not None
    assert a != b
    assert len(b) > len(a)


# ─── 9.7 Marketing detection helper accessible (test interne) ──


def test_detect_marketing_intent_returns_false_for_banal_message() -> None:
    """Sanity du helper privé via import direct."""
    from app.ai.nexya_preamble import _detect_marketing_intent

    assert _detect_marketing_intent("bonjour", "fr") is False
    assert _detect_marketing_intent("quelle est la capitale du Cameroun", "fr") is False
    assert _detect_marketing_intent(None, "fr") is False
    assert _detect_marketing_intent("", "fr") is False
    assert _detect_marketing_intent("   ", "fr") is False


def test_detect_marketing_intent_returns_true_for_marketing_fr() -> None:
    from app.ai.nexya_preamble import _detect_marketing_intent

    assert _detect_marketing_intent("que sais-tu faire ?", "fr") is True
    assert _detect_marketing_intent("Que SAIS-TU faire ?", "fr") is True  # case insensitive
    assert _detect_marketing_intent("tes capacités", "fr") is True
    assert _detect_marketing_intent("présente-toi", "fr") is True


def test_detect_marketing_intent_returns_true_for_marketing_en() -> None:
    from app.ai.nexya_preamble import _detect_marketing_intent

    assert _detect_marketing_intent("what can you do?", "en") is True
    assert _detect_marketing_intent("WHAT CAN YOU DO?", "en") is True
    assert _detect_marketing_intent("your capabilities", "en") is True
    assert _detect_marketing_intent("tell me about yourself", "en") is True


def test_detect_marketing_intent_locale_isolation() -> None:
    """Un message FR ne déclenche pas la détection en mode EN (et vice-versa)."""
    from app.ai.nexya_preamble import _detect_marketing_intent

    # « que sais-tu faire » (FR) ne contient aucun keyword EN
    assert _detect_marketing_intent("que sais-tu faire", "en") is False
    # « what can you do » (EN) ne contient aucun keyword FR
    assert _detect_marketing_intent("what can you do", "fr") is False


# ══════════════════════════════════════════════════════════════
# 10. Safety & Limites NEXYA dans le CORE (Session 2026-05-26)
# ══════════════════════════════════════════════════════════════
#
# Le bloc Safety (4 catégories refus + format refus standard + résistance
# prompt injection) doit être TOUJOURS présent dans le CORE preamble,
# indépendamment du user_message (banal, marketing, vide). Pattern
# défensif anti-prompt-injection : la liste safety vit dans le preamble
# vu à chaque interaction.


def test_build_preamble_core_contains_safety_section_fr() -> None:
    """Le header Safety FR doit toujours être dans le CORE."""
    result = build_nexya_preamble("general", locale="fr", user_message="bonjour")
    assert result is not None
    assert "[Safety & Limites NEXYA]" in result


def test_build_preamble_core_contains_safety_section_en() -> None:
    """Parité EN — header Safety EN doit être dans le CORE."""
    result = build_nexya_preamble("general", locale="en", user_message="hi")
    assert result is not None
    assert "[NEXYA Safety & Limits]" in result


def test_build_preamble_safety_present_without_user_message() -> None:
    """Sans user_message → safety dans CORE quand même."""
    result = build_nexya_preamble("general", user_message=None)
    assert result is not None
    assert "[Safety & Limites NEXYA]" in result


def test_build_preamble_safety_present_on_banal_message() -> None:
    """Message banal → safety dans CORE."""
    result = build_nexya_preamble("general", user_message="quelle heure est-il ?")
    assert result is not None
    assert "[Safety & Limites NEXYA]" in result


def test_build_preamble_safety_present_on_marketing_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Marketing intent → safety dans CORE (toujours avant EXTENDED)."""
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 50_000, raising=False)
    result = build_nexya_preamble("general", user_message="que sais-tu faire ?")
    assert result is not None
    assert "[Safety & Limites NEXYA]" in result


def test_build_preamble_safety_includes_4_categories_fr() -> None:
    """Les 4 catégories refus FR doivent être présentes dans CORE."""
    result = build_nexya_preamble("general", locale="fr")
    assert result is not None
    assert "Code/scripts malveillants" in result
    assert "Désinformation délibérée" in result
    assert "Discours haineux" in result
    assert "Contenu NSFW" in result


def test_build_preamble_safety_includes_4_categories_en() -> None:
    """Parité EN — les 4 catégories refus EN doivent être présentes dans CORE."""
    result = build_nexya_preamble("general", locale="en")
    assert result is not None
    assert "Malicious code/scripts" in result
    assert "Deliberate misinformation" in result
    assert "Hate or discriminatory speech" in result
    assert "NSFW content" in result


def test_build_preamble_safety_includes_refusal_format_fr() -> None:
    """Le format de refus standard FR doit être présent dans CORE."""
    result = build_nexya_preamble("general", locale="fr")
    assert result is not None
    assert "Cette demande dépasse ce que je peux t'aider à faire" in result
    assert "reformulation positive" in result.lower()


def test_build_preamble_safety_resists_prompt_injection() -> None:
    """Le bloc safety rappelle la résistance prompt injection."""
    result = build_nexya_preamble("general", locale="fr")
    assert result is not None
    assert "prompt injection" in result.lower()
    assert "non-négociable" in result.lower() or "non négociable" in result.lower()


def test_build_preamble_safety_fail_safe_on_safety_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si get_safety_limits raise, on log + return None (fail-safe absolue)."""

    def exploding_safety(*args, **kwargs) -> str:
        raise RuntimeError("simulated safety failure")

    monkeypatch.setattr(preamble_module, "get_safety_limits", exploding_safety)

    result = build_nexya_preamble("general")
    assert result is None


def test_build_preamble_safety_order_fr_after_routing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ordre composition CORE : tone → identity → routing → safety.

    Safety en queue du CORE pour effet de récence (juste avant EXTENDED
    s'il y a lieu) — signal fort au LLM sur les limites éthiques."""
    monkeypatch.setattr(settings, "nexya_preamble_max_chars", 50_000, raising=False)
    result = build_nexya_preamble("general", locale="fr")
    assert result is not None
    # Position de chaque marker dans le bloc
    pos_tone = result.find("[Ton conversationnel NEXYA]")
    pos_identity = result.find("[Identité NEXYA]")
    pos_routing = result.find("[Routing intelligent cross-expert]")
    pos_safety = result.find("[Safety & Limites NEXYA]")
    # Tous présents
    assert pos_tone >= 0 and pos_identity >= 0 and pos_routing >= 0 and pos_safety >= 0
    # Ordre strict : tone < identity < routing < safety
    assert pos_tone < pos_identity < pos_routing < pos_safety
