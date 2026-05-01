"""
Évals IA — Rapports markdown + JSON.

Sortie double :
- **Markdown** : lisible humain (rapport committable, attaché à une PR
  ou à une issue nightly).
- **JSON** : ingestion machine (annotations CI, futur dashboard K2 V2).

Le rapport markdown contient :
- Header (date, juge, commit_sha, total questions)
- Tableau pass_rate par catégorie + diff vs baseline
- Liste des questions régressées (top 10)
- Détail brut (tableau ID, catégorie, score, passed, raison)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import structlog

from tests.evals.baseline import BaselineDiff
from tests.evals.judge import Verdict

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# DATACLASSES (consommées par le runner)
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class QuestionResult:
    """Résultat d'une question : la question + le verdict du juge."""

    question_id: str
    category: str
    expert_id: str | None
    question_text: str
    answer_text: str
    verdict: Verdict


@dataclass(frozen=True, slots=True)
class RunResult:
    """Résultat agrégé d'un run complet.

    `pass_rate_per_category` : ratio [0.0, 1.0] (pas en pourcent — la
    conversion en pp se fait au moment du diff vs baseline).
    """

    judge_name: str
    started_at_iso: str
    finished_at_iso: str
    total_questions: int
    pass_rate_per_category: dict[str, float]
    score_avg_per_category: dict[str, float]
    questions: list[QuestionResult]

    def total_pass_rate(self) -> float:
        passed = sum(1 for q in self.questions if q.verdict.passed)
        return passed / max(1, len(self.questions))


# ═══════════════════════════════════════════════════════════════════
# MARKDOWN RENDER
# ═══════════════════════════════════════════════════════════════════


def render_markdown(
    *,
    run: RunResult,
    diff: BaselineDiff | None,
    threshold_pp: float,
    out_path: Path | None = None,
) -> str:
    """Rend le rapport markdown. Si `out_path`, écrit aussi sur disque."""
    lines: list[str] = []

    lines.append("# 📊 Rapport Évals IA — NEXYA")
    lines.append("")
    lines.append(f"- **Date** : {run.finished_at_iso}")
    lines.append(f"- **Juge** : `{run.judge_name}`")
    lines.append(f"- **Questions** : {run.total_questions}")
    lines.append(f"- **Pass rate global** : {run.total_pass_rate() * 100:.1f} %")
    if diff is not None:
        if diff.judge_mismatch:
            lines.append("- ⚠️ **Juge ≠ baseline** — comparaison apples vs oranges.")
        if diff.has_regression(threshold_pp):
            regressed = diff.regressed_categories(threshold_pp)
            lines.append(f"- ❌ **Régression détectée** : {', '.join(regressed)}")
        else:
            lines.append("- ✅ **Pas de régression au-dessus du seuil**.")
    lines.append("")

    # Tableau par catégorie
    lines.append("## 📈 Pass rate par catégorie")
    lines.append("")
    lines.append("| Catégorie | Pass rate | Score moyen | Δ pp baseline | Δ score baseline | Verdict |")
    lines.append("|-----------|-----------|-------------|---------------|------------------|---------|")
    for cat in sorted(run.pass_rate_per_category.keys()):
        pr = run.pass_rate_per_category[cat] * 100.0
        sc = run.score_avg_per_category.get(cat, 0.0)
        if diff is not None:
            pp_drop = diff.pp_drop_per_category.get(cat, 0.0)
            sc_drop = diff.score_drop_per_category.get(cat, 0.0)
            verdict_emoji = "❌" if pp_drop > threshold_pp else ("⚠️" if pp_drop > 0 else "✅")
            lines.append(
                f"| {cat} | {pr:.1f} % | {sc:.2f} | "
                f"{-pp_drop:+.1f} pp | {-sc_drop:+.2f} | {verdict_emoji} |"
            )
        else:
            lines.append(f"| {cat} | {pr:.1f} % | {sc:.2f} | (pas de baseline) | — | — |")
    lines.append("")

    # Top régressions
    if diff is not None and diff.has_regression(0.0):
        regressions = sorted(
            run.questions,
            key=lambda q: q.verdict.score,
        )[:10]
        lines.append("## 🔻 Top 10 questions au score le plus bas")
        lines.append("")
        lines.append("| ID | Catégorie | Expert | Score | Passed | Raison |")
        lines.append("|----|-----------|--------|-------|--------|--------|")
        for q in regressions:
            reason = (q.verdict.reasoning or "")[:80].replace("|", "/").replace("\n", " ")
            passed = "✅" if q.verdict.passed else "❌"
            lines.append(
                f"| {q.question_id} | {q.category} | {q.expert_id or '—'} | "
                f"{q.verdict.score:.1f} | {passed} | {reason} |"
            )
        lines.append("")

    # Détail brut (collapsible)
    lines.append("<details>")
    lines.append("<summary>📋 Détail toutes questions</summary>")
    lines.append("")
    lines.append("| ID | Cat. | Expert | Score | Passed |")
    lines.append("|----|------|--------|-------|--------|")
    for q in run.questions:
        passed = "✅" if q.verdict.passed else "❌"
        lines.append(
            f"| {q.question_id} | {q.category} | {q.expert_id or '—'} | "
            f"{q.verdict.score:.1f} | {passed} |"
        )
    lines.append("")
    lines.append("</details>")

    md = "\n".join(lines) + "\n"
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(md, encoding="utf-8")
        log.info("evals.report.markdown.written", path=str(out_path))
    return md


# ═══════════════════════════════════════════════════════════════════
# JSON RENDER
# ═══════════════════════════════════════════════════════════════════


def render_json(
    *,
    run: RunResult,
    diff: BaselineDiff | None,
    out_path: Path | None = None,
) -> dict[str, Any]:
    """Rend le rapport JSON pour ingestion machine."""
    payload: dict[str, Any] = {
        "judge_name": run.judge_name,
        "started_at": run.started_at_iso,
        "finished_at": run.finished_at_iso,
        "total_questions": run.total_questions,
        "pass_rate_per_category": run.pass_rate_per_category,
        "score_avg_per_category": run.score_avg_per_category,
        "total_pass_rate": run.total_pass_rate(),
        "questions": [
            {
                "id": q.question_id,
                "category": q.category,
                "expert_id": q.expert_id,
                "score": q.verdict.score,
                "passed": q.verdict.passed,
                "reasoning": q.verdict.reasoning,
            }
            for q in run.questions
        ],
    }
    if diff is not None:
        payload["diff"] = {
            "pp_drop_per_category": diff.pp_drop_per_category,
            "score_drop_per_category": diff.score_drop_per_category,
            "judge_mismatch": diff.judge_mismatch,
            "total_pp_drop": diff.total_pp_drop(),
        }

    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        log.info("evals.report.json.written", path=str(out_path))
    return payload
