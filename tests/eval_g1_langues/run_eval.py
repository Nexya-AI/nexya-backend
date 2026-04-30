"""
Blind test runner — Expert Langues (G1) vs Gemini 2.5 Pro brut.

Usage :
    python tests/eval_g1_langues/run_eval.py

    # Mode dev (juste 5 questions, pas d'écriture de rapport) :
    python tests/eval_g1_langues/run_eval.py --limit 5 --no-report

Pour chaque question du YAML :
  A = réponse NEXYA (build_expert_corpus_context + Gemini 2.5 Pro)
  B = réponse Gemini 2.5 Pro **brut** (sans corpus)

Puis un juge Gemini 2.5 Pro reçoit les deux réponses + les critères attendus
et retourne un JSON strict :
    {"winner": "A"|"B"|"tie", "score_a": 0-10, "score_b": 0-10,
     "reasoning": "..."}

Pass criterion : ≥ 24/30 victoires A (80 %).

Skip gracieux si `GEMINI_API_KEY` vide — le test ne CASSE pas la CI, il
log un warning et exit 0 avec un message explicite.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
import yaml

log = structlog.get_logger()


JUDGE_MODEL = "gemini-2.5-pro"
CANDIDATE_MODEL = "gemini-2.5-pro"
PASS_THRESHOLD = 24  # /30 = 80 %


# ══════════════════════════════════════════════════════════════
# Schémas
# ══════════════════════════════════════════════════════════════


@dataclass(slots=True)
class Question:
    id: str
    domain: str
    target_lang: str
    question_fr: str
    expected_criteria: list[str]


@dataclass(slots=True)
class JudgeVerdict:
    winner: str  # "A" | "B" | "tie"
    score_a: float
    score_b: float
    reasoning: str


# ══════════════════════════════════════════════════════════════
# Chargement YAML
# ══════════════════════════════════════════════════════════════


def load_questions(path: Path) -> list[Question]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        Question(
            id=q["id"],
            domain=q["domain"],
            target_lang=q["target_lang"],
            question_fr=q["question_fr"],
            expected_criteria=list(q["expected_criteria"]),
        )
        for q in data["questions"]
    ]


# ══════════════════════════════════════════════════════════════
# Appels LLM
# ══════════════════════════════════════════════════════════════


async def answer_with_rag(question: Question, db, provider_embed) -> str:
    """Réponse A — NEXYA (corpus RAG + Gemini 2.5 Pro)."""
    from app.features.experts.context_builder import build_expert_corpus_context

    corpus_context = await build_expert_corpus_context(
        expert_slug="language",
        query=question.question_fr,
        db=db,
        provider=provider_embed,
    )
    system_prompt = (
        "Tu es NEXYA, expert en langues. Réponds précisément à la question "
        "ci-dessous en t'appuyant sur les extraits de corpus fournis si "
        "pertinents. Traduis/conjugue correctement. Donne des alternatives "
        "idiomatiques si approprié."
    )
    if corpus_context:
        system_prompt = corpus_context + "\n\n" + system_prompt

    return await _call_gemini_pro(system_prompt, question.question_fr)


async def answer_raw_gemini(question: Question) -> str:
    """Réponse B — Gemini 2.5 Pro brut, sans corpus."""
    system_prompt = (
        "Tu es un assistant expert en langues. Réponds précisément à la "
        "question ci-dessous. Traduis/conjugue correctement."
    )
    return await _call_gemini_pro(system_prompt, question.question_fr)


async def _call_gemini_pro(system_prompt: str, user_message: str) -> str:
    """Appel direct Gemini 2.5 Pro via `google-genai`.

    Supporte les 2 modes d'auth (identique GeminiEmbeddingsProvider) :
    - Vertex AI si `settings.gemini_use_vertex=True` (utilise ADC + project)
    - AI Studio sinon (utilise GEMINI_API_KEY)
    """
    from google import genai  # noqa: PLC0415

    from app.config import settings

    if settings.gemini_use_vertex:
        client = genai.Client(
            vertexai=True,
            project=settings.gcp_project_id,
            location=settings.gcp_region,
        )
    else:
        client = genai.Client(api_key=settings.gemini_api_key)
    prompt = f"{system_prompt}\n\n---\n\nQuestion utilisateur : {user_message}"
    response = await client.aio.models.generate_content(
        model=CANDIDATE_MODEL,
        contents=prompt,
    )
    return getattr(response, "text", None) or ""


# ══════════════════════════════════════════════════════════════
# Juge Gemini-as-judge
# ══════════════════════════════════════════════════════════════


JUDGE_PROMPT_TEMPLATE = """Tu es un juge indépendant et rigoureux. Compare deux réponses (A et B) \
à une même question et retourne UNIQUEMENT un objet JSON valide (pas de \
markdown, pas de texte avant/après).

Question : {question}

Critères attendus :
{criteria_block}

Réponse A :
{answer_a}

---

Réponse B :
{answer_b}

---

Retourne strictement :
{{"winner": "A" | "B" | "tie",
  "score_a": <float 0-10>,
  "score_b": <float 0-10>,
  "reasoning": "<1-3 phrases>"}}
