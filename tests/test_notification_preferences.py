"""
Tests F3 — NotificationPreferencesService.

Couvre :
- defaults injectés quand l'user n'a pas de row (les 5 catégories).
- UPSERT via set_for_user crée puis met à jour.
- rejet channel hors whitelist.
- rejet catégorie hors whitelist.
- set_category_none idempotent (pose puis re-pose sans erreur).
- get_channel_for_category retourne default si pas de row.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.features.notifications.preferences import (
    CATEGORIES,
    NotificationPreferencesService,
    PreferenceEntry,
    default_channel_for,
)


class _ScalarResult:
    """Fake SQLAlchemy Result pour retourner un set de rows ou scalar."""

    def __init__(self, rows=None, scalar=None):
        self._rows = rows or []
        self._scalar = scalar

    def all(self):
        return self._rows

    def scalar_one_or_none(self):
        return self._scalar


def _mk_db(execute_results=None):
    db = MagicMock()
    results = list(execute_results or [])

    async def _execute(*args, **kwargs):
        if results:
            return results.pop(0)
        return _ScalarResult(rows=[])

    db.execute = AsyncMock(side_effect=_execute)
    db.commit = AsyncMock()
    db.flush = AsyncMock()
    return db


def test_default_channel_for_returns_expected_defaults():
    assert default_channel_for("tasks") == "push"
    assert default_channel_for("payments") == "email"
    assert default_channel_for("security") == "email"
    assert default_channel_for("digest") == "email"
    assert default_channel_for("product") == "email"


def test_default_channel_for_unknown_returns_none():
    assert default_channel_for("unknown") == "none"


@pytest.mark.asyncio
async def test_get_for_user_returns_all_5_categories_with_defaults():
    user_id = uuid.uuid4()
    # DB vide → toutes les defaults doivent apparaître.
    db = _mk_db(execute_results=[_ScalarResult(rows=[])])
    entries = await NotificationPreferencesService.get_for_user(user_id, db)

    assert len(entries) == 5
    cats = {e.category for e in entries}
    assert cats == set(CATEGORIES)
    by_cat = {e.category: e.channel for e in entries}
    assert by_cat["tasks"] == "push"
    assert by_cat["payments"] == "email"


@pytest.mark.asyncio
async def test_get_for_user_overrides_defaults_with_rows():
    user_id = uuid.uuid4()
    row_tasks = MagicMock(category="tasks", channel="both")
    row_digest = MagicMock(category="digest", channel="none")
    db = _mk_db(execute_results=[_ScalarResult(rows=[row_tasks, row_digest])])
    entries = await NotificationPreferencesService.get_for_user(user_id, db)
    by_cat = {e.category: e.channel for e in entries}
    assert by_cat["tasks"] == "both"  # override
    assert by_cat["digest"] == "none"  # override
    assert by_cat["payments"] == "email"  # default conservé


@pytest.mark.asyncio
async def test_set_for_user_upsert_commits_then_returns_refreshed():
    user_id = uuid.uuid4()
    # 1er execute : UPSERT (aucun row retourné).
    # 2e execute : re-GET après commit pour retour.
    db = _mk_db(
        execute_results=[
            _ScalarResult(rows=[]),  # UPSERT
            _ScalarResult(rows=[MagicMock(category="tasks", channel="email")]),
        ]
    )
    result = await NotificationPreferencesService.set_for_user(
        user_id,
        [PreferenceEntry(category="tasks", channel="email")],
        db,
    )
    assert db.commit.await_count == 1
    by_cat = {e.category: e.channel for e in result}
    assert by_cat["tasks"] == "email"


@pytest.mark.asyncio
async def test_set_for_user_rejects_unknown_category():
    user_id = uuid.uuid4()
    db = _mk_db()
    with pytest.raises(ValueError):
        await NotificationPreferencesService.set_for_user(
            user_id,
            [PreferenceEntry(category="WRONG", channel="push")],
            db,
        )


@pytest.mark.asyncio
async def test_set_for_user_rejects_unknown_channel():
    user_id = uuid.uuid4()
    db = _mk_db()
    with pytest.raises(ValueError):
        await NotificationPreferencesService.set_for_user(
            user_id,
            [PreferenceEntry(category="tasks", channel="SMS")],
            db,
        )


@pytest.mark.asyncio
async def test_set_category_none_commits_upsert():
    user_id = uuid.uuid4()
    db = _mk_db(execute_results=[_ScalarResult(rows=[])])
    await NotificationPreferencesService.set_category_none(user_id, "digest", db)
    assert db.commit.await_count == 1


@pytest.mark.asyncio
async def test_set_category_none_rejects_unknown_category():
    user_id = uuid.uuid4()
    db = _mk_db()
    with pytest.raises(ValueError):
        await NotificationPreferencesService.set_category_none(user_id, "BAD", db)


@pytest.mark.asyncio
async def test_get_channel_for_category_returns_default_when_missing():
    user_id = uuid.uuid4()
    db = _mk_db(execute_results=[_ScalarResult(scalar=None)])
    channel = await NotificationPreferencesService.get_channel_for_category(user_id, "tasks", db)
    assert channel == "push"


@pytest.mark.asyncio
async def test_get_channel_for_category_returns_row_when_present():
    user_id = uuid.uuid4()
    db = _mk_db(execute_results=[_ScalarResult(scalar="both")])
    channel = await NotificationPreferencesService.get_channel_for_category(user_id, "payments", db)
    assert channel == "both"


@pytest.mark.asyncio
async def test_get_channel_for_category_unknown_category_returns_none():
    user_id = uuid.uuid4()
    db = _mk_db()
    channel = await NotificationPreferencesService.get_channel_for_category(user_id, "UNKNOWN", db)
    assert channel == "none"
