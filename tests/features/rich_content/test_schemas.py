"""Tests `RichContentPayload` Pydantic schemas (C4.4 + C4.5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.features.rich_content.schemas import (
    DocumentDraftData,
    EmailDraftData,
    LinkedInPostDraftData,
    RichContentPayload,
    SmsDraftData,
    TweetDraftData,
    WhatsAppDraftData,
)


class TestEmailDraftData:
    def test_minimal_payload(self) -> None:
        data = EmailDraftData(body="Bonjour, ceci est un email.")
        assert data.subject is None
        assert data.to is None
        assert data.body == "Bonjour, ceci est un email."

    def test_full_payload(self) -> None:
        data = EmailDraftData(
            subject="Relance livraison",
            body="Bonjour Marie, ...",
            to="marie@example.com",
        )
        assert data.subject == "Relance livraison"
        assert data.to == "marie@example.com"

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EmailDraftData(body="")

    def test_body_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EmailDraftData(body="x" * 10_001)

    def test_subject_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EmailDraftData(subject="x" * 301, body="hi")

    def test_to_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EmailDraftData(to="x" * 321, body="hi")

    def test_subject_whitespace_normalized_to_none(self) -> None:
        data = EmailDraftData(subject="   ", body="hi")
        assert data.subject is None

    def test_to_whitespace_normalized_to_none(self) -> None:
        data = EmailDraftData(to="   ", body="hi")
        assert data.to is None


class TestWhatsAppDraftData:
    def test_minimal_payload(self) -> None:
        data = WhatsAppDraftData(body="Salut Marie !")
        assert data.phone is None
        assert data.body == "Salut Marie !"

    def test_full_payload(self) -> None:
        data = WhatsAppDraftData(phone="+237698765432", body="Salut !")
        assert data.phone == "+237698765432"

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WhatsAppDraftData(body="")

    def test_body_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WhatsAppDraftData(body="x" * 10_001)

    def test_phone_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WhatsAppDraftData(phone="x" * 21, body="hi")

    def test_phone_whitespace_normalized_to_none(self) -> None:
        data = WhatsAppDraftData(phone="   ", body="hi")
        assert data.phone is None


class TestRichContentPayload:
    def test_email_factory(self) -> None:
        payload = RichContentPayload.email(
            subject="Test",
            body="Hello",
            to="test@example.com",
        )
        assert payload.kind == "email_draft"
        assert payload.data["subject"] == "Test"
        assert payload.data["body"] == "Hello"
        assert payload.data["to"] == "test@example.com"

    def test_email_factory_minimal(self) -> None:
        payload = RichContentPayload.email(subject=None, body="Hello")
        assert payload.kind == "email_draft"
        assert payload.data["subject"] is None
        assert payload.data["to"] is None

    def test_whatsapp_factory(self) -> None:
        payload = RichContentPayload.whatsapp(phone="+237698765432", body="Hi")
        assert payload.kind == "whatsapp_draft"
        assert payload.data["phone"] == "+237698765432"
        assert payload.data["body"] == "Hi"

    def test_whatsapp_factory_minimal(self) -> None:
        payload = RichContentPayload.whatsapp(phone=None, body="Hi")
        assert payload.kind == "whatsapp_draft"
        assert payload.data["phone"] is None

    def test_email_factory_validates_caps(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.email(subject=None, body="x" * 10_001)

    def test_whatsapp_factory_validates_caps(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.whatsapp(phone=None, body="x" * 10_001)

    def test_model_dump_serialization(self) -> None:
        payload = RichContentPayload.email(subject="Hi", body="Hello")
        dumped = payload.model_dump()
        assert dumped["kind"] == "email_draft"
        assert dumped["data"]["subject"] == "Hi"
        assert dumped["data"]["body"] == "Hello"


# ──────────────────────────────────────────────────────────────────────
# C4.5 — Nouveaux schémas (SMS / LinkedIn / Tweet / Document)
# ──────────────────────────────────────────────────────────────────────


class TestSmsDraftData:
    def test_minimal_payload(self) -> None:
        data = SmsDraftData(body="Salut Marie !")
        assert data.phone is None
        assert data.body == "Salut Marie !"

    def test_full_payload(self) -> None:
        data = SmsDraftData(phone="+237698765432", body="Salut !")
        assert data.phone == "+237698765432"

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SmsDraftData(body="")

    def test_body_too_long_rejected(self) -> None:
        # SMS cap = 1600 chars
        with pytest.raises(ValidationError):
            SmsDraftData(body="x" * 1_601)

    def test_body_at_cap_accepted(self) -> None:
        data = SmsDraftData(body="x" * 1_600)
        assert len(data.body) == 1_600

    def test_phone_whitespace_normalized_to_none(self) -> None:
        data = SmsDraftData(phone="   ", body="hi")
        assert data.phone is None


class TestLinkedInPostDraftData:
    def test_minimal_payload(self) -> None:
        data = LinkedInPostDraftData(body="Hello LinkedIn !")
        assert data.body == "Hello LinkedIn !"

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LinkedInPostDraftData(body="")

    def test_body_too_long_rejected(self) -> None:
        # LinkedIn cap = 3000 chars
        with pytest.raises(ValidationError):
            LinkedInPostDraftData(body="x" * 3_001)

    def test_body_at_cap_accepted(self) -> None:
        data = LinkedInPostDraftData(body="x" * 3_000)
        assert len(data.body) == 3_000


class TestTweetDraftData:
    def test_minimal_payload(self) -> None:
        data = TweetDraftData(body="Hello world !")
        assert data.body == "Hello world !"

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TweetDraftData(body="")

    def test_body_too_long_rejected(self) -> None:
        # Tweet cap = 280 chars (limite officielle Twitter/X)
        with pytest.raises(ValidationError):
            TweetDraftData(body="x" * 281)

    def test_body_at_cap_accepted(self) -> None:
        data = TweetDraftData(body="x" * 280)
        assert len(data.body) == 280


class TestDocumentDraftData:
    def test_minimal_payload(self) -> None:
        data = DocumentDraftData(body="Contenu du document.")
        assert data.title is None
        assert data.recipient is None
        assert data.body == "Contenu du document."

    def test_full_payload(self) -> None:
        data = DocumentDraftData(
            title="Demande d'acte de naissance",
            body="Madame la Maire, ...",
            recipient="Madame la Maire de Yaoundé",
        )
        assert data.title == "Demande d'acte de naissance"
        assert data.recipient == "Madame la Maire de Yaoundé"

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DocumentDraftData(body="")

    def test_body_too_long_rejected(self) -> None:
        # Document cap = 50000 chars
        with pytest.raises(ValidationError):
            DocumentDraftData(body="x" * 50_001)

    def test_body_at_cap_accepted(self) -> None:
        data = DocumentDraftData(body="x" * 50_000)
        assert len(data.body) == 50_000

    def test_title_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DocumentDraftData(title="x" * 301, body="hi")

    def test_recipient_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DocumentDraftData(recipient="x" * 201, body="hi")

    def test_title_whitespace_normalized_to_none(self) -> None:
        data = DocumentDraftData(title="   ", body="hi")
        assert data.title is None

    def test_recipient_whitespace_normalized_to_none(self) -> None:
        data = DocumentDraftData(recipient="   ", body="hi")
        assert data.recipient is None


class TestRichContentPayloadFactoriesC45:
    """Factories C4.5 (sms / linkedin_post / tweet / document)."""

    def test_sms_factory(self) -> None:
        payload = RichContentPayload.sms(phone="+237698765432", body="Hi")
        assert payload.kind == "sms_draft"
        assert payload.data["phone"] == "+237698765432"
        assert payload.data["body"] == "Hi"

    def test_sms_factory_minimal(self) -> None:
        payload = RichContentPayload.sms(phone=None, body="Hi")
        assert payload.kind == "sms_draft"
        assert payload.data["phone"] is None

    def test_linkedin_factory(self) -> None:
        payload = RichContentPayload.linkedin_post(body="Hello LinkedIn !")
        assert payload.kind == "linkedin_post_draft"
        assert payload.data["body"] == "Hello LinkedIn !"

    def test_tweet_factory(self) -> None:
        payload = RichContentPayload.tweet(body="Hello world !")
        assert payload.kind == "tweet_draft"
        assert payload.data["body"] == "Hello world !"

    def test_document_factory(self) -> None:
        payload = RichContentPayload.document(
            title="Demande d'acte",
            body="Madame la Maire, ...",
            recipient="Madame la Maire",
        )
        assert payload.kind == "document_draft"
        assert payload.data["title"] == "Demande d'acte"
        assert payload.data["recipient"] == "Madame la Maire"
        assert payload.data["body"] == "Madame la Maire, ..."

    def test_document_factory_minimal(self) -> None:
        payload = RichContentPayload.document(title=None, body="Contenu")
        assert payload.kind == "document_draft"
        assert payload.data["title"] is None
        assert payload.data["recipient"] is None

    def test_sms_factory_validates_caps(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.sms(phone=None, body="x" * 1_601)

    def test_linkedin_factory_validates_caps(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.linkedin_post(body="x" * 3_001)

    def test_tweet_factory_validates_caps(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.tweet(body="x" * 281)

    def test_document_factory_validates_caps(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.document(title=None, body="x" * 50_001)
