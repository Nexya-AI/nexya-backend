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


class TestDetectRichContentCascadeC45:
    """Tests de la cascade C4.5 (6 kinds : WhatsApp > SMS > Tweet > LinkedIn > Email > Document)."""

    def test_whatsapp_priority_over_sms(self) -> None:
        # Both intents could match → WhatsApp wins (first in cascade)
        result = detect_rich_content(
            user_message="Rédige un WhatsApp à Marie",
            assistant_text="Salut Marie ! On se voit demain à 10h ?",
        )
        assert result is not None
        assert result["kind"] == "whatsapp_draft"

    def test_sms_detected_when_intent_sms_only(self) -> None:
        result = detect_rich_content(
            user_message="Rédige un SMS à mon père",
            assistant_text="Salut papa, je rentre tard ce soir. À+",
        )
        assert result is not None
        assert result["kind"] == "sms_draft"

    def test_tweet_detected(self) -> None:
        result = detect_rich_content(
            user_message="Rédige un tweet pour ma promotion",
            assistant_text="Heureux d'annoncer ma promotion au poste de Lead Dev ! 🚀 #flutter",
        )
        assert result is not None
        assert result["kind"] == "tweet_draft"

    def test_linkedin_detected(self) -> None:
        result = detect_rich_content(
            user_message="Rédige un post LinkedIn pour annoncer ma promotion",
            assistant_text=(
                "Aujourd'hui je suis fier d'annoncer ma promotion. "
                "Un grand merci à l'équipe. Hâte de continuer cette aventure ! "
                "#flutter #africa"
            ),
        )
        assert result is not None
        assert result["kind"] == "linkedin_post_draft"

    def test_document_detected_with_formal_letter(self) -> None:
        result = detect_rich_content(
            user_message="Rédige une lettre formelle au maire de Yaoundé",
            assistant_text=(
                "Objet : Demande d'acte de naissance\n\n"
                "Madame la Maire de Yaoundé,\n\n"
                "Je soussigné, Loth Ivan Ngassa Yimga, ai l'honneur de solliciter de votre "
                "haute bienveillance la délivrance d'un acte de naissance.\n\n"
                "Né le 1er janvier 1990 à Yaoundé, je joins à la présente les pièces "
                "justificatives habituelles.\n\n"
                "Dans l'attente de votre réponse, je vous prie d'agréer, Madame la Maire, "
                "l'expression de mes salutations distinguées.\n\n"
                "Loth Ivan"
            ),
        )
        assert result is not None
        assert result["kind"] == "document_draft"

    def test_document_detected_with_course_intent(self) -> None:
        body = (
            "# Les boucles for en Python\n\n"
            "Une boucle `for` permet d'itérer.\n\n"
            "## Syntaxe\n\n```python\nfor item in iterable:\n    print(item)\n```\n\n"
            "## Exemples\n\nIteration sur une liste, range, dict...\n\n"
        ) * 5
        result = detect_rich_content(
            user_message="Génère un cours détaillé sur les boucles for en Python",
            assistant_text=body,
        )
        assert result is not None
        assert result["kind"] == "document_draft"

    def test_email_priority_over_document_on_short_email(self) -> None:
        # Email body short (~500 chars) + email intent → email wins, document
        # ne match pas car body trop court pour son seuil 500 chars
        result = detect_rich_content(
            user_message="Rédige un mail de relance",
            assistant_text=(
                "Sujet : Relance livraison\n\n"
                "Bonjour Marie,\n\n"
                "Je me permets de vous relancer.\n\n"
                "Cordialement,\nIvan"
            ),
        )
        assert result is not None
        assert result["kind"] == "email_draft"
