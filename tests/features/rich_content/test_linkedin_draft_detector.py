"""Tests `linkedin_draft_detector` (C4.5)."""

from __future__ import annotations

import pytest

from app.features.rich_content.linkedin_draft_detector import (
    detect_linkedin_intent,
    detect_rich_content_linkedin,
)


class TestDetectLinkedInIntent:
    """Intent classifier — message user upstream."""

    @pytest.mark.parametrize(
        "user_message",
        [
            "Rédige un post LinkedIn pour annoncer ma promotion",
            "écris une publication LinkedIn",
            "Crée un post LinkedIn de remerciement",
            "Publie un post LinkedIn pour célébrer 5 ans",
            "post LinkedIn pour annoncer ma nouvelle entreprise",
            "RÉDIGE UN POST LINKEDIN",
        ],
    )
    def test_fr_intent_detected(self, user_message: str) -> None:
        assert detect_linkedin_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            "Write a LinkedIn post about my new role",
            "draft a LinkedIn article",
            "compose a LinkedIn post",
            "LinkedIn post to celebrate",
            "create a LinkedIn update",
        ],
    )
    def test_en_intent_detected(self, user_message: str) -> None:
        assert detect_linkedin_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            "Comment fonctionne LinkedIn ?",
            "What is LinkedIn?",
            "J'ai un compte LinkedIn depuis 2020",
            "I have many LinkedIn connections",
            "Comment supprimer mon compte LinkedIn ?",
            "",
            "   ",
        ],
    )
    def test_no_intent(self, user_message: str) -> None:
        assert detect_linkedin_intent(user_message) is False

    def test_none_input(self) -> None:
        assert detect_linkedin_intent(None) is False  # type: ignore[arg-type]


class TestDetectRichContentLinkedIn:
    """Point d'entrée — détection intent-based exclusive."""

    def test_intent_with_body_returns_payload(self) -> None:
        result = detect_rich_content_linkedin(
            user_message="Rédige un post LinkedIn pour annoncer ma promotion",
            assistant_text=(
                "Aujourd'hui je suis fier d'annoncer ma promotion au poste de Lead Developer "
                "chez NexyaLabs.\n\nUn grand merci à toute l'équipe pour la confiance.\n\n"
                "Hâte de continuer cette belle aventure !\n\n#flutter #africa #leadership"
            ),
        )
        assert result is not None
        assert result["kind"] == "linkedin_post_draft"
        assert "promotion" in result["data"]["body"]

    def test_no_intent_returns_none(self) -> None:
        result = detect_rich_content_linkedin(
            user_message="Comment fonctionne LinkedIn ?",
            assistant_text="LinkedIn est un réseau social professionnel...",
        )
        assert result is None

    def test_intent_with_short_body_returns_none(self) -> None:
        result = detect_rich_content_linkedin(
            user_message="Rédige un post LinkedIn",
            assistant_text="Hi",
        )
        assert result is None

    def test_long_body_capped_at_3000(self) -> None:
        long_body = (
            "Aujourd'hui je suis fier d'annoncer ma promotion. " + ("Lorem ipsum. " * 300)
        )
        result = detect_rich_content_linkedin(
            user_message="Rédige un post LinkedIn",
            assistant_text=long_body,
        )
        assert result is not None
        assert len(result["data"]["body"]) <= 3_000

    def test_empty_inputs_return_none(self) -> None:
        assert detect_rich_content_linkedin("", "") is None
        assert detect_rich_content_linkedin("Rédige un post LinkedIn", "") is None
