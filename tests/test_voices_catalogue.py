"""N1 — Tests catalogue voix NEXYA (constante Python)."""

from __future__ import annotations

from app.features.voice.voices_catalogue import get_voice_catalogue


def test_catalogue_returns_6_voices():
    voices = get_voice_catalogue()
    assert len(voices) == 6


def test_catalogue_voice_ids_match_flutter():
    """Les 6 IDs doivent matcher EXACTEMENT le Flutter
    `nexya_front_end/lib/features/settings/models/voice_model.dart`.
    """
    voices = get_voice_catalogue()
    ids = [v.id for v in voices]
    assert ids == ["aurora", "memora", "soleil", "sagesse", "eron", "nyanga"]


def test_catalogue_tones_valid():
    """Tous les tones sont dans Literal['deep', 'medium', 'high']."""
    voices = get_voice_catalogue()
    valid_tones = {"deep", "medium", "high"}
    for voice in voices:
        assert voice.tone in valid_tones


def test_catalogue_ids_unique():
    voices = get_voice_catalogue()
    ids = [v.id for v in voices]
    assert len(ids) == len(set(ids))


def test_catalogue_returns_fresh_list_not_shared_state():
    """Helper retourne une liste copiée (mutations externes ne
    polluent pas la constante)."""
    a = get_voice_catalogue()
    b = get_voice_catalogue()
    a.pop()
    assert len(b) == 6  # pas affecté
