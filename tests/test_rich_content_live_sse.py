"""Fix génération documents (live, 2026-06-10) — émission SSE `rich_content`.

Vérifie que `_persisted_stream` émet un `event: rich_content` EN DIRECT, juste
avant le `event: done`, quand le backend détecte un brouillon actionnable dans
la réponse assistante. Permet au client de poser la carte (PDF/Word, email,
WhatsApp, code...) sans attendre le reload de la conversation.

Couvre aussi les garde-fous :
- chat ordinaire (pas de brouillon) → aucun `rich_content` émis ;
- stream en échec (`done reason=error`) → aucun `rich_content`, même si le
  contenu partiel ressemble à un document ;
- la détection live est transmise à la finalisation (`rich_content_already_detected`)
  pour éviter une double détection (source de vérité unique) ;
- helpers purs `_sse_rich_content` / `_detect_rich_content_for_stream`.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock

from app.ai.engine.query_engine import StreamOutcome
from app.features.chat import router

# ───────────────────────── Fixtures de contenu ─────────────────────────

DOC_USER = "rédige une lettre formelle au maire de Yaoundé"
DOC_BODY = (
    "Madame la Maire de Yaoundé,\n\n"
    "Objet : demande d'autorisation pour l'organisation d'un événement public.\n\n"
    "Par la présente, je sollicite respectueusement votre autorisation pour "
    "organiser une manifestation culturelle sur la place publique de la ville. "
    "Cet événement rassemblera de nombreux citoyens autour de la promotion de "
    "la culture locale et du vivre-ensemble.\n\n"
    "Je vous prie d'agréer, Madame la Maire, l'expression de ma haute "
    "considération."
)


class _FakeHandler:
    """StreamHandler minimal : rejoue une séquence d'events SSE bruts."""

    def __init__(self, events: list[str]) -> None:
        self._events = events

    async def stream(self, request, ctx) -> AsyncIterator[str]:  # noqa: ANN001
        for event in self._events:
            yield event


def _chunk(delta: str) -> str:
    return f"event: chunk\ndata: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"


def _doc_events() -> list[str]:
    return [_chunk(DOC_BODY), 'event: done\ndata: {"reason":"stop"}\n\n']


async def _collect(events: list[str], user_message: str, monkeypatch) -> list[str]:
    """Draine `_persisted_stream` en neutralisant la finalisation DB."""
    monkeypatch.setattr(router, "_finalize_in_fresh_session", AsyncMock())
    collected: list[str] = []
    gen = router._persisted_stream(
        handler=_FakeHandler(events),
        request=MagicMock(),
        ctx=MagicMock(),
        metrics=MagicMock(),
        assistant_message_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        user_message=user_message,
    )
    async for event in gen:
        collected.append(event)
    return collected


# ───────────────────────── Émission live ─────────────────────────


async def test_persisted_stream_emits_rich_content_before_done(monkeypatch) -> None:
    collected = await _collect(_doc_events(), DOC_USER, monkeypatch)

    rich_idx = next(
        (i for i, e in enumerate(collected) if e.startswith("event: rich_content")),
        None,
    )
    done_idx = next(
        (i for i, e in enumerate(collected) if e.startswith("event: done")),
        None,
    )
    assert rich_idx is not None, "un event: rich_content doit être émis pour un document"
    assert done_idx is not None
    assert rich_idx < done_idx, "rich_content doit précéder done (le client le reçoit avant la fin)"

    data_str = collected[rich_idx].split("data: ", 1)[1].strip()
    payload = json.loads(data_str)
    assert payload["kind"] == "document_draft"
    assert "data" in payload


async def test_persisted_stream_no_rich_content_for_plain_chat(monkeypatch) -> None:
    events = [
        _chunk("La capitale du Cameroun est Yaoundé."),
        'event: done\ndata: {"reason":"stop"}\n\n',
    ]
    collected = await _collect(events, "quelle est la capitale du Cameroun", monkeypatch)
    assert not any(e.startswith("event: rich_content") for e in collected)


