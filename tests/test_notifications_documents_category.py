"""Tests catégorie notification `documents` (C4.12).

Garde-fou critique : sans `documents` dans `CATEGORIES` + `_DEFAULT_CHANNELS`,
`get_channel_for_category('documents')` renverrait `none` → AUCUN push
silencieux quand le worker tente de notifier « 📄 doc prêt ».
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.features.notifications.preferences import (
    CATEGORIES,
    NotificationPreferencesService,
    default_channel_for,
)

pytestmark = pytest.mark.asyncio


def test_documents_in_categories():
    assert "documents" in CATEGORIES


def test_documents_default_channel_is_push():
    """Un doc lourd généré = push temps-réel (pas email)."""
    assert default_channel_for("documents") == "push"


async def test_get_channel_for_documents_returns_push_when_no_row():
    """Sans préférence user posée, le default 'push' est retourné (pas 'none')."""
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = None
    db.execute = AsyncMock(return_value=result)

    channel = await NotificationPreferencesService.get_channel_for_category(
        uuid.uuid4(), "documents", db
    )
    assert channel == "push"


async def test_get_channel_for_documents_respects_user_pref():
    """Si l'user a posé 'none', on respecte (désinscription RGPD possible)."""
    db = MagicMock()
    result = MagicMock()
    result.scalar_one_or_none.return_value = "none"
    db.execute = AsyncMock(return_value=result)

    channel = await NotificationPreferencesService.get_channel_for_category(
        uuid.uuid4(), "documents", db
    )
    assert channel == "none"
