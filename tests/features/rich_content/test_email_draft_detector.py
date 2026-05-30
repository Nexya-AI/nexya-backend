"""Tests `email_draft_detector` (C4.4)."""

from __future__ import annotations

import pytest

from app.features.rich_content.email_draft_detector import (
    detect_email_body,
    detect_email_intent,
    detect_rich_content_email,
)


class TestDetectEmailIntent:
    """Intent classifier — message user upstream."""

    @pytest.mark.parametrize(
        "user_message",
        [
            "Rédige un mail à mon fournisseur",
            "rédige un email de relance",
            "écris un email pour Marie",
            "Peux-tu me préparer un mail de remerciement ?",
            "génère un courriel à mon banquier",
            "Tape un email pour confirmer le rendez-vous",
            "rédiger un email à mon professeur",
            "RÉDIGE UN EMAIL",  # case-insensitive
        ],
    )
    def test_fr_intent_detected(self, user_message: str) -> None:
        assert detect_email_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            "Write an email to my supplier",
            "draft an email of apology",
            "compose an email for Marie",
            "Can you prepare an email to confirm the meeting?",
            "create an e-mail to my bank",
            "write a mail to John",
        ],
    )
    def test_en_intent_detected(self, user_message: str) -> None:
        assert detect_email_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            "J'ai reçu un email étrange hier",
            "Comment fonctionne le protocole SMTP ?",
            "Explique-moi ce qu'est un email",
            "Je voudrais savoir si l'email de Marie est arrivé",
            "What is an email?",
            "I got an email yesterday",
            "",
            "   ",
        ],
    )
    def test_no_intent(self, user_message: str) -> None:
        assert detect_email_intent(user_message) is False

    def test_none_input(self) -> None:
        # Defensive — caller might pass None inadvertently
        assert detect_email_intent(None) is False  # type: ignore[arg-type]


class TestDetectEmailBody:
    """Heuristique markers — réponse assistante."""

    def test_full_email_fr_detected(self) -> None:
        text = (
            "Sujet : Relance livraison commande #2847\n\n"
            "Bonjour Monsieur Diallo,\n\n"
            "Je me permets de vous relancer concernant la commande passée "
            "le 15 mai dernier. À ce jour, je n'ai toujours pas reçu de "
            "nouvelles concernant la date de livraison prévue.\n\n"
            "Pourriez-vous me confirmer le statut de cette commande ?\n\n"
            "Cordialement,\nIvan"
        )
        is_email, payload = detect_email_body(text)
        assert is_email is True
        assert payload is not None
        assert payload["subject"] == "Relance livraison commande #2847"
        assert "Bonjour Monsieur Diallo" in payload["body"]
        assert "Cordialement" in payload["body"]
        assert payload["to"] is None

    def test_full_email_en_detected(self) -> None:
        text = (
            "Subject: Order #2847 follow-up\n\n"
            "Dear Mr. Diallo,\n\n"
            "I'm writing to follow up on the order placed on May 15th. "
            "I haven't received any updates regarding the delivery date.\n\n"
            "Could you please confirm the status of this order?\n\n"
            "Best regards,\nIvan"
        )
        is_email, payload = detect_email_body(text)
        assert is_email is True
        assert payload is not None
        assert payload["subject"] == "Order #2847 follow-up"

    def test_email_with_markdown_subject(self) -> None:
        text = (
            "**Sujet :** Demande de congés\n\n"
            "Bonjour Madame Nkamga,\n\n"
            "Je souhaite poser des congés du 1er au 15 juin prochains.\n\n"
            "Cordialement,\nIvan"
        )
        is_email, payload = detect_email_body(text)
        assert is_email is True
        assert payload is not None
        assert payload["subject"] == "Demande de congés"

    def test_email_without_subject_still_detected(self) -> None:
        # greeting + closing → score 4, passes threshold
        text = (
            "Bonjour Madame Nkamga,\n\n"
            "Je vous écris pour vous informer que la réunion de demain "
            "est annulée et reportée à vendredi prochain à 10h.\n\n"
            "Cordialement,\nIvan"
        )
        is_email, payload = detect_email_body(text)
        assert is_email is True
        assert payload is not None
        assert payload["subject"] is None

    def test_text_too_short_not_email(self) -> None:
        is_email, payload = detect_email_body("Bonjour Marie, Cordialement")
        assert is_email is False
        assert payload is None

    def test_random_text_not_email(self) -> None:
        text = (
            "La photosynthèse est un processus biochimique permettant "
            "aux plantes de convertir l'énergie lumineuse en énergie "
            "chimique stockée sous forme de glucose. Ce processus se "
            "déroule dans les chloroplastes et nécessite de l'eau et "
            "du dioxyde de carbone."
        )
        is_email, _ = detect_email_body(text)
        assert is_email is False

    def test_recipe_not_email(self) -> None:
        text = (
            "Pour préparer le ndolé pour 4 personnes :\n"
            "1. Lavez et hachez 500g de feuilles de ndolé.\n"
            "2. Faites revenir l'oignon dans 3 cuillères d'huile.\n"
            "3. Ajoutez la pâte d'arachide et mélangez.\n"
            "4. Servez chaud avec du riz ou du plantain."
        )
        is_email, _ = detect_email_body(text)
        assert is_email is False

    def test_empty_string(self) -> None:
        assert detect_email_body("") == (False, None)

    def test_none_input(self) -> None:
        assert detect_email_body(None) == (False, None)  # type: ignore[arg-type]

    def test_body_capped_at_10k(self) -> None:
        # Build a > 10k chars email with structure
        long_paragraph = "Lorem ipsum dolor sit amet. " * 500  # ~14k chars
        text = (
            "Sujet : Test long body\n\n"
            "Bonjour Marie,\n\n"
            f"{long_paragraph}\n\n"
            "Cordialement,\nIvan"
        )
        is_email, payload = detect_email_body(text)
        assert is_email is True
        assert payload is not None
        assert len(payload["body"]) <= 10_000