async def test_persisted_stream_no_rich_content_on_failed_stream(monkeypatch) -> None:
    # Contenu type-document MAIS stream en échec → pas de carte (gate completed).
    events = [
        _chunk(DOC_BODY),
        'event: error\ndata: {"code":"LLM_UNAVAILABLE","message":"down"}\n\n',
        'event: done\ndata: {"reason":"error"}\n\n',
    ]
    collected = await _collect(events, DOC_USER, monkeypatch)
    assert not any(e.startswith("event: rich_content") for e in collected)


async def test_persisted_stream_relays_all_original_events(monkeypatch) -> None:
    # Anti-régression : le wrapper ne mange aucun event d'origine.
    collected = await _collect(_doc_events(), DOC_USER, monkeypatch)
    assert any(e.startswith("event: chunk") for e in collected)
    assert any(e.startswith("event: done") for e in collected)


async def test_persisted_stream_passes_detected_rich_to_finalize(monkeypatch) -> None:
    fake_finalize = AsyncMock()
    monkeypatch.setattr(router, "_finalize_in_fresh_session", fake_finalize)
    gen = router._persisted_stream(
        handler=_FakeHandler(_doc_events()),
        request=MagicMock(),
        ctx=MagicMock(),
        metrics=MagicMock(),
        assistant_message_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        user_message=DOC_USER,
    )
    async for _event in gen:
        pass

    assert fake_finalize.await_count == 1
    kwargs = fake_finalize.await_args.kwargs
    assert kwargs["rich_content_already_detected"] is True
    assert kwargs["rich_content"] is not None
    assert kwargs["rich_content"]["kind"] == "document_draft"


async def test_persisted_stream_finalize_flag_false_when_no_done(monkeypatch) -> None:
    # Stream coupé sans `done` (disconnect) → pas de détection live, le
    # finalize garde son chemin historique (rich_content_already_detected=False).
    fake_finalize = AsyncMock()
    monkeypatch.setattr(router, "_finalize_in_fresh_session", fake_finalize)
    gen = router._persisted_stream(
        handler=_FakeHandler([_chunk(DOC_BODY)]),  # pas de done
        request=MagicMock(),
        ctx=MagicMock(),
        metrics=MagicMock(),
        assistant_message_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        user_message=DOC_USER,
    )
    async for _event in gen:
        pass

    kwargs = fake_finalize.await_args.kwargs
    assert kwargs["rich_content_already_detected"] is False
    assert kwargs["rich_content"] is None


# ───────────────────────── Helpers purs ─────────────────────────


def test_sse_rich_content_format() -> None:
    out = router._sse_rich_content({"kind": "document_draft", "data": {"title": "X"}})
    assert out.startswith("event: rich_content\ndata: ")
    assert out.endswith("\n\n")
    body = out[len("event: rich_content\ndata: ") :].strip()
    parsed = json.loads(body)
    assert parsed["kind"] == "document_draft"
    assert parsed["data"]["title"] == "X"


def test_detect_rich_content_for_stream_gates_on_completed() -> None:
    mid = uuid.uuid4()

    # Stream en échec → None même avec un contenu type-document.
    failed = StreamOutcome(done_reason="error", content_parts=[DOC_BODY])
    assert router._detect_rich_content_for_stream(failed, DOC_USER, mid) is None

    # Contenu vide → None.
    empty = StreamOutcome(done_reason="stop", content_parts=[])
    assert router._detect_rich_content_for_stream(empty, DOC_USER, mid) is None

    # Completed + document → payload document_draft.
    ok = StreamOutcome(done_reason="stop", content_parts=[DOC_BODY])
    res = router._detect_rich_content_for_stream(ok, DOC_USER, mid)
    assert res is not None
    assert res["kind"] == "document_draft"


def test_detect_rich_content_for_stream_failsafe(monkeypatch) -> None:
    # Le détecteur lève → None + pas de propagation (chat continue sans carte).
    def _boom(*_args, **_kwargs):
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(router, "detect_rich_content", _boom)
    ok = StreamOutcome(done_reason="stop", content_parts=[DOC_BODY])
    assert router._detect_rich_content_for_stream(ok, DOC_USER, uuid.uuid4()) is None
