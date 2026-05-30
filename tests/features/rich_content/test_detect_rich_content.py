"""Tests `detect_rich_content` (entry point combinant email + whatsapp)."""

from __future__ import annotations

from app.features.rich_content import detect_rich_content


class TestDetectRichContent:
    def test_email_takes_precedence_when_no_whatsapp_intent(self) -> None:
        result = detect_rich_content(
            user_message="Rédige un mail de relance à mon fournisseur",
            assistant_text=(
                "Sujet : Relance commande\n\n"
                "Bonjour Monsieur Diallo,\n\n"
                "Je me permets de vous relancer.\n\n"
                "Cordialement,\nIvan"
            ),
        )
        assert result is not None
        assert result["kind"] == "email_draft"

    def test_whatsapp_detected_when_intent(self) -> None:
        result = detect_rich_content(
            user_message="Rédige un WhatsApp à Marie",
            assistant_text="Salut Marie ! On se voit demain à 10h ?",
        )
        assert result is not None
        assert result["kind"] == "whatsapp_draft"

    def test_whatsapp_intent_priority_over_email_body(self) -> None:
        # WhatsApp intent + email-looking body → WhatsApp wins (intent more specific)
        result = detect_rich_content(
            user_message="Rédige un WhatsApp pour mon client",
            assistant_text=(
                "Sujet : Confirmation\n\n"
                "Bonjour Madame,\n\nVoici la confirmation.\n\nCordialement,\nIvan"
            ),
        )
        assert result is not None
        assert result["kind"] == "whatsapp_draft"

    def test_no_intent_no_body_returns_none(self) -> None:
        result = detect_rich_content(
            user_message="Quelle est la capitale du Cameroun ?",
            assistant_text="La capitale est Yaoundé.",
        )
        assert result is None

    def test_empty_assistant_text_returns_none(self) -> None:
        assert detect_rich_content("Rédige un mail", "") is None
        assert detect_rich_content("Rédige un mail", "   ") is None

    def test_non_string_inputs_return_none(self) -> None:
        assert detect_rich_content(None, "body") is None  # type: ignore[arg-type]
        assert detect_rich_content("user", None) is None  # type: ignore[arg-type]
        assert detect_rich_content(123, "body") is None  # type: ignore[arg-type]

    def test_payload_is_dict_ready_for_metadata_json(self) -> None:
        """Le retour doit être un dict directement insérable dans metadata_json JSONB."""
        result = detect_rich_content(
            user_message="Rédige un mail à Marie",
            assistant_text=(
                "Sujet : Bonjour\n\nBonjour Marie,\n\nÇa va ?\n\nCordialement,\nIvan"
            ),
        )
        assert isinstance(result, dict)
        assert "kind" in result
        assert "data" in result
        assert isinstance(result["data"], dict)