"""


async def judge(question: Question, answer_a: str, answer_b: str) -> JudgeVerdict:
    criteria_block = "\n".join(f"- {c}" for c in question.expected_criteria)
    prompt = JUDGE_PROMPT_TEMPLATE.format(
        question=question.question_fr,
        criteria_block=criteria_block,
        answer_a=answer_a,
        answer_b=answer_b,
    )
    raw = await _call_gemini_pro("Tu es un juge JSON-only. Jamais de markdown.", prompt)
    parsed = _parse_judge_json(raw)
    return JudgeVerdict(
        winner=str(parsed.get("winner", "tie")).upper().strip(),
        score_a=float(parsed.get("score_a", 0.0)),
        score_b=float(parsed.get("score_b", 0.0)),
        reasoning=str(parsed.get("reasoning", "")),
    )


def _parse_judge_json(raw: str) -> dict[str, Any]:
    """Parse tolérant : enlève markdown wrapper ```json ... ```."""
    text = raw.strip()
    # Retire un bloc ```json ... ``` ou ``` ... ```.
    m = re.search(r"```(?:json)?\s*(\{.+?\})\s*```", text, flags=re.DOTALL)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Dernière tentative : extraire le 1er objet JSON détectable.
        m = re.search(r"(\{.*\})", text, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    return {"winner": "tie", "score_a": 0, "score_b": 0, "reasoning": "parse_failed"}


# ══════════════════════════════════════════════════════════════
# Rapport markdown
# ══════════════════════════════════════════════════════════════


def render_report(verdicts: list[tuple[Question, JudgeVerdict]], out_path: Path) -> None:
    wins_a = sum(1 for _, v in verdicts if v.winner == "A")
    wins_b = sum(1 for _, v in verdicts if v.winner == "B")
    ties = sum(1 for _, v in verdicts if v.winner not in ("A", "B"))
    total = len(verdicts)
    pct = (wins_a / total * 100) if total else 0

    lines: list[str] = []
    lines.append("# Blind test G1 — Expert Langues")
    lines.append("")
    lines.append(f"- Date : {datetime.now(UTC).isoformat()}")
    lines.append(f"- Questions : **{total}**")
    lines.append(f"- Victoires A (NEXYA RAG) : **{wins_a}** ({pct:.1f} %)")
    lines.append(f"- Victoires B (Gemini brut) : {wins_b}")
    lines.append(f"- Égalités : {ties}")
    lines.append(
        f"- Seuil : ≥ {PASS_THRESHOLD}/{total} → "
        f"**{'PASS ✅' if wins_a >= PASS_THRESHOLD else 'FAIL ❌'}**"
    )
    lines.append("")
    lines.append("## Détail")
    lines.append("")
    lines.append("| ID | Domaine | Winner | Score A | Score B | Raison |")
    lines.append("|----|---------|--------|---------|---------|--------|")
    for q, v in verdicts:
        reason = v.reasoning.replace("\n", " ").replace("|", "/")[:120]
        lines.append(
            f"| {q.id} | {q.domain} | {v.winner} | {v.score_a:.1f} | {v.score_b:.1f} | {reason} |"
        )
    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("eval.report.written", path=str(out_path))


# ══════════════════════════════════════════════════════════════
# Main
# ══════════════════════════════════════════════════════════════


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--no-report", action="store_true")
    args = parser.parse_args(argv)

    from app.config import settings

    if not settings.gemini_api_key:
        print(
            "SKIP: GEMINI_API_KEY vide — évaluation G1 impossible sans "
            "vrai LLM. Remplis la clé dans .env puis relance.",
            file=sys.stderr,
        )
        return 0

    questions_path = Path(__file__).parent / "questions.yaml"
    questions = load_questions(questions_path)
    if args.limit:
        questions = questions[: args.limit]

    from app.ai.embeddings import get_embeddings_provider
    from app.core.database.postgres import AsyncSessionLocal

    provider_embed = get_embeddings_provider()

    verdicts: list[tuple[Question, JudgeVerdict]] = []
    async with AsyncSessionLocal() as db:
        for i, q in enumerate(questions, start=1):
            log.info("eval.question.start", id=q.id, idx=i, total=len(questions))
            answer_a = await answer_with_rag(q, db, provider_embed)
            answer_b = await answer_raw_gemini(q)
            verdict = await judge(q, answer_a, answer_b)
            verdicts.append((q, verdict))
            log.info(
                "eval.question.done",
                id=q.id,
                winner=verdict.winner,
                score_a=verdict.score_a,
                score_b=verdict.score_b,
            )

    wins_a = sum(1 for _, v in verdicts if v.winner == "A")
    log.info(
        "eval.summary",
        total=len(verdicts),
        wins_a=wins_a,
        threshold=PASS_THRESHOLD,
        pass_=wins_a >= PASS_THRESHOLD,
    )

    if not args.no_report:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        out = Path(__file__).parent / f"report_{today}.md"
        render_report(verdicts, out)

    return 0 if wins_a >= PASS_THRESHOLD else 1


if __name__ == "__main__":  # pragma: no cover
    import sys

    # Windows : psycopg async refuse ProactorEventLoop (défaut Py 3.8+).
    # Même discipline que app/main.py, migrations/env.py, scripts/import_*.py.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))
