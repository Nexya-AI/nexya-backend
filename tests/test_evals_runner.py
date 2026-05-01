"""
Tests N3 — `tests/evals/runner.py` + `candidate.py` + `report.py`.

Couvre :
1. `load_corpus` charge tous les YAMLs (5 catégories).
2. `load_corpus(category=...)` filtre.
3. `Question` parsing depuis dict YAML.
4. `_aggregate` calcule pass_rate/score_avg correctement.
5. `routing` catégorie : verdict synthétique sans LLM.
6. Routing mismatch → score 0 + passed False.
7. `_check_routing` détecte quand registre diverge.
8. `run_evals` end-to-end avec MockJudge.
9. `render_markdown` produit du markdown valide non vide.
10. `render_json` produit du JSON valide ingérable.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.evals.candidate import _check_routing, generate_candidate_answer
from tests.evals.judge import MockJudge
from tests.evals.report import QuestionResult, RunResult, render_json, render_markdown
from tests.evals.runner import (
    CORPUS_CATEGORIES,
    _aggregate,
    _parse_question,
    load_corpus,
    run_evals,
)

# ══════════════════════════════════════════════════════════════
# load_corpus
# ══════════════════════════════════════════════════════════════


def test_load_corpus_all_categories_returns_questions() -> None:
    qs = load_corpus()
    assert len(qs) > 100, f"Expected >100 questions in corpus, got {len(qs)}"
    cats = {q.category for q in qs}
    assert cats == set(CORPUS_CATEGORIES)


def test_load_corpus_single_category() -> None:
    qs = load_corpus(category="routing")
    assert len(qs) > 0
    assert all(q.category == "routing" for q in qs)


def test_load_corpus_routing_has_expected_provider_and_model() -> None:
    qs = load_corpus(category="routing")
    # Au moins une question routing doit avoir expected_provider/model
    assert any(q.expected_provider for q in qs)
    assert any(q.expected_model for q in qs)


def test_load_corpus_unknown_category_returns_empty() -> None:
    qs = load_corpus(category="nonexistent")
    assert qs == []


def test_parse_question_with_minimal_fields() -> None:
    q = _parse_question({"id": "q1", "question": "Texte ?"}, category="safety")
    assert q.id == "q1"
    assert q.category == "safety"
    assert q.question == "Texte ?"
    assert q.expected_pass_score == 7.0  # défaut
    assert q.expected_criteria == []


def test_parse_question_full_fields() -> None:
    q = _parse_question(
        {
            "id": "q1",
            "question": "Texte ?",
            "expert_id": "computer",
            "expected_criteria": ["c1", "c2"],
            "expected_pass_score": 8.5,
            "expected_provider": "gemini",
            "expected_model": "gemini-2.5-pro",
        },
        category="routing",
    )
    assert q.expert_id == "computer"
    assert q.expected_criteria == ["c1", "c2"]
    assert q.expected_pass_score == 8.5
    assert q.expected_provider == "gemini"
    assert q.expected_model == "gemini-2.5-pro"


# ══════════════════════════════════════════════════════════════
# _aggregate
# ══════════════════════════════════════════════════════════════


def _mk_qr(
    category: str,
    score: float,
    passed: bool,
    qid: str = "q",
) -> QuestionResult:
    from tests.evals.judge import Verdict

    return QuestionResult(
        question_id=qid,
        category=category,
        expert_id=None,
        question_text="?",
        answer_text="!",
        verdict=Verdict(score=score, passed=passed, reasoning="test"),
    )


def test_aggregate_single_category_all_passed() -> None:
    results = [
        _mk_qr("safety", 9.0, True),
        _mk_qr("safety", 8.5, True),
    ]
    pr, sc = _aggregate(results)
    assert pr["safety"] == 1.0
    assert sc["safety"] == pytest.approx(8.75)


def test_aggregate_mixed_pass_rate() -> None:
    results = [
        _mk_qr("a", 9.0, True),
        _mk_qr("a", 5.0, False),
        _mk_qr("a", 8.0, True),
        _mk_qr("a", 4.0, False),
    ]
    pr, sc = _aggregate(results)
    assert pr["a"] == 0.5
    assert sc["a"] == pytest.approx(6.5)


def test_aggregate_multiple_categories_independent() -> None:
    results = [
        _mk_qr("safety", 10.0, True),
        _mk_qr("format", 5.0, False),
        _mk_qr("safety", 9.0, True),
    ]
    pr, sc = _aggregate(results)
    assert pr["safety"] == 1.0
    assert pr["format"] == 0.0
    assert sc["safety"] == pytest.approx(9.5)
    assert sc["format"] == pytest.approx(5.0)


# ══════════════════════════════════════════════════════════════
# Routing — introspection
# ══════════════════════════════════════════════════════════════


def test_check_routing_match_for_known_expert() -> None:
    # general → gemini / gemini-2.5-flash (cf. EXPERT_REGISTRY)
    candidate = _check_routing(
        expert_id="general",
        expected_provider="gemini",
        expected_model="gemini-2.5-flash",
    )
    assert candidate.is_synthetic_routing is True
    assert candidate.routing_match is True


def test_check_routing_mismatch_when_registry_changes() -> None:
    candidate = _check_routing(
        expert_id="general",
        expected_provider="openai",  # ≠ gemini réel
        expected_model="gpt-4o",
    )
    assert candidate.is_synthetic_routing is True
    assert candidate.routing_match is False
    assert "MISMATCH" in candidate.text


def test_check_routing_unknown_expert_falls_back_to_general() -> None:
    candidate = _check_routing(
        expert_id="nonexistent",
        expected_provider="gemini",
        expected_model="gemini-2.5-flash",
    )
    # Inconnu → general → flash → match avec expected
    assert candidate.routing_match is True


@pytest.mark.asyncio
async def test_generate_candidate_routing_no_llm_call() -> None:
    """`category=routing` ne doit JAMAIS appeler le LLM (pure introspection)."""
    candidate = await generate_candidate_answer(
        category="routing",
        question_text="Vérification routing.",
        expert_id="computer",
        expected_provider="gemini",
        expected_model="gemini-2.5-flash",
    )
    assert candidate.is_synthetic_routing is True
    assert candidate.routing_match is True


# ══════════════════════════════════════════════════════════════
# run_evals end-to-end (MockJudge)
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_run_evals_routing_only_passes_with_intact_registry() -> None:
    """Si la registre n'a pas dérivé, routing doit être à 100 %."""
    judge = MockJudge()
    result = await run_evals(judge=judge, category="routing")
    assert result.total_questions > 0
    # Routing utilise verdict synthétique — pass = match
    pr_routing = result.pass_rate_per_category["routing"]
    assert pr_routing >= 0.9, f"Routing pass_rate={pr_routing} (registry diverged?)"


