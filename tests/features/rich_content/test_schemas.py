"""Tests `RichContentPayload` Pydantic schemas (C4.4)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.features.rich_content.schemas import (
    EmailDraftData,
    RichContentPayload,
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
