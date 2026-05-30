"""Tests `sms_draft_detector` (C4.5)."""

from __future__ import annotations

import pytest

from app.features.rich_content.sms_draft_detector import (
    detect_rich_content_sms,
    detect_sms_intent,
)


class TestDetectSmsIntent:
    """Intent classifier — message user upstream."""

    @pytest.mark.parametrize(
        "user_message",
        [
            "Rédige un SMS à mon client",
            "écris un texto pour Marie",
            "Peux-tu me préparer un SMS de relance ?",
            "SMS à mon fournisseur pour confirmer",
            "Tape un message texte pour confirmer le rendez-vous",
            "RÉDIGE UN SMS",
            "Envoie un SMS à mon père",
        ],
    )
    def test_fr_intent_detected(self, user_message: str) -> None:
        assert detect_sms_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            "Write an SMS to my client",
            "draft a text message for Marie",
            "compose a text to confirm the meeting",
            "SMS to my supplier",
            "send a text message to John",
        ],
    )
    def test_en_intent_detected(self, user_message: str) -> None:
        assert detect_sms_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            "Comment fonctionnent les SMS ?",
            "J'ai reçu un SMS suspect",
            "What is SMS?",
            "I got an SMS yesterday",
            "Combien coûte un SMS chez Orange ?",
            "",
            "   ",
        ],
    )
    def test_no_intent(self, user_message: str) -> None:
        assert detect_sms_intent(user_message) is False

    def test_none_input(self) -> None:
        assert detect_sms_intent(None) is False  # type: ignore[arg-type]


class TestDetectRichContentSms:
    """Point d'entrée — détection intent-based exclusive."""

    def test_intent_with_body_returns_payload(self) -> None:
        result = detect_rich_content_sms(
            user_message="Rédige un SMS à Marie pour confirmer le RDV",
            assistant_text="Salut Marie ! Je te confirme notre RDV demain à 10h. A demain !",
        )
        assert result is not None
        assert result["kind"] == "sms_draft"
        assert "Salut Marie" in result["data"]["body"]
        assert result["data"]["phone"] is None

    def test_no_intent_returns_none(self) -> None:
        result = detect_rich_content_sms(
            user_message="Comment fonctionnent les SMS ?",
            assistant_text="Salut ! Les SMS sont des messages texte de 160 caractères.",
        )
        assert result is None

    def test_intent_with_short_body_returns_none(self) -> None:
        result = detect_rich_content_sms(
            user_message="Rédige un SMS",
            assistant_text="OK",
        )
        assert result is None

    def test_empty_inputs_return_none(self) -> None:
        assert detect_rich_content_sms("", "") is None
        assert detect_rich_content_sms("Rédige un SMS", "") is None

    def test_long_body_capped_at_1600(self) -> None:
        long_body = "Salut Marie ! " + ("Lorem ipsum. " * 200)  # > 1600 chars
        result = detect_rich_content_sms(
            user_message="Rédige un SMS pour Marie",
            assistant_text=long_body,
        )
        assert result is not None
        assert len(result["data"]["body"]) <= 1_600
