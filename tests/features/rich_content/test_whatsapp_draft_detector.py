"""Tests `whatsapp_draft_detector` (C4.4)."""

from __future__ import annotations

import pytest

from app.features.rich_content.whatsapp_draft_detector import (
    detect_rich_content_whatsapp,
    detect_whatsapp_intent,
)


class TestDetectWhatsAppIntent:
    """Intent classifier — message user upstream."""

    @pytest.mark.parametrize(
        "user_message",
        [
            "Rédige un message WhatsApp à mon client",
            "écris un WhatsApp pour Marie",
            "Peux-tu me préparer un message WhatsApp de relance ?",
            "WhatsApp à mon fournisseur pour confirmer",
            "Tape un message WhatsApp pour confirmer le rendez-vous",
            "RÉDIGE UN MESSAGE WHATSAPP",
        ],
    )
    def test_fr_intent_detected(self, user_message: str) -> None:
        assert detect_whatsapp_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            "Write a WhatsApp message to my client",
            "draft a WhatsApp for Marie",
            "compose a WhatsApp to confirm the meeting",
            "WhatsApp to my supplier",
            "write a whatsapp to John",
        ],
    )
    def test_en_intent_detected(self, user_message: str) -> None:
        assert detect_whatsapp_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            "Comment installer WhatsApp ?",
            "WhatsApp est-il sécurisé ?",
            "J'ai reçu un message WhatsApp suspect",
            "What is WhatsApp Business?",
            "I got a WhatsApp message",
            "Comment marche WhatsApp Business ?",
            "",
            "   ",
        ],
    )
    def test_no_intent(self, user_message: str) -> None:
        assert detect_whatsapp_intent(user_message) is False

    def test_none_input(self) -> None:
        assert detect_whatsapp_intent(None) is False  # type: ignore[arg-type]


class TestDetectRichContentWhatsApp:
    """Point d'entrée — détection intent-based exclusive."""

    def test_intent_with_body_returns_payload(self) -> None:
        result = detect_rich_content_whatsapp(
            user_message="Rédige un WhatsApp à Marie pour confirmer le RDV",
            assistant_text="Salut Marie ! Je te confirme notre RDV demain à 10h. À demain !",
        )
        assert result is not None
        assert result["kind"] == "whatsapp_draft"
        assert "Salut Marie" in result["data"]["body"]
        assert result["data"]["phone"] is None

    def test_no_intent_returns_none_even_with_body(self) -> None:
        # User asked something else, even if response is conversational
        result = detect_rich_content_whatsapp(
            user_message="Comment fonctionne WhatsApp ?",
            assistant_text="Salut ! WhatsApp est une appli de messagerie instantanée.",
        )
        assert result is None

    def test_intent_with_short_body_returns_none(self) -> None:
        result = detect_rich_content_whatsapp(
            user_message="Rédige un WhatsApp",
            assistant_text="Salut",
        )
        assert result is None

    def test_empty_inputs_return_none(self) -> None:
        assert detect_rich_content_whatsapp("", "") is None
        assert detect_rich_content_whatsapp("Rédige un WhatsApp", "") is None

    def test_long_body_capped_at_10k(self) -> None:
        long_body = "Salut Marie ! " + ("Lorem ipsum. " * 1000)  # > 10k chars
        result = detect_rich_content_whatsapp(
            user_message="Rédige un WhatsApp pour Marie",
            assistant_text=long_body,
        )
        assert result is not None
        assert len(result["data"]["body"]) <= 10_000
