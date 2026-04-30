"""
Tests F3 — JWT unsubscribe tokens (RS256, purpose=email_unsubscribe, TTL 365j).

Couvre :
- round-trip encode/decode avec claims attendus
- TTL long (365j par défaut) respecté dans exp
- wrong purpose → UnsubscribeTokenInvalidException
- token expiré → UnsubscribeTokenExpiredException
- token malformé (signature ko) → UnsubscribeTokenInvalidException
- catégorie hors whitelist dans le payload → Invalid
- claim `cat` manquant → Invalid
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import jwt
import pytest

from app.config import settings
from app.core.auth.unsubscribe_tokens import (
    ALGORITHM,
    TOKEN_PURPOSE_UNSUBSCRIBE,
    create_unsubscribe_token,
    decode_unsubscribe_token,
)
from app.core.errors.exceptions import (
    UnsubscribeTokenExpiredException,
    UnsubscribeTokenInvalidException,
)


def test_unsubscribe_token_roundtrip_carries_claims():
    user_id = uuid.uuid4()
    token = create_unsubscribe_token(user_id, "tasks")
    payload = decode_unsubscribe_token(token)

    assert payload["sub"] == str(user_id)
    assert payload["cat"] == "tasks"
    assert payload["purpose"] == TOKEN_PURPOSE_UNSUBSCRIBE
    assert "jti" in payload
    assert "iat" in payload
    assert "exp" in payload


def test_unsubscribe_token_ttl_respects_settings(monkeypatch):
    # Simule un TTL configuré à 10 jours (au lieu du défaut 365).
    monkeypatch.setattr(settings, "notification_unsubscribe_token_ttl_days", 10)
    user_id = uuid.uuid4()
    token = create_unsubscribe_token(user_id, "product")
    payload = decode_unsubscribe_token(token)
    # exp - iat ≈ 10 jours (±1 minute de tolérance)
    delta = int(payload["exp"]) - int(payload["iat"])
    expected = int(timedelta(days=10).total_seconds())
    assert abs(delta - expected) < 60


def test_unsubscribe_token_wrong_purpose_raises_invalid():
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid.uuid4()),
        "purpose": "password_reset",  # mauvais purpose
        "cat": "tasks",
        "iat": now,
        "exp": now + timedelta(days=30),
        "jti": str(uuid.uuid4()),
    }
    forged = jwt.encode(payload, settings.jwt_private_key, algorithm=ALGORITHM)
    with pytest.raises(UnsubscribeTokenInvalidException):
        decode_unsubscribe_token(forged)


def test_unsubscribe_token_expired_raises_expired():
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid.uuid4()),
        "purpose": TOKEN_PURPOSE_UNSUBSCRIBE,
        "cat": "product",
        "iat": now - timedelta(days=400),
        "exp": now - timedelta(days=1),  # expiré hier
        "jti": str(uuid.uuid4()),
    }
    expired = jwt.encode(payload, settings.jwt_private_key, algorithm=ALGORITHM)
    with pytest.raises(UnsubscribeTokenExpiredException):
        decode_unsubscribe_token(expired)


def test_unsubscribe_token_malformed_raises_invalid():
    with pytest.raises(UnsubscribeTokenInvalidException):
        decode_unsubscribe_token("garbage.not.a.valid.jwt")


def test_unsubscribe_token_category_out_of_whitelist_raises_invalid():
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid.uuid4()),
        "purpose": TOKEN_PURPOSE_UNSUBSCRIBE,
        "cat": "NEWSLETTER_SPAM",  # hors whitelist
        "iat": now,
        "exp": now + timedelta(days=30),
        "jti": str(uuid.uuid4()),
    }
    forged = jwt.encode(payload, settings.jwt_private_key, algorithm=ALGORITHM)
    with pytest.raises(UnsubscribeTokenInvalidException):
        decode_unsubscribe_token(forged)


def test_unsubscribe_token_missing_cat_raises_invalid():
    now = datetime.now(UTC)
    payload = {
        "sub": str(uuid.uuid4()),
        "purpose": TOKEN_PURPOSE_UNSUBSCRIBE,
        # cat absent
        "iat": now,
        "exp": now + timedelta(days=30),
        "jti": str(uuid.uuid4()),
    }
    forged = jwt.encode(payload, settings.jwt_private_key, algorithm=ALGORITHM)
    with pytest.raises(UnsubscribeTokenInvalidException):
        decode_unsubscribe_token(forged)


def test_unsubscribe_token_missing_sub_raises_invalid():
    now = datetime.now(UTC)
    payload = {
        # sub absent
        "purpose": TOKEN_PURPOSE_UNSUBSCRIBE,
        "cat": "tasks",
        "iat": now,
        "exp": now + timedelta(days=30),
        "jti": str(uuid.uuid4()),
    }
    forged = jwt.encode(payload, settings.jwt_private_key, algorithm=ALGORITHM)
    with pytest.raises(UnsubscribeTokenInvalidException):
        decode_unsubscribe_token(forged)
