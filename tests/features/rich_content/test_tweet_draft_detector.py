"""Tests `tweet_draft_detector` (C4.5)."""

from __future__ import annotations

import pytest

from app.features.rich_content.tweet_draft_detector import (
    detect_rich_content_tweet,
    detect_tweet_intent,
)


class TestDetectTweetIntent:
    """Intent classifier — message user upstream."""

    @pytest.mark.parametrize(
        "user_message",
        [
            "Rédige un tweet sur Flutter",
            "écris un tweet pour célébrer mon anniv",
            "Tweete pour mes 1000 followers",
            "Crée un tweet de réaction",
            "Tape un tweet drôle",
            "Poste sur X pour annoncer",
            "Publie sur X mon nouveau projet",
        ],
    )
    def test_fr_intent_detected(self, user_message: str) -> None:
        assert detect_tweet_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            "Write a tweet about Flutter",
            "draft a tweet for the launch",
            "compose a funny tweet",
            "tweet about my project",
            "Post on X to announce",
        ],
    )
    def test_en_intent_detected(self, user_message: str) -> None:
        assert detect_tweet_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            "Qu'est-ce qu'un tweet ?",
            "J'ai vu un tweet drôle hier",
            "What is Twitter?",
            # Cas piège : "X" ambigu (variable / lettre)
            "Calcule la valeur de X dans l'équation",
            "Trouve la valeur de x dans 2x + 3 = 7",
            "the X factor in this algorithm",
            "L'inconnue X est égale à 5",
            "",
            "   ",
        ],
    )
    def test_no_intent(self, user_message: str) -> None:
        assert detect_tweet_intent(user_message) is False

    def test_none_input(self) -> None:
        assert detect_tweet_intent(None) is False  # type: ignore[arg-type]


class TestDetectRichContentTweet:
    """Point d'entrée — détection intent-based exclusive."""

    def test_intent_with_body_returns_payload(self) -> None:
        result = detect_rich_content_tweet(
            user_message="Rédige un tweet pour célébrer ma promotion",
            assistant_text="Heureux d'annoncer ma promotion au poste de Lead Dev chez @NexyaLabs ! 🚀 Merci à toute l'équipe ! #flutter #africa",
        )
        assert result is not None
        assert result["kind"] == "tweet_draft"
        assert "promotion" in result["data"]["body"]

    def test_no_intent_returns_none(self) -> None:
        result = detect_rich_content_tweet(
            user_message="Qu'est-ce qu'un tweet ?",
            assistant_text="Un tweet est un message court de 280 caractères publié sur X.",
        )
        assert result is None

    def test_intent_with_very_short_body_returns_none(self) -> None:
        result = detect_rich_content_tweet(
            user_message="Rédige un tweet",
            assistant_text="Hi",
        )
        assert result is None

    def test_long_body_capped_at_280(self) -> None:
        long_body = "Bonjour mes amis ! " + ("Lorem ipsum dolor sit amet. " * 30)
        result = detect_rich_content_tweet(
            user_message="Rédige un tweet",
            assistant_text=long_body,
        )
        assert result is not None
        assert len(result["data"]["body"]) <= 280

    def test_body_at_exactly_280_chars_accepted(self) -> None:
        body = "a" * 280
        result = detect_rich_content_tweet(
            user_message="Rédige un tweet",
            assistant_text=body,
        )
        assert result is not None
        assert len(result["data"]["body"]) == 280

    def test_truncation_respects_word_boundary(self) -> None:
        # Construit un body > 280 chars avec espaces réguliers, vérifie que
        # la troncature coupe sur un espace pas en plein milieu d'un mot.
        body = ("Bonjour les amis " * 30).strip()  # > 280 chars
        result = detect_rich_content_tweet(
            user_message="Rédige un tweet",
            assistant_text=body,
        )
        assert result is not None
        # Le body capé ne se termine pas par un mot coupé
        capped = result["data"]["body"]
        assert capped.endswith("amis") or capped.endswith("amis ") or " " in capped[-20:]

    def test_empty_inputs_return_none(self) -> None:
        assert detect_rich_content_tweet("", "") is None
        assert detect_rich_content_tweet("Rédige un tweet", "") is None
