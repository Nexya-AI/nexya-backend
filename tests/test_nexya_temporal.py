"""
Tests planner-from-chat LOT 2 — `app.ai.nexya_temporal`.

Couvre les deux blocs contextuels injectés à la runtime dans le system
prompt :
- `build_temporal_context()` : date/heure UTC courante (le LLM en a besoin
  pour transformer « demain 8h » en date ISO absolue).
- `build_tools_guidance()` : doctrine d'usage des 4 tools Planner, source
  unique (sortie de `expert_prompts/general.py` au LOT 2).

Étend également le fix timezone (2026-05-23) : avec `client_timezone="+01:00"`,
le bloc doit injecter l'heure LOCALE de l'utilisateur + instruction au LLM
de produire ses ISO datetimes avec l'offset.

Fonctions pures — aucun appel LLM, aucune I/O.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.ai.nexya_temporal import (
    _format_offset_iso,
    _parse_client_timezone,
    build_temporal_context,
    build_tools_guidance,
)

# ══════════════════════════════════════════════════════════════
# build_temporal_context
# ══════════════════════════════════════════════════════════════

# 2026-05-22 est un vendredi (weekday()==4).
_FIXED_NOW = datetime(2026, 5, 22, 14, 37, tzinfo=UTC)


def test_temporal_context_has_header() -> None:
    block = build_temporal_context(now=_FIXED_NOW)
    assert "[Contexte temporel" in block


def test_temporal_context_contains_french_weekday_and_month() -> None:
    block = build_temporal_context(now=_FIXED_NOW)
    assert "vendredi" in block
    assert "22 mai 2026" in block


def test_temporal_context_contains_iso_today() -> None:
    block = build_temporal_context(now=_FIXED_NOW)
    assert "2026-05-22" in block


def test_temporal_context_contains_utc_time() -> None:
    block = build_temporal_context(now=_FIXED_NOW)
    assert "14:37 UTC" in block


def test_temporal_context_computes_tomorrow() -> None:
    block = build_temporal_context(now=_FIXED_NOW)
    # 2026-05-23 = samedi.
    assert "2026-05-23" in block
    assert "samedi" in block


def test_temporal_context_computes_day_after_tomorrow() -> None:
    block = build_temporal_context(now=_FIXED_NOW)
    # 2026-05-24 = dimanche.
    assert "2026-05-24" in block
    assert "dimanche" in block


def test_temporal_context_states_weekday_convention() -> None:
    """Le bloc doit donner la convention 0=lundi pour les tools."""
    block = build_temporal_context(now=_FIXED_NOW)
    assert "0=lundi" in block
    assert "6=dimanche" in block


def test_temporal_context_mentions_utc_and_relative_dates() -> None:
    block = build_temporal_context(now=_FIXED_NOW)
    assert "UTC" in block
    assert "relative" in block.lower()


def test_temporal_context_naive_datetime_treated_as_utc() -> None:
    """Un `now` naïf est interprété comme UTC (pas de crash, heure brute)."""
    naive = datetime(2026, 5, 22, 9, 5)  # noqa: DTZ001 — test volontaire
    block = build_temporal_context(now=naive)
    assert "09:05 UTC" in block


def test_temporal_context_aware_non_utc_converted_to_utc() -> None:
    """Un `now` aware non-UTC est converti en UTC."""
    plus_two = datetime(2026, 5, 22, 14, 37, tzinfo=timezone(timedelta(hours=2)))
    block = build_temporal_context(now=plus_two)
    # 14:37 UTC+2 → 12:37 UTC.
    assert "12:37 UTC" in block


def test_temporal_context_default_now_does_not_crash() -> None:
    """Sans argument, utilise `datetime.now(UTC)` — doit produire un bloc."""
    block = build_temporal_context()
    assert "[Contexte temporel" in block
    assert "UTC" in block


def test_temporal_context_is_idempotent_for_fixed_now() -> None:
    a = build_temporal_context(now=_FIXED_NOW)
    b = build_temporal_context(now=_FIXED_NOW)
    assert a == b


# ══════════════════════════════════════════════════════════════
# build_tools_guidance
# ══════════════════════════════════════════════════════════════


def test_tools_guidance_returns_non_empty() -> None:
    guidance = build_tools_guidance()
    assert guidance and len(guidance) > 200


def test_tools_guidance_mentions_all_four_planner_tools() -> None:
    guidance = build_tools_guidance()
    for name in ("create_task", "list_tasks", "update_task", "pause_task"):
        assert name in guidance, f"{name} absent de build_tools_guidance()"


def test_tools_guidance_states_priority_rule() -> None:
    """La doctrine impose d'appeler le tool plutôt que répondre en texte."""
    lowered = build_tools_guidance().lower()
    assert "appelle" in lowered
    assert "tool" in lowered


def test_tools_guidance_requires_self_sufficient_prompt() -> None:
    """Le champ `prompt` de create_task doit être auto-suffisant."""
    lowered = build_tools_guidance().lower()
    assert "auto-suffisant" in lowered or "autonome" in lowered


def test_tools_guidance_mentions_confirmation_after_execution() -> None:
    lowered = build_tools_guidance().lower()
    assert "confirm" in lowered


def test_tools_guidance_is_idempotent() -> None:
    assert build_tools_guidance() == build_tools_guidance()


