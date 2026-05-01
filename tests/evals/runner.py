"""
Évals IA — Runner principal.

Orchestre :
1. Charger les corpus YAML par catégorie.
2. Pour chaque question, générer la réponse candidate (`candidate.py`).
3. Juger la réponse (`judge.py`) — sauf catégorie `routing` qui a un
   verdict synthétique direct.
4. Agréger en `RunResult` (pass_rate / score_avg par catégorie).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import structlog
import yaml

from tests.evals.candidate import (
    CandidateAnswer,
    generate_candidate_answer,
    routing_expected_for,
)
from tests.evals.judge import JudgeBase, Verdict
from tests.evals.report import QuestionResult, RunResult

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# DATACLASS — Question
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class Question:
    """Une question du corpus.

    `expert_id` : pour `routing/safety/format/accuracy/identity`, c'est
    l'expert sous lequel la question est posée à NEXYA.

    `expected_pass_score` : seuil [0,10] pour considérer la réponse comme
    « passée ». Défaut 7.0. Une catégorie `safety` peut exiger 9.0.

    `expected_criteria` : critères passés au juge sémantique.

    `expected_provider`/`expected_model` : pour `routing` uniquement —
    contrat à vérifier sur EXPERT_REGISTRY.
    """

    id: str
    category: str
    question: str
    expert_id: str | None = None
    expected_criteria: list[str] | None = None
    expected_pass_score: float = 7.0
    expected_provider: str | None = None
    expected_model: str | None = None


CORPUS_DIR = Path(__file__).parent / "corpus"
CORPUS_CATEGORIES: tuple[str, ...] = (
    "routing",
    "safety",
    "format",
    "accuracy",
    "identity",
)


# ═══════════════════════════════════════════════════════════════════
# CHARGEMENT YAML
# ═══════════════════════════════════════════════════════════════════


def load_corpus(
    category: str | None = None,
    *,
    corpus_dir: Path | None = None,
) -> list[Question]:
    """Charge les YAML corpus. Si `category=None`, charge toutes les
    catégories connues."""
    base = corpus_dir or CORPUS_DIR
    cats = (category,) if category else CORPUS_CATEGORIES

    questions: list[Question] = []
    for cat in cats:
        path = base / f"{cat}.yaml"
        if not path.exists():
            log.warning("evals.corpus.missing", category=cat, path=str(path))
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for raw in data.get("questions", []):
            questions.append(_parse_question(raw, cat))
    return questions


def _parse_question(raw: dict, category: str) -> Question:
    return Question(
        id=str(raw["id"]),
        category=category,
        question=str(raw["question"]),
        expert_id=raw.get("expert_id"),
        expected_criteria=list(raw.get("expected_criteria") or []),
        expected_pass_score=float(raw.get("expected_pass_score", 7.0)),
        expected_provider=raw.get("expected_provider"),
        expected_model=raw.get("expected_model"),
    )


# ═══════════════════════════════════════════════════════════════════
# RUN
# ═══════════════════════════════════════════════════════════════════


async def run_evals(
    *,
    judge: JudgeBase,
    category: str | None = None,
    limit: int | None = None,
    corpus_dir: Path | None = None,
    mock_candidate: bool = False,
) -> RunResult:
    """Lance les évals. Retourne un `RunResult` agrégé."""
    started_at = datetime.now(UTC).isoformat()
    questions = load_corpus(category=category, corpus_dir=corpus_dir)
    if limit is not None:
        questions = questions[:limit]

    if not questions:
        log.warning("evals.run.empty_corpus", category=category)

    log.info(
        "evals.run.start",
        judge=judge.name,
        category=category or "all",
        total=len(questions),
    )

    results: list[QuestionResult] = []
    for idx, q in enumerate(questions, start=1):
        log.info(
            "evals.run.question",
            id=q.id,
            idx=idx,
            total=len(questions),
            category=q.category,
        )
        candidate = await generate_candidate_answer(
            category=q.category,
            question_text=q.question,
            expert_id=q.expert_id,
            expected_provider=q.expected_provider or _default_expected(q, "provider"),
            expected_model=q.expected_model or _default_expected(q, "model"),
            mock_candidate=mock_candidate,
        )
        verdict = await _resolve_verdict(judge=judge, question=q, candidate=candidate)
        results.append(
            QuestionResult(
                question_id=q.id,
                category=q.category,
                expert_id=q.expert_id,
                question_text=q.question,
                answer_text=candidate.text,
                verdict=verdict,
            )
        )

    finished_at = datetime.now(UTC).isoformat()
    pass_rate, score_avg = _aggregate(results)

    return RunResult(
        judge_name=judge.name,
        started_at_iso=started_at,
        finished_at_iso=finished_at,
        total_questions=len(results),
        pass_rate_per_category=pass_rate,
        score_avg_per_category=score_avg,
        questions=results,
    )


async def _resolve_verdict(
    *,
    judge: JudgeBase,
    question: Question,
    candidate: CandidateAnswer,
) -> Verdict:
    """Pour `routing`, on a un verdict synthétique basé sur le match
    introspection. Pour les autres catégories, on délègue au juge.
    """
    if candidate.is_synthetic_routing:
        score = 10.0 if candidate.routing_match else 0.0
        return Verdict(
            score=score,
            passed=candidate.routing_match,
            reasoning=("routing match" if candidate.routing_match else "routing mismatch"),
        )

    return await judge.judge(
        question=question.question,
        answer=candidate.text,
        criteria=question.expected_criteria or [],
        pass_score=question.expected_pass_score,
    )


def _default_expected(q: Question, field: str) -> str | None:
    """Pour les routing questions sans `expected_provider`/`expected_model`
    explicites, on dérive depuis EXPERT_REGISTRY (snapshot vivant)."""
    if q.category != "routing" or not q.expert_id:
        return None
    provider, model = routing_expected_for(q.expert_id)
    return provider if field == "provider" else model


def _aggregate(
    results: Iterable[QuestionResult],
) -> tuple[dict[str, float], dict[str, float]]:
    """Calcule pass_rate et score_avg par catégorie."""
    by_cat_passed: dict[str, list[bool]] = defaultdict(list)
    by_cat_score: dict[str, list[float]] = defaultdict(list)

    for r in results:
        by_cat_passed[r.category].append(r.verdict.passed)
        by_cat_score[r.category].append(r.verdict.score)

    pass_rate = {
        cat: (sum(1 for p in items if p) / max(1, len(items)))
        for cat, items in by_cat_passed.items()
    }
    score_avg = {cat: (sum(items) / max(1, len(items))) for cat, items in by_cat_score.items()}
    return pass_rate, score_avg
