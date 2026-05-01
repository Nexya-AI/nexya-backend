"""
Tests N3 — `tests/evals/judge.py`.

Couvre :
1. MockJudge déterministe (même couple Q/A → même score, à jamais).
2. MockJudge respect du seuil pass_score.
3. GeminiJudge fail-safe sur erreur SDK.
4. Parser JSON tolérant 3 passes (JSON direct, markdown wrapper,
   premier objet détecté).
5. Factory `judge_factory` dispatch correct.
"""

from __future__ import annotations

import pytest

from tests.evals.judge import (
    GeminiJudge,
    JudgeBase,
    MockJudge,
    Verdict,
    _parse_judge_json,
    judge_factory,
)

# ══════════════════════════════════════════════════════════════
# MockJudge
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_judge_is_deterministic_for_same_question_answer() -> None:
    """SHA-based : même Q/A → même score à jamais."""
    j = MockJudge()
    v1 = await j.judge(question="Q1", answer="A1", criteria=[], pass_score=5.0)
    v2 = await j.judge(question="Q1", answer="A1", criteria=[], pass_score=5.0)
    assert v1.score == v2.score
    assert v1.passed == v2.passed


@pytest.mark.asyncio
async def test_mock_judge_different_qa_yield_different_scores() -> None:
    j = MockJudge()
    s1 = (await j.judge(question="Q1", answer="A1", criteria=[], pass_score=5.0)).score
    s2 = (await j.judge(question="Q2", answer="A2", criteria=[], pass_score=5.0)).score
    assert s1 != s2  # SHA-256 garantit la divergence


@pytest.mark.asyncio
async def test_mock_judge_score_in_valid_range() -> None:
    j = MockJudge()
    for q, a in [("a", "b"), ("c", "d"), ("très long " * 50, "réponse longue"), ("", "")]:
        v = await j.judge(question=q, answer=a, criteria=[], pass_score=5.0)
        assert 0.0 <= v.score <= 10.0


@pytest.mark.asyncio
async def test_mock_judge_passed_respects_pass_score() -> None:
    j = MockJudge()
    v_low = await j.judge(question="Q", answer="A", criteria=[], pass_score=11.0)  # impossible
    assert v_low.passed is False
    v_high = await j.judge(question="Q", answer="A", criteria=[], pass_score=-1.0)  # always pass
    assert v_high.passed is True


@pytest.mark.asyncio
async def test_mock_judge_ignores_criteria_for_determinism() -> None:
    """Les critères changent ne devraient PAS changer le score (mock)."""
    j = MockJudge()
    v1 = await j.judge(question="Q", answer="A", criteria=["c1"], pass_score=5.0)
    v2 = await j.judge(question="Q", answer="A", criteria=["c2", "c3"], pass_score=5.0)
    assert v1.score == v2.score


def test_mock_judge_name_is_mock() -> None:
    assert MockJudge().name == "mock"


# ══════════════════════════════════════════════════════════════
# GeminiJudge
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_gemini_judge_fail_safe_on_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si le SDK lève, retourne Verdict score=0 reasoning judge_error."""
    j = GeminiJudge()

    async def _raise(*args, **kwargs):  # type: ignore[no-untyped-def]
        raise RuntimeError("network down")

    monkeypatch.setattr(j, "_call_gemini", _raise)
    v = await j.judge(question="Q", answer="A", criteria=["c"], pass_score=7.0)
    assert v.score == 0.0
    assert v.passed is False
    assert "judge_error" in v.reasoning


@pytest.mark.asyncio
async def test_gemini_judge_clamps_score_to_valid_range(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Si Gemini retourne 99.0 (mal géré), clamp à 10.0."""
    j = GeminiJudge()

    async def _return_high(*args, **kwargs):  # type: ignore[no-untyped-def]
        return '{"score": 99.0, "reasoning": "très bon"}'

    monkeypatch.setattr(j, "_call_gemini", _return_high)
    v = await j.judge(question="Q", answer="A", criteria=[], pass_score=7.0)
    assert v.score == 10.0


def test_gemini_judge_default_model_name() -> None:
    assert GeminiJudge().name == "gemini-2.5-pro"


# ══════════════════════════════════════════════════════════════
# Parser tolérant
# ══════════════════════════════════════════════════════════════


def test_parse_judge_json_passe_1_direct() -> None:
    raw = '{"score": 7.5, "reasoning": "ok"}'
    parsed = _parse_judge_json(raw)
    assert parsed["score"] == 7.5
    assert parsed["reasoning"] == "ok"


def test_parse_judge_json_passe_2_markdown_wrapper() -> None:
    raw = "Voici ma note :\n```json\n{\"score\": 8.0, \"reasoning\": \"ok\"}\n```\nFin."
    parsed = _parse_judge_json(raw)
    assert parsed["score"] == 8.0


def test_parse_judge_json_passe_3_first_object_detected() -> None:
    raw = "Note finale: {\"score\": 6.5, \"reasoning\": \"moyen\"} merci."
    parsed = _parse_judge_json(raw)
    assert parsed["score"] == 6.5


def test_parse_judge_json_failure_returns_default() -> None:
    raw = "Pas de JSON ici, juste du texte."
    parsed = _parse_judge_json(raw)
    assert parsed["score"] == 0.0
    assert parsed["reasoning"] == "parse_failed"


def test_parse_judge_json_empty_string() -> None:
    parsed = _parse_judge_json("")
    assert parsed["score"] == 0.0


# ══════════════════════════════════════════════════════════════
# Factory
# ══════════════════════════════════════════════════════════════


def test_judge_factory_mock() -> None:
    j = judge_factory("mock")
    assert isinstance(j, MockJudge)


def test_judge_factory_gemini_aliases() -> None:
    for alias in ("gemini", "gemini-2.5-pro", "gemini-pro", "GEMINI"):
        j = judge_factory(alias)
        assert isinstance(j, GeminiJudge)


def test_judge_factory_unknown_raises() -> None:
    with pytest.raises(ValueError):
        judge_factory("openai-gpt4")


# ══════════════════════════════════════════════════════════════
# Verdict frozen
# ══════════════════════════════════════════════════════════════


def test_verdict_is_frozen() -> None:
    v = Verdict(score=7.0, passed=True, reasoning="ok")
    with pytest.raises((AttributeError, Exception)):
        v.score = 99.0  # type: ignore[misc]


def test_judge_base_is_abstract() -> None:
    """JudgeBase ne peut pas être instancié directement (ABC)."""
    with pytest.raises(TypeError):
        JudgeBase()  # type: ignore[abstract]
