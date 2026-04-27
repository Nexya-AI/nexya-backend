"""
Tests N3 — `tests/evals/baseline.py`.

Couvre :
1. `Baseline` round-trip JSON (save → load → equal).
2. `make_baseline` injecte date + commit_sha auto.
3. `diff_vs_baseline` calcule pp_drop correctement.
4. `BaselineDiff.has_regression` respect du seuil.
5. `BaselineDiff.regressed_categories` retourne triées.
6. `load_baseline` retourne None si fichier manquant.
7. `load_baseline` tolère un JSON malformé (return None + log).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.evals.baseline import (
    Baseline,
    BaselineDiff,
    diff_vs_baseline,
    load_baseline,
    make_baseline,
    save_baseline,
)


# ══════════════════════════════════════════════════════════════
# Baseline round-trip
# ══════════════════════════════════════════════════════════════


def test_baseline_round_trip_json() -> None:
    b1 = Baseline(
        commit_sha="abc123",
        date_iso="2026-04-27T10:00:00+00:00",
        judge_name="mock",
        total_questions=135,
        pass_rate_per_category={"safety": 0.95, "routing": 1.0},
        score_avg_per_category={"safety": 9.2, "routing": 10.0},
    )
    b2 = Baseline.from_json(b1.to_json())
    assert b1 == b2


def test_baseline_save_and_load(tmp_path: Path) -> None:
    path = tmp_path / "baselines" / "baseline.json"
    b = Baseline(
        commit_sha="def456",
        date_iso="2026-04-27T10:00:00+00:00",
        judge_name="gemini-2.5-pro",
        total_questions=100,
        pass_rate_per_category={"a": 0.8},
        score_avg_per_category={"a": 8.0},
    )
    save_baseline(b, path)
    assert path.exists()
    loaded = load_baseline(path)
    assert loaded == b


def test_load_baseline_missing_file_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "nonexistent.json"
    assert load_baseline(path) is None


def test_load_baseline_malformed_json_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "malformed.json"
    path.write_text("not a valid json {", encoding="utf-8")
    assert load_baseline(path) is None


def test_load_baseline_missing_field_returns_none(tmp_path: Path) -> None:
    path = tmp_path / "incomplete.json"
    path.write_text(json.dumps({"commit_sha": "abc"}), encoding="utf-8")
    assert load_baseline(path) is None


# ══════════════════════════════════════════════════════════════
# make_baseline
# ══════════════════════════════════════════════════════════════


def test_make_baseline_includes_date_and_commit_sha() -> None:
    b = make_baseline(
        judge_name="mock",
        total_questions=10,
        pass_rate_per_category={"a": 1.0},
        score_avg_per_category={"a": 10.0},
    )
    assert b.judge_name == "mock"
    assert b.total_questions == 10
    assert b.date_iso  # auto-rempli ISO
    assert b.commit_sha  # `(unknown)` ou un vrai SHA


def test_make_baseline_explicit_commit_sha_used() -> None:
    b = make_baseline(
        judge_name="mock",
        total_questions=1,
        pass_rate_per_category={},
        score_avg_per_category={},
        commit_sha="custom-sha",
    )
    assert b.commit_sha == "custom-sha"


# ══════════════════════════════════════════════════════════════
# diff_vs_baseline
# ══════════════════════════════════════════════════════════════


def _baseline_factory(
    pass_rate: dict[str, float],
    score: dict[str, float],
    *,
    judge_name: str = "mock",
) -> Baseline:
    return Baseline(
        commit_sha="x",
        date_iso="2026-04-27T00:00:00+00:00",
        judge_name=judge_name,
        total_questions=10,
        pass_rate_per_category=pass_rate,
        score_avg_per_category=score,
    )


def test_diff_detects_regression_when_pass_rate_drops() -> None:
    baseline = _baseline_factory({"safety": 0.90}, {"safety": 9.0})
    diff = diff_vs_baseline(
        current_pass_rate={"safety": 0.80},
        current_score_avg={"safety": 8.0},
        current_judge_name="mock",
        baseline=baseline,
    )
    assert diff.pp_drop_per_category["safety"] == pytest.approx(10.0)
    assert diff.score_drop_per_category["safety"] == pytest.approx(1.0)
    assert diff.has_regression(threshold_pp=5.0) is True
    assert diff.has_regression(threshold_pp=15.0) is False


def test_diff_no_regression_when_improvement() -> None:
    baseline = _baseline_factory({"safety": 0.80}, {"safety": 8.0})
    diff = diff_vs_baseline(
        current_pass_rate={"safety": 0.90},
        current_score_avg={"safety": 9.0},
        current_judge_name="mock",
        baseline=baseline,
    )
    # pp_drop = -10 (amélioration)
    assert diff.pp_drop_per_category["safety"] == pytest.approx(-10.0)
    assert diff.has_regression(threshold_pp=0.0) is False


def test_diff_judge_mismatch_flagged() -> None:
    baseline = _baseline_factory({"a": 1.0}, {"a": 10.0}, judge_name="mock")
    diff = diff_vs_baseline(
        current_pass_rate={"a": 1.0},
        current_score_avg={"a": 10.0},
        current_judge_name="gemini-2.5-pro",
        baseline=baseline,
    )
    assert diff.judge_mismatch is True


def test_diff_regressed_categories_sorted() -> None:
    baseline = _baseline_factory(
        {"safety": 0.90, "format": 0.80, "accuracy": 0.70},
        {"safety": 9.0, "format": 8.0, "accuracy": 7.0},
    )
    diff = diff_vs_baseline(
        current_pass_rate={"safety": 0.50, "format": 0.40, "accuracy": 0.70},
        current_score_avg={"safety": 5.0, "format": 4.0, "accuracy": 7.0},
        current_judge_name="mock",
        baseline=baseline,
    )
    cats = diff.regressed_categories(threshold_pp=10.0)
    assert cats == ["format", "safety"]  # sorted, accuracy stable absente


def test_baseline_diff_total_pp_drop() -> None:
    diff = BaselineDiff(
        pp_drop_per_category={"a": 5.0, "b": -3.0, "c": 8.0},
        score_drop_per_category={},
    )
    assert diff.total_pp_drop() == pytest.approx(10.0)


def test_diff_handles_missing_category_in_current() -> None:
    """Une catégorie présente dans la baseline mais absente du current
    apparaît comme une régression majeure (pp_drop = baseline_pr * 100)."""
    baseline = _baseline_factory({"safety": 0.95}, {"safety": 9.5})
    diff = diff_vs_baseline(
        current_pass_rate={},  # safety absente
        current_score_avg={},
        current_judge_name="mock",
        baseline=baseline,
    )
    assert diff.pp_drop_per_category["safety"] == pytest.approx(95.0)
