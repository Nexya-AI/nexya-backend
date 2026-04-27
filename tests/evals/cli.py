"""
Évals IA — Entrée CLI.

Usage :
    python -m tests.evals.cli --judge=mock --category=all
    python -m tests.evals.cli --judge=gemini --category=safety --limit=10
    python -m tests.evals.cli --judge=mock --update-baseline

Exit codes :
- 0 : tout OK (pas de régression au-dessus du seuil)
- 1 : régression détectée (au moins une catégorie au-delà de threshold-pp)
- 2 : erreur d'invocation (corpus vide, args invalides, etc.)

Skip gracieux : si `--judge=gemini` et pas de `GEMINI_API_KEY`, exit 0
avec warning (cas dev/CI sans secret API).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)

DEFAULT_THRESHOLD_PP = 10.0
DEFAULT_REPORT_DIR = Path(__file__).parent / "reports"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="tests.evals.cli",
        description="NEXYA AI evaluation harness (N3).",
    )
    p.add_argument(
        "--judge",
        choices=["mock", "gemini"],
        default="mock",
        help="Juge à utiliser (mock par défaut — gratuit, déterministe).",
    )
    p.add_argument(
        "--category",
        choices=["routing", "safety", "format", "accuracy", "identity", "all"],
        default="all",
        help="Catégorie unique à tester. 'all' = toutes.",
    )
    p.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Limite de questions traitées (debug rapide).",
    )
    p.add_argument(
        "--update-baseline",
        action="store_true",
        help="Sauvegarde la baseline avec les résultats actuels.",
    )
    p.add_argument(
        "--threshold-pp",
        type=float,
        default=DEFAULT_THRESHOLD_PP,
        help=f"Seuil en pp pour fail-on-regression (défaut {DEFAULT_THRESHOLD_PP}).",
    )
    p.add_argument(
        "--md-out",
        type=str,
        default=None,
        help="Chemin du rapport markdown (défaut : tests/evals/reports/report_<date>.md).",
    )
    p.add_argument(
        "--json-out",
        type=str,
        default=None,
        help="Chemin du rapport JSON.",
    )
    p.add_argument(
        "--no-baseline-check",
        action="store_true",
        help="Ne compare pas vs baseline (utile pour bootstrap).",
    )
    return p


async def _amain(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    # Skip gracieux si juge gemini sans clé API
    from app.config import settings  # noqa: PLC0415

    if args.judge == "gemini" and not settings.gemini_api_key and not settings.gemini_use_vertex:
        print(
            "SKIP: GEMINI_API_KEY vide ET gemini_use_vertex=False — "
            "judge=gemini impossible. Utilise --judge=mock ou remplis ta clé.",
            file=sys.stderr,
        )
        return 0

    from tests.evals.baseline import (  # noqa: PLC0415
        default_baseline_path,
        diff_vs_baseline,
        load_baseline,
        make_baseline,
        save_baseline,
    )
    from tests.evals.judge import judge_factory  # noqa: PLC0415
    from tests.evals.report import render_json, render_markdown  # noqa: PLC0415
    from tests.evals.runner import run_evals  # noqa: PLC0415

    judge = judge_factory(args.judge)
    category = None if args.category == "all" else args.category

    # Si mock judge, on force aussi mock candidate (test du pipeline,
    # pas de la qualité LLM — mock judge ne sait pas évaluer une vraie
    # réponse de toute façon, autant économiser les tokens).
    mock_candidate = args.judge == "mock"

    run = await run_evals(
        judge=judge, category=category, limit=args.limit, mock_candidate=mock_candidate
    )
    if run.total_questions == 0:
        print("ERREUR : corpus vide. Vérifie tests/evals/corpus/.", file=sys.stderr)
        return 2

    # Diff vs baseline (sauf si bootstrap demandé)
    diff = None
    if not args.no_baseline_check and not args.update_baseline:
        baseline = load_baseline(default_baseline_path())
        if baseline is not None:
            diff = diff_vs_baseline(
                current_pass_rate=run.pass_rate_per_category,
                current_score_avg=run.score_avg_per_category,
                current_judge_name=run.judge_name,
                baseline=baseline,
            )

    # Rapports
    md_path = (
        Path(args.md_out)
        if args.md_out
        else DEFAULT_REPORT_DIR / f"report_{run.finished_at_iso[:10]}.md"
    )
    render_markdown(run=run, diff=diff, threshold_pp=args.threshold_pp, out_path=md_path)
    if args.json_out:
        render_json(run=run, diff=diff, out_path=Path(args.json_out))

    # Update baseline si demandé
    if args.update_baseline:
        new_baseline = make_baseline(
            judge_name=run.judge_name,
            total_questions=run.total_questions,
            pass_rate_per_category=run.pass_rate_per_category,
            score_avg_per_category=run.score_avg_per_category,
        )
        save_baseline(new_baseline, default_baseline_path())
        print(
            f"✅ Baseline mise à jour : {default_baseline_path()}",
            file=sys.stderr,
        )

    # Résumé console
    total_pr = run.total_pass_rate() * 100.0
    print(
        f"\n📊 Évals — judge={run.judge_name} | total_pass={total_pr:.1f}% "
        f"| questions={run.total_questions}",
        file=sys.stderr,
    )
    for cat in sorted(run.pass_rate_per_category.keys()):
        pr = run.pass_rate_per_category[cat] * 100
        sc = run.score_avg_per_category[cat]
        print(f"  - {cat:10s} pass_rate={pr:5.1f}% score={sc:.2f}", file=sys.stderr)

    if diff is None:
        return 0

    if diff.has_regression(args.threshold_pp):
        regressed = diff.regressed_categories(args.threshold_pp)
        print(
            f"❌ RÉGRESSION détectée (>{args.threshold_pp:.1f}pp) : {', '.join(regressed)}",
            file=sys.stderr,
        )
        return 1

    print("✅ Pas de régression au-dessus du seuil.", file=sys.stderr)
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows : psycopg async refuse ProactorEventLoop (défaut Py 3.8+).
    # Même discipline que app/main.py, migrations/env.py, scripts/import_*.py.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    return asyncio.run(_amain(argv))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
