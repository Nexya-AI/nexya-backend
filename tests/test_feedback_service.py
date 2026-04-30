"""N1 — FeedbackService unit tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors.exceptions import ResourceNotFoundException
from app.features.feedback.models import MessageFeedback
from app.features.feedback.schemas import FeedbackCreate
from app.features.feedback.service import FeedbackService


def _mk_user() -> MagicMock:
    user = MagicMock()
    user.id = uuid.uuid4()
    return user


def _mk_message(conv_user_id: uuid.UUID | None = None) -> MagicMock:
    msg = MagicMock()
    msg.id = uuid.uuid4()
    return msg


def _mk_feedback_row(user_id, message_id, rating="like", comment=None):
    row = MagicMock(spec=MessageFeedback)
    row.id = uuid.uuid4()
    row.user_id = user_id
    row.message_id = message_id
    row.rating = rating
    row.comment = comment
    row.created_at = datetime.now(UTC)
    row.updated_at = datetime.now(UTC)
    return row


def _mk_db_with_owner_check_ok(message: MagicMock, upsert_row: MagicMock = None) -> MagicMock:
    """DB mock : 1er execute retourne le message owner-checked,
    2ᵉ execute (UPSERT) retourne la row feedback finale."""
    db = MagicMock()
    owner_result = MagicMock()
    owner_result.scalar_one_or_none = lambda: message
    upsert_result = MagicMock()
    upsert_result.scalar_one = lambda: upsert_row

    side_effects = [owner_result]
    if upsert_row is not None:
        side_effects.append(upsert_result)
    db.execute = AsyncMock(side_effect=side_effects)
    db.commit = AsyncMock()
    return db


@pytest.mark.asyncio
async def test_record_feedback_happy_path():
    user = _mk_user()
    message = _mk_message()
    feedback = _mk_feedback_row(user.id, message.id, rating="like")
    db = _mk_db_with_owner_check_ok(message, feedback)

    body = FeedbackCreate(rating="like", comment=None)
    result = await FeedbackService.record_feedback(user, message.id, body, db)

    assert result.rating == "like"
    assert result.user_id == user.id
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_record_feedback_idempotent_upsert_same_rating():
    """Re-poste même rating → UPSERT tombe sur on_conflict_do_update."""
    user = _mk_user()
    message = _mk_message()
    existing_feedback = _mk_feedback_row(user.id, message.id, rating="like")
    db = _mk_db_with_owner_check_ok(message, existing_feedback)

    body = FeedbackCreate(rating="like")
    result = await FeedbackService.record_feedback(user, message.id, body, db)
    assert result.rating == "like"


@pytest.mark.asyncio
async def test_record_feedback_changes_rating_via_upsert():
    """Change like→dislike : UPSERT met à jour le row existant."""
    user = _mk_user()
    message = _mk_message()
    updated = _mk_feedback_row(user.id, message.id, rating="dislike")
    db = _mk_db_with_owner_check_ok(message, updated)

    body = FeedbackCreate(rating="dislike", comment="Réponse imprécise")
    result = await FeedbackService.record_feedback(user, message.id, body, db)
    assert result.rating == "dislike"


@pytest.mark.asyncio
async def test_record_feedback_404_idor_safe():
    """User pas propriétaire de la conv → 404 (jamais 403)."""
    user = _mk_user()
    message_id = uuid.uuid4()
    db = MagicMock()
    owner_result = MagicMock()
    owner_result.scalar_one_or_none = lambda: None
    db.execute = AsyncMock(return_value=owner_result)
    db.commit = AsyncMock()

    body = FeedbackCreate(rating="like")
    with pytest.raises(ResourceNotFoundException):
        await FeedbackService.record_feedback(user, message_id, body, db)


@pytest.mark.asyncio
async def test_delete_feedback_idempotent_no_row():
    """DELETE sur un message sans feedback → no error, 0 row affected."""
    user = _mk_user()
    message_id = uuid.uuid4()
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    await FeedbackService.delete_feedback(user, message_id, db)
    db.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_feedback_with_existing_row():
    user = _mk_user()
    message_id = uuid.uuid4()
    db = MagicMock()
    db.execute = AsyncMock()
    db.commit = AsyncMock()

    await FeedbackService.delete_feedback(user, message_id, db)
    # SQL DELETE émis avec scope user_id + message_id
    call = db.execute.call_args
    assert call is not None


@pytest.mark.asyncio
async def test_get_for_message_returns_none_if_absent():
    user = _mk_user()
    message_id = uuid.uuid4()
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = lambda: None
    db.execute = AsyncMock(return_value=result)

    out = await FeedbackService.get_for_message(user, message_id, db)
    assert out is None


@pytest.mark.asyncio
async def test_get_for_message_returns_row_if_present():
    user = _mk_user()
    message_id = uuid.uuid4()
    feedback = _mk_feedback_row(user.id, message_id)
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none = lambda: feedback
    db.execute = AsyncMock(return_value=result)

    out = await FeedbackService.get_for_message(user, message_id, db)
    assert out is feedback


def test_feedback_create_validates_comment_length():
    """Pydantic rejet comment > 1000 chars."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FeedbackCreate(rating="like", comment="x" * 1001)


def test_feedback_create_validates_rating_literal():
    """Pydantic rejet rating hors {like, dislike}."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        FeedbackCreate(rating="thumbs_up")  # type: ignore[arg-type]


def test_feedback_create_accepts_none_comment():
    body = FeedbackCreate(rating="like")
    assert body.comment is None


def test_feedback_create_accepts_dislike():
    body = FeedbackCreate(rating="dislike", comment="trop long")
    assert body.rating == "dislike"
    assert body.comment == "trop long"
