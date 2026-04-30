"""
Tests d'intégration — worker `extract_durable_facts` (Session D2).

On monkey-patche `AsyncSessionLocal`, `_call_llm_for_facts` et
`MemoryStore.add` pour isoler le worker de toute I/O réelle. On vérifie :
- skip early sur conv manquante / deleted / already_extracted / not_enough_messages / user_missing,
- happy path : LLM → parser → insertions + sentinelle posée,
- fail-safe LLM → skip 'llm_failed', sentinelle NON posée,
- fail-safe MemoryStore.add → continue boucle + sentinelle posée,
- filtre sensibilité → fait skippé, autres passent,
- JSON cassé → 0 faits, sentinelle posée.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.core.errors.exceptions import MemoryQuotaExceededException
from app.features.chat.models import Conversation, Message
from app.features.memory.service import MemoryStore
from workers import memory_tasks

# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════


_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
_CONV_ID = uuid.UUID("aaaaaaaa-0000-4000-8000-000000000001")


def _make_conversation(
    *,
    deleted: bool = False,
    already_extracted: bool = False,
    message_count: int = 10,
) -> Conversation:
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    conv = Conversation(user_id=_USER_ID, expert_id="general")
    conv.id = _CONV_ID
    conv.created_at = now
    conv.updated_at = now
    conv.deleted_at = now if deleted else None
    conv.memory_extracted_at = now if already_extracted else None
    conv.message_count = message_count
    conv.is_archived = False
    conv.is_favorite = False
    conv.title_generated_at = None
    conv.title = None
    conv.last_message_at = now
    return conv


def _make_messages(n: int) -> list[Message]:
    now = datetime(2026, 4, 24, 12, 0, 0, tzinfo=UTC)
    msgs: list[Message] = []
    for i in range(n):
        m = Message(
            conversation_id=_CONV_ID,
            role="user" if i % 2 == 0 else "assistant",
            content=f"message {i}",
            status="completed",
        )
        m.id = uuid.uuid4()
        m.created_at = now
        m.updated_at = now
        m.deleted_at = None
        msgs.append(m)
    return msgs


def _make_user() -> Any:
    user = MagicMock()
    user.id = _USER_ID
    user.is_pro = False
    return user


class _FakeDB:
    """Fake AsyncSession avec hook configurable par type de statement."""

    def __init__(
        self,
        *,
        conversation: Conversation | None,
        messages: list[Message],
        user: Any | None,
    ) -> None:
        self._conversation = conversation
        self._messages = messages
        self._user = user
        self._executed_updates: list[Any] = []
        self.commit = AsyncMock()
        self.refresh = AsyncMock()

    async def get(self, model, key):
        if model is Conversation:
            return self._conversation
        return None

    async def execute(self, stmt, *args, **kwargs):
        """Route selon le type de requête.

        - SELECT Message → retourne messages pré-configurés.
        - SELECT User   → retourne user pré-configuré (scalar_one_or_none).
        - UPDATE conv   → capture pour vérif post-test.
        - Autres        → ScalarMock générique.
        """
        sql = str(stmt).lower()
        if "update conversations" in sql:
            self._executed_updates.append(stmt)
            return MagicMock()
        if "from users" in sql:
            result = MagicMock()
            result.scalar_one_or_none.return_value = self._user
            return result
        if "from messages" in sql:
            result = MagicMock()
            scalars = MagicMock()
            scalars.all.return_value = list(self._messages)
            result.scalars.return_value = scalars
            return result
        # Fallback générique pour MemoryStore.add (count, INSERT, etc.)
        result = MagicMock()
        result.scalar_one.return_value = 0
        result.scalar_one_or_none.return_value = None
        scalars = MagicMock()
        scalars.all.return_value = []
        result.scalars.return_value = scalars
        return result


@pytest.fixture
def patch_db(monkeypatch: pytest.MonkeyPatch):
    """Context manager qui remplace `AsyncSessionLocal` par un fake."""

    def _patch(*, conversation, messages, user):
        db = _FakeDB(conversation=conversation, messages=messages, user=user)

        class _LocalSession:
            async def __aenter__(self):
                return db

            async def __aexit__(self, *args):
                return False

        def _factory():
            return _LocalSession()

        monkeypatch.setattr(memory_tasks, "AsyncSessionLocal", _factory)
        return db

    return _patch


# ══════════════════════════════════════════════════════════════
# 1. Short-circuits (skip early)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_skip_if_conversation_missing(patch_db) -> None:
    patch_db(conversation=None, messages=[], user=None)
    result = await memory_tasks.extract_durable_facts({}, str(_CONV_ID))
    assert result == {"skipped": True, "reason": "missing"}


@pytest.mark.asyncio
async def test_worker_skip_if_conversation_deleted(patch_db) -> None:
    conv = _make_conversation(deleted=True)
    patch_db(conversation=conv, messages=[], user=None)
    result = await memory_tasks.extract_durable_facts({}, str(_CONV_ID))
    assert result == {"skipped": True, "reason": "deleted"}


@pytest.mark.asyncio
async def test_worker_skip_if_already_extracted(patch_db) -> None:
    conv = _make_conversation(already_extracted=True)
    patch_db(conversation=conv, messages=[], user=None)
    result = await memory_tasks.extract_durable_facts({}, str(_CONV_ID))
    assert result == {"skipped": True, "reason": "already_extracted"}


@pytest.mark.asyncio
async def test_worker_skip_if_not_enough_messages(patch_db) -> None:
    """Moins de EXTRACTION_MIN_MESSAGES → skip ET sentinelle NON posée."""
    conv = _make_conversation()
    db = patch_db(
        conversation=conv,
        messages=_make_messages(3),  # < 6
        user=_make_user(),
    )
    result = await memory_tasks.extract_durable_facts({}, str(_CONV_ID))
    assert result == {"skipped": True, "reason": "not_enough_messages"}
    # Sentinelle NON posée — on veut permettre re-enqueue plus tard.
    assert not db._executed_updates


@pytest.mark.asyncio
async def test_worker_skip_if_user_missing(patch_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """User purgé RGPD entre la conv et le worker → skip 'user_missing'."""
    conv = _make_conversation()
    patch_db(
        conversation=conv,
        messages=_make_messages(10),
        user=None,
    )
    result = await memory_tasks.extract_durable_facts({}, str(_CONV_ID))
    assert result == {"skipped": True, "reason": "user_missing"}


# ══════════════════════════════════════════════════════════════
# 2. Happy path — 3 faits extraits et insérés
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_happy_path_inserts_facts_and_posts_sentinel(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv = _make_conversation()
    db = patch_db(
        conversation=conv,
        messages=_make_messages(10),
        user=_make_user(),
    )

    # Mock LLM pour retourner 3 faits valides.
    llm_response = (
        '{"facts": ['
        '"L\'utilisateur est développeur Flutter",'
        '"L\'utilisateur habite au Cameroun",'
        '"L\'utilisateur travaille sur un projet NEXYA"'
        "]}"
    )
    monkeypatch.setattr(
        memory_tasks,
        "_call_llm_for_facts",
        AsyncMock(return_value=(llm_response, "gemini", "gemini-flash")),
    )

    mock_add = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(MemoryStore, "add", mock_add)

    result = await memory_tasks.extract_durable_facts({}, str(_CONV_ID))
    assert result["skipped"] is False
    assert result["facts_extracted"] == 3
    assert result["facts_skipped_sensitive"] == 0
    assert mock_add.await_count == 3
    # Vérif que chaque appel porte source='extracted' + source_conversation_id.
    for call in mock_add.await_args_list:
        kwargs = call.kwargs
        assert kwargs["source"] == "extracted"
        assert kwargs["source_conversation_id"] == _CONV_ID
    # Sentinelle posée.
    assert len(db._executed_updates) == 1


# ══════════════════════════════════════════════════════════════
# 3. Fail-safe — LLM down
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_skip_on_llm_failure_no_sentinel(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv = _make_conversation()
    db = patch_db(
        conversation=conv,
        messages=_make_messages(10),
        user=_make_user(),
    )
    monkeypatch.setattr(
        memory_tasks,
        "_call_llm_for_facts",
        AsyncMock(side_effect=RuntimeError("LLM timeout")),
    )

    result = await memory_tasks.extract_durable_facts({}, str(_CONV_ID))
    assert result == {"skipped": True, "reason": "llm_failed"}
    # Sentinelle NON posée → permet retry via cron fallback Phase 12.
    assert not db._executed_updates


# ══════════════════════════════════════════════════════════════
# 4. JSON cassé → 0 faits mais sentinelle posée
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_broken_json_posts_sentinel_with_zero_facts(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """JSON non-parseable → parser retourne [], sentinelle POSÉE.

    La conv a été analysée, rien n'a été extrait, mais on ne veut PAS
    la ré-analyser (le LLM produirait probablement encore du JSON cassé)."""
    conv = _make_conversation()
    db = patch_db(
        conversation=conv,
        messages=_make_messages(10),
        user=_make_user(),
    )
    monkeypatch.setattr(
        memory_tasks,
        "_call_llm_for_facts",
        AsyncMock(return_value=("{not valid json", "gemini", "gemini-flash")),
    )
    mock_add = AsyncMock()
    monkeypatch.setattr(MemoryStore, "add", mock_add)

    result = await memory_tasks.extract_durable_facts({}, str(_CONV_ID))
    assert result["skipped"] is False
    assert result["facts_extracted"] == 0
    mock_add.assert_not_awaited()
    # Sentinelle posée → évite la boucle infinie.
    assert len(db._executed_updates) == 1


# ══════════════════════════════════════════════════════════════
# 5. Fail-safe — MemoryStore.add raise → continue boucle
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_continues_on_memorystore_quota_error(
    patch_db, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si MemoryStore.add raise MemoryQuotaExceeded pour 1 fait, on
    continue la boucle pour les autres."""
    conv = _make_conversation()
    db = patch_db(
        conversation=conv,
        messages=_make_messages(10),
        user=_make_user(),
    )
    llm_response = (
        '{"facts": ['
        '"L\'utilisateur est Ivan",'
        '"L\'utilisateur a 28 ans",'
        '"L\'utilisateur aime la cuisine"'
        "]}"
    )
    monkeypatch.setattr(
        memory_tasks,
        "_call_llm_for_facts",
        AsyncMock(return_value=(llm_response, "gemini", "gemini-flash")),
    )
    # 1er ok, 2e raise quota, 3e ok.
    mock_add = AsyncMock(
        side_effect=[
            MagicMock(),
            MemoryQuotaExceededException(current=100, maximum=100, plan="free"),
            MagicMock(),
        ]
    )
    monkeypatch.setattr(MemoryStore, "add", mock_add)

    result = await memory_tasks.extract_durable_facts({}, str(_CONV_ID))
    assert result["skipped"] is False
    assert result["facts_extracted"] == 2
    assert result["facts_skipped_other"] >= 1
    assert mock_add.await_count == 3
    # Sentinelle posée malgré l'erreur partielle.
    assert len(db._executed_updates) == 1