# ══════════════════════════════════════════════════════════════
# tz-fix (2026-05-23) — _parse_client_timezone
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "raw,expected_minutes",
    [
        ("+01:00", 60),
        ("+00:00", 0),
        ("Z", 0),
        ("z", 0),  # tolère minuscule
        ("-05:00", -300),
        ("+05:30", 330),  # Inde
        ("-09:30", -570),  # Marquises
        ("+14:00", 840),  # Kiribati (extrême positif)
        ("-12:00", -720),
    ],
)
def test_parse_client_timezone_valid(raw: str, expected_minutes: int) -> None:
    tz = _parse_client_timezone(raw)
    assert tz is not None
    offset = tz.utcoffset(None)
    assert offset is not None
    assert int(offset.total_seconds() // 60) == expected_minutes


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "+1:00",  # heure pas 2 chiffres
        "+01:0",  # minute pas 2 chiffres
        "01:00",  # sans signe
        "+25:00",  # hors borne
        "+99:99",
        "abc",
        "+01:60",  # minute invalide
        "+aa:bb",
        "+15:00",  # > +14:00
        "GMT+1",
        "Europe/Paris",  # IANA non supporté V1
    ],
)
def test_parse_client_timezone_invalid_returns_none(raw: str | None) -> None:
    assert _parse_client_timezone(raw) is None


def test_parse_client_timezone_strips_whitespace() -> None:
    tz = _parse_client_timezone("  +02:00  ")
    assert tz is not None
    assert tz.utcoffset(None) == timedelta(hours=2)


# ══════════════════════════════════════════════════════════════
# tz-fix — _format_offset_iso
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "td,expected",
    [
        (timedelta(hours=1), "+01:00"),
        (timedelta(hours=0), "+00:00"),
        (timedelta(hours=-5), "-05:00"),
        (timedelta(hours=5, minutes=30), "+05:30"),
        (timedelta(hours=-9, minutes=-30), "-09:30"),
        (timedelta(hours=14), "+14:00"),
    ],
)
def test_format_offset_iso(td: timedelta, expected: str) -> None:
    assert _format_offset_iso(timezone(td)) == expected


# ══════════════════════════════════════════════════════════════
# tz-fix — build_temporal_context avec client_timezone
# ══════════════════════════════════════════════════════════════


def test_temporal_context_with_client_tz_uses_local_time_header() -> None:
    """Avec client_tz="+01:00", le bloc parle d'heure utilisateur, pas serveur."""
    block = build_temporal_context(now=_FIXED_NOW, client_timezone="+01:00")
    assert "[Contexte temporel — horloge utilisateur]" in block
    assert "UTC+01:00" in block
    # 14:37 UTC + 1h = 15:37 locale.
    assert "15:37" in block


def test_temporal_context_with_negative_client_tz() -> None:
    """Offset négatif (Amériques) : l'heure locale est antérieure à UTC."""
    block = build_temporal_context(now=_FIXED_NOW, client_timezone="-05:00")
    assert "UTC-05:00" in block
    # 14:37 UTC - 5h = 09:37 locale.
    assert "09:37" in block


def test_temporal_context_with_client_tz_keeps_utc_for_info() -> None:
    """L'heure UTC doit rester affichée comme info — utile au LLM si doute."""
    block = build_temporal_context(now=_FIXED_NOW, client_timezone="+01:00")
    assert "2026-05-22 14:37 UTC" in block


def test_temporal_context_with_client_tz_instructs_llm_to_use_offset() -> None:
    """Le LLM doit recevoir une instruction explicite : utiliser l'offset."""
    block = build_temporal_context(now=_FIXED_NOW, client_timezone="+01:00")
    lowered = block.lower()
    assert "heure locale" in lowered
    # L'ISO d'exemple doit contenir l'offset, pas UTC.
    assert "+01:00`)" in block or "+01:00`" in block


def test_temporal_context_with_client_tz_handles_date_rollover() -> None:
    """22:00 UTC + 3h offset = 01:00 le lendemain locale → jour différent."""
    late_utc = datetime(2026, 5, 22, 22, 0, tzinfo=UTC)
    block = build_temporal_context(now=late_utc, client_timezone="+03:00")
    # 22:00 UTC + 3h = 01:00 le 23/05 locale.
    assert "01:00" in block
    assert "2026-05-23" in block


def test_temporal_context_invalid_client_tz_falls_back_to_utc() -> None:
    """Offset invalide → fallback strict sur le mode UTC-only legacy."""
    block = build_temporal_context(now=_FIXED_NOW, client_timezone="invalid")
    assert "[Contexte temporel — horloge serveur NEXYA]" in block
    assert "14:37 UTC" in block
    assert "horloge utilisateur" not in block


def test_temporal_context_none_client_tz_falls_back_to_utc() -> None:
    """`None` → comportement legacy strictement préservé."""
    block_none = build_temporal_context(now=_FIXED_NOW, client_timezone=None)
    block_legacy = build_temporal_context(now=_FIXED_NOW)
    assert block_none == block_legacy


def test_temporal_context_z_client_tz_is_utc() -> None:
    """`Z` est l'alias ISO de UTC → bloc avec offset +00:00 visible."""
    block = build_temporal_context(now=_FIXED_NOW, client_timezone="Z")
    assert "UTC+00:00" in block
    # 14:37 UTC reste 14:37 locale.
    assert "14:37" in block


def test_temporal_context_with_client_tz_idempotent() -> None:
    a = build_temporal_context(now=_FIXED_NOW, client_timezone="+01:00")
    b = build_temporal_context(now=_FIXED_NOW, client_timezone="+01:00")
    assert a == b
