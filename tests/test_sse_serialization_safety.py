"""Regression-guard : `_sse` ne doit JAMAIS crasher sur un objet non-serialisable.

Incident 2026-07-14 : un tool planner en echec mettait `exc.errors()` (une pydantic
ValidationError dont `ctx.error` contient une `ValueError` brute) dans le payload
`tool_result`. `json.dumps` levait alors `TypeError: Object of type ValueError is
not JSON serializable`, qui CRASHAIT tout le flux `/chat/stream` en 500 au lieu
d'afficher simplement l'echec du tool. `_sse` utilise desormais `default=str`.
"""

from __future__ import annotations

from app.ai.streaming import _sse


def test_sse_survives_non_serializable_object():
    # Reproduit la forme exacte du payload qui crashait : une exception brute
    # imbriquee dans la structure d'erreur d'un tool_result.
    data = {
        "id": "tc_1",
        "name": "create_task",
        "success": False,
        "error": {"details": [{"ctx": {"error": ValueError("boom")}}]},
    }
    out = _sse("tool_result", data)  # ne doit PAS lever
    assert out.startswith("event: tool_result\n")
    assert out.endswith("\n\n")
    assert "boom" in out  # l'exception est stringifiee, pas droppee ni crashee


def test_sse_normal_dict_still_compact_json():
    out = _sse("delta", {"delta": "hello"})
    assert 'data: {"delta":"hello"}' in out


def test_sse_raw_string_passthrough():
    out = _sse("done", "[DONE]")
    assert out == "event: done\ndata: [DONE]\n\n"