# ══════════════════════════════════════════════════════════════
# 6. Filtre sensibilité — skip les faits sensibles
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_worker_filters_sensitive_facts(patch_db, monkeypatch: pytest.MonkeyPatch) -> None:
    """2 faits neutres + 1 sensible → 2 insertions, 1 skip sensible."""
    conv = _make_conversation()
    db = patch_db(
        conversation=conv,
        messages=_make_messages(10),
        user=_make_user(),
    )
    llm_response = (
        '{"facts": ['
        '"L\'utilisateur est développeur Flutter",'
        '"L\'utilisateur souffre de dépression",'  # sensible
        '"L\'utilisateur habite au Cameroun"'
        "]}"
    )
    monkeypatch.setattr(
        memory_tasks,
        "_call_llm_for_facts",
        AsyncMock(return_value=(llm_response, "gemini", "gemini-flash")),
    )
    mock_add = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(MemoryStore, "add", mock_add)

    result = await memory_tasks.extract_durable_facts({}, str(_CONV_ID))
    assert result["skipped"] is False
    assert result["facts_extracted"] == 2
    assert result["facts_skipped_sensitive"] == 1
    assert mock_add.await_count == 2


# ══════════════════════════════════════════════════════════════
# 7. Enqueue fail-safe
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_enqueue_redis_down_logs_and_returns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Redis down → warning log, return None sans raise."""

    async def _fail_pool():
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(memory_tasks, "_get_arq_pool", _fail_pool)
    # Ne doit pas raise.
    result = await memory_tasks.enqueue_memory_extraction(_CONV_ID)
    assert result is None