class TestDetectRichContentEmail:
    """Point d'entrée — combine intent + body."""

    def test_intent_and_body_match_returns_payload(self) -> None:
        result = detect_rich_content_email(
            user_message="Rédige un mail de relance à mon fournisseur",
            assistant_text=(
                "Sujet : Relance commande #2847\n\n"
                "Bonjour Monsieur Diallo,\n\n"
                "Je me permets de vous relancer.\n\n"
                "Cordialement,\nIvan"
            ),
        )
        assert result is not None
        assert result["kind"] == "email_draft"
        assert result["data"]["subject"] == "Relance commande #2847"
        assert "Bonjour Monsieur Diallo" in result["data"]["body"]
        assert result["data"]["to"] is None

    def test_body_only_with_subject_returns_payload(self) -> None:
        # No clear intent in user message, but body has clear structure
        # including subject → high confidence
        result = detect_rich_content_email(
            user_message="Aide-moi à m'organiser",
            assistant_text=(
                "Sujet : Confirmation rendez-vous\n\n"
                "Bonjour Madame,\n\n"
                "Je vous confirme notre rendez-vous de demain matin à 10h.\n\n"
                "Cordialement,\nIvan"
            ),
        )
        assert result is not None
        assert result["kind"] == "email_draft"

    def test_body_only_without_subject_returns_none(self) -> None:
        # Ambiguous — greeting + closing but no subject. Could be a letter,
        # a speech, a formal text. Don't flag.
        result = detect_rich_content_email(
            user_message="Aide-moi à écrire une lettre formelle",
            assistant_text=(
                "Bonjour Madame Nkamga,\n\n"
                "Je vous écris pour vous informer de la nouvelle politique "
                "de notre établissement concernant les horaires d'ouverture.\n\n"
                "Cordialement,\nIvan"
            ),
        )
        assert result is None

    def test_intent_without_body_returns_none(self) -> None:
        # User asked for email but LLM gave a random response
        result = detect_rich_content_email(
            user_message="Rédige un mail à Marie",
            assistant_text="Pour rédiger un email professionnel, voici quelques conseils.",
        )
        assert result is None

    def test_no_intent_no_body_returns_none(self) -> None:
        result = detect_rich_content_email(
            user_message="Quelle est la capitale du Cameroun ?",
            assistant_text="La capitale du Cameroun est Yaoundé.",
        )
        assert result is None

    def test_empty_inputs_return_none(self) -> None:
        assert detect_rich_content_email("", "") is None
        assert detect_rich_content_email("Rédige un mail", "") is None
