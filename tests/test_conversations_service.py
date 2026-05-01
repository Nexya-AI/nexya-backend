"""
Tests unitaires — ConversationService.

Quatre invariants critiques du Lot 2, testables sans Postgres :

1. Le curseur opaque fait un aller-retour exact (encode → decode = identité).
2. Un curseur forgé ou altéré lève `ValidationException` (422).
3. `_get_owned_conversation` rend la conv quand le user match.
4. `_get_owned_conversation` lève `ResourceNotFoundException` (404, jamais 403)
   quand le user ne match pas — seul rempart IDOR du service.

Les tests d'intégration avec Postgres réel (pagination, filtres, bump counters
en charge) viendront dans le Lot 3 quand la suite DB sera provisionnée.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors.exceptions import (
    ResourceNotFoundException,
    ValidationException,
)
from app.features.chat.models import Conversation
from app.features.chat.service import (
    ConversationService,
    _decode_cursor,
    _encode_cursor,
)

# ══════════════════════════════════════════════════════════════
# 1. Curseur — round-trip exact
# ══════════════════════════════════════════════════════════════


def test_cursor_round_trip_preserves_timestamp_and_id() -> None:
    """Encode puis décode : les deux valeurs doivent ressortir intactes.

    Vérifie qu'un client peut stocker un curseur, le renvoyer N minutes
    plus tard, et le service comprend exactement la même position.
    """
    sort_ts = datetime(2026, 4, 21, 14, 32, 15, 123456, tzinfo=UTC)
    row_id = uuid.UUID("9b8c0d6e-51f4-4a43-8d2a-01e2c0a7b612")

    cursor = _encode_cursor(sort_ts, row_id)
    decoded_ts, decoded_id = _decode_cursor(cursor)

    assert decoded_ts == sort_ts
    assert decoded_id == row_id


# ══════════════════════════════════════════════════════════════
# 2. Curseur — malformé → ValidationException (pas 500)
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "bad_cursor",
    [
        "not-base64!",  # base64 cassé
        "aGVsbG8=",  # base64 valide mais sans séparateur
        "MjAyNi0wNC0yMXwxMjM=",  # séparateur OK mais UUID absent
        "fHgteHw=",  # deux séparateurs, rien dedans
    ],
)
def test_malformed_cursor_raises_validation_exception(bad_cursor: str) -> None:
    """Un curseur cassé ne doit jamais faire crasher le handler en 500.

    `ValidationException` sera transformée par le handler global en 422
    `VALIDATION_ERROR` — message utilisateur propre, pas de stack trace.
    """
    with pytest.raises(ValidationException):
        _decode_cursor(bad_cursor)


# ══════════════════════════════════════════════════════════════
# 3. Isolation cross-user — owner → renvoie la conversation
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_owned_conversation_returns_conversation_for_owner() -> None:
    """Le helper rend la conv quand le user_id match — cas nominal."""
    owner_id = uuid.uuid4()
    conv_id = uuid.uuid4()
    # On fabrique une instance ORM en mémoire (aucun accès DB).
    conversation = Conversation(
        id=conv_id,
        user_id=owner_id,
        expert_id="general",
    )

    result_mock = MagicMock()
    result_mock.scalar_one_or_none = MagicMock(return_value=conversation)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result_mock)

    returned = await ConversationService._get_owned_conversation(conv_id, owner_id, db)

    assert returned is conversation
    db.execute.assert_awaited_once()


# ══════════════════════════════════════════════════════════════
# 4. Isolation cross-user — non-owner → 404 (jamais 403)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_owned_conversation_raises_not_found_for_non_owner() -> None:
    """Un user qui tape un conv_id d'un autre user reçoit 404 — pas 403.

    Le SQL filtre sur `user_id = :current_user AND deleted_at IS NULL` :
    si la conv existe mais appartient à un autre user, le `scalar_one_or_none`
    revient à `None`. Le service lève alors `ResourceNotFoundException` —
    impossible de distinguer côté client « n'existe pas » de « pas à vous ».
    """
    intruder_id = uuid.uuid4()
    conv_id = uuid.uuid4()

    result_mock = MagicMock()
    result_mock.scalar_one_or_none = MagicMock(return_value=None)
    db = MagicMock()
    db.execute = AsyncMock(return_value=result_mock)

    with pytest.raises(ResourceNotFoundException) as excinfo:
        await ConversationService._get_owned_conversation(conv_id, intruder_id, db)

    assert excinfo.value.code == "RESOURCE_NOT_FOUND"
    assert excinfo.value.status_code == 404


# ══════════════════════════════════════════════════════════════
# 5. C1 — `list_for_user` avec param `q` : forme SQL attendue
# ══════════════════════════════════════════════════════════════
#
# On ne démarre pas Postgres ici. On capture la statement SQLAlchemy passée
# à `db.execute` et on inspecte sa compilation littérale pour vérifier :
#
# - quand `q` est posé, le WHERE contient l'opérateur FTS `@@` +
#   `plainto_tsquery('french', …)` (côté messages) et un `ILIKE` (côté titre
#   pour le trigram) reliés par un `OR`.
# - quand `q` est absent (None ou whitespace-only), AUCUNE de ces clauses
#   n'apparaît — la requête reste identique à la version pré-C1 pour ne pas
#   régresser le plan d'exécution des listes sans recherche.


def _compiled_sql(stmt) -> str:
    """Renvoie la statement compilée avec literal binds, tout en minuscules,
    pour grep insensible à la casse."""
    return str(stmt.compile(compile_kwargs={"literal_binds": True})).lower()


@pytest.mark.asyncio
async def test_list_for_user_with_q_injects_fts_and_trigram_clauses() -> None:
    captured: dict[str, object] = {}

    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    db = MagicMock()

    async def _capture_execute(stmt, *args, **kwargs):
        captured["stmt"] = stmt
        return result_mock

    db.execute = _capture_execute

    user = MagicMock()
    user.id = uuid.uuid4()

    await ConversationService.list_for_user(user, db, q="postgres migration")

    sql = _compiled_sql(captured["stmt"])
    # Trigram côté titre
    assert "ilike" in sql or "like" in sql
    # FTS côté messages
    assert "plainto_tsquery" in sql
    assert "search_vector" in sql
    assert "@@" in sql
    # Rattachement via OR + EXISTS
    assert "exists" in sql
    assert " or " in sql


@pytest.mark.asyncio
async def test_list_for_user_without_q_does_not_inject_fts_clauses() -> None:
    captured: dict[str, object] = {}

    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    db = MagicMock()

    async def _capture_execute(stmt, *args, **kwargs):
        captured["stmt"] = stmt
        return result_mock

    db.execute = _capture_execute

    user = MagicMock()
    user.id = uuid.uuid4()

    await ConversationService.list_for_user(user, db, q=None)

    sql = _compiled_sql(captured["stmt"])
    assert "plainto_tsquery" not in sql
    assert "search_vector" not in sql


@pytest.mark.asyncio
async def test_list_for_user_with_whitespace_only_q_skips_fts() -> None:
    """Un `q='   '` strippé donne `""` — on ne filtre pas (équivalent à None)."""
    captured: dict[str, object] = {}

    result_mock = MagicMock()
    result_mock.scalars.return_value.all.return_value = []
    db = MagicMock()

    async def _capture_execute(stmt, *args, **kwargs):
        captured["stmt"] = stmt
        return result_mock

    db.execute = _capture_execute

    user = MagicMock()
    user.id = uuid.uuid4()

    await ConversationService.list_for_user(user, db, q="   ")

    sql = _compiled_sql(captured["stmt"])
    assert "plainto_tsquery" not in sql
    assert "search_vector" not in sql