@pytest.mark.asyncio
async def test_run_evals_with_limit() -> None:
    judge = MockJudge()
    result = await run_evals(judge=judge, category="routing", limit=3)
    assert result.total_questions == 3


# ══════════════════════════════════════════════════════════════
# Reports
# ══════════════════════════════════════════════════════════════


def test_render_markdown_includes_header_and_table(tmp_path: Path) -> None:
    run = RunResult(
        judge_name="mock",
        started_at_iso="2026-04-27T00:00:00+00:00",
        finished_at_iso="2026-04-27T00:01:00+00:00",
        total_questions=2,
        pass_rate_per_category={"safety": 1.0},
        score_avg_per_category={"safety": 9.5},
        questions=[
            _mk_qr("safety", 10.0, True, qid="q1"),
            _mk_qr("safety", 9.0, True, qid="q2"),
        ],
    )
    out = tmp_path / "report.md"
    md = render_markdown(run=run, diff=None, threshold_pp=10.0, out_path=out)
    assert "📊 Rapport Évals IA" in md
    assert "safety" in md
    assert out.exists()
    assert "(pas de baseline)" in md


def test_render_json_serializable(tmp_path: Path) -> None:
    run = RunResult(
        judge_name="mock",
        started_at_iso="2026-04-27T00:00:00+00:00",
        finished_at_iso="2026-04-27T00:01:00+00:00",
        total_questions=1,
        pass_rate_per_category={"a": 1.0},
        score_avg_per_category={"a": 10.0},
        questions=[_mk_qr("a", 10.0, True, qid="q1")],
    )
    out = tmp_path / "report.json"
    payload = render_json(run=run, diff=None, out_path=out)
    # Round-trip JSON
    raw = out.read_text(encoding="utf-8")
    loaded = json.loads(raw)
    assert loaded["judge_name"] == "mock"
    assert loaded["total_questions"] == 1
    assert loaded["questions"][0]["passed"] is True
    assert payload["total_pass_rate"] == 1.0
