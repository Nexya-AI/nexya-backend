"""
Blind test runner — Expert Juridique (G3 legal) vs Gemini 2.5 Pro brut.

Usage :
    python tests/eval_g3_legal/run_eval.py

    # Mode dev (5 questions, pas de rapport) :
    python tests/eval_g3_legal/run_eval.py --limit 5 --no-report

    # Une seule question (mesure rapide après un fix) :
    python tests/eval_g3_legal/run_eval.py --question-id penal_vol

Pour chaque question du YAML :
  A = réponse NEXYA (corpus RAG legal conscient du domaine + Gemini 2.5 Pro)
  B = réponse Gemini 2.5 Pro **brut** (sans corpus, assistant juridique générique)

Un juge Gemini 2.5 Pro reçoit les deux réponses + les critères attendus et
retourne un JSON strict {"winner","score_a","score_b","reasoning"}.

Le différentiel juridique se joue sur la PRÉCISION DE LA CITATION : le corpus
fournit l'article camerounais/OHADA/CIMA exact, là où Gemini brut connaît les
principes mais cite souvent un article français, approximatif ou inventé.

Pass criterion : >= 24/30 victoires A (80 %). Cible produit : >= 85 %.
Skip gracieux si ni Vertex AI ni GEMINI_API_KEY.
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
PASS_RATE = 0.80  # 24/30
EXPERT_SLUG = "legal"


@dataclass(slots=True)
class Question:
    id: str
    domain: str
    question_fr: str
    expected_criteria: list[str]


@dataclass(slots=True)
class JudgeVerdict:
    winner: str
    score_a: float
    score_b: float
    reasoning: str


def load_questions(path: Path) -> list[Question]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    return [
        Question(
            id=q["id"],
            domain=q["domain"],
            question_fr=q["question_fr"],
            expected_criteria=list(q["expected_criteria"]),
        )
        for q in data["questions"]
    ]


# ── Prompts système ───────────────────────────────────────────

_NEXYA_LEGAL_SYSTEM = (
    "Tu es l'Expert Droit & Justice de NEXYA (Nexyalabs). Tu fournis de "
    "l'information juridique générale en droit camerounais, droit OHADA "
    "(17 pays africains) et droit CIMA des assurances. Ta marque de fabrique : "
    "CITER LA SOURCE LÉGALE EXACTE (numéro d'article + nom du code/acte). "
    "Quand le système te fournit des extraits de codes framés "
    "`<<<DOCUMENT EXTRACT>>>` (corpus juridique vérifié), utilise-les comme "
    "SOURCE PREMIÈRE des références exactes et cite leurs articles. "
    "MAIS reste à jour : si un extrait correspond visiblement à une version "
    "ancienne d'un texte (acte uniforme OHADA révisé en 2010/2014, code civil "
    "colonial remplacé par une loi camerounaise plus récente, loi abrogée), "
    "signale-le explicitement et donne l'état du DROIT EN VIGUEUR au Cameroun "
    "en t'appuyant sur ta connaissance, sans te limiter à l'extrait daté. "
    "Construis une réponse complète et structurée (définition, règle, "
    "conséquences pratiques), pas une simple citation. "
    "N'invente JAMAIS une référence : si tu n'es pas certain, dis-le. "
    "Tu n'es ni avocat ni notaire : pour un cas concret, rappelle de "
    "consulter un professionnel. Si la question sort du droit (cuisine, "
    "code informatique, calcul, médecine), redirige vers le mode adéquat "
    "sans inventer de réponse hors-scope."
)

_RAW_LEGAL_SYSTEM = (
    "Tu es un assistant juridique. Réponds de façon claire et structurée, "
    "en citant les articles de loi pertinents quand c'est possible. Sois concis."
)


async def answer_with_rag(question: Question, db, provider_embed, corpus_kwargs: dict) -> str:
    """Réponse A — NEXYA (corpus RAG legal conscient du domaine + Gemini Pro)."""
    from app.features.experts.context_builder import build_expert_corpus_context

    corpus_context = await build_expert_corpus_context(
        expert_slug=EXPERT_SLUG,
        query=question.question_fr,
        db=db,
        provider=provider_embed,
        **corpus_kwargs,
    )
    system_prompt = _NEXYA_LEGAL_SYSTEM
    if corpus_context:
        system_prompt = corpus_context + "\n\n" + system_prompt
    return await _call_gemini_pro(system_prompt, question.question_fr)


async def answer_raw_gemini(question: Question) -> str:
    """Réponse B — Gemini 2.5 Pro brut, sans corpus."""
    return await _call_gemini_pro(_RAW_LEGAL_SYSTEM, question.question_fr)


_CLIENT = None


def _get_client():
    """Client genai créé une seule fois (réutilisé sur les ~96 appels du run,
    au lieu d'un client + token refresh par appel — évite la saturation de
    connexions qui provoquait des `RemoteDisconnected` sur les longs runs)."""
    global _CLIENT
    if _CLIENT is None:
        from google import genai  # noqa: PLC0415

        from app.config import settings

        if settings.gemini_use_vertex:
            _CLIENT = genai.Client(
                vertexai=True, project=settings.gcp_project_id, location=settings.gcp_region
            )
        else:
            _CLIENT = genai.Client(api_key=settings.gemini_api_key)
    return _CLIENT


async def _call_gemini_pro(system_prompt: str, user_message: str) -> str:
    """Appel Gemini 2.5 Pro avec retry exponentiel sur erreurs réseau
    transitoires (un run de 32 questions = 96 appels sur ~50 min ; un seul
    hoquet réseau ne doit pas sacrifier tout le run)."""
    prompt = f"{system_prompt}\n\n---\n\nQuestion utilisateur : {user_message}"
    backoff = 3.0
    last_exc: Exception | None = None
    for attempt in range(1, 6):
        try:
            client = _get_client()
            response = await client.aio.models.generate_content(
                model=CANDIDATE_MODEL, contents=prompt
            )
            return getattr(response, "text", None) or ""
        except Exception as exc:  # noqa: BLE001 — réseau/transitoire, on retente
            last_exc = exc
            log.warning("eval_g3.call.retry", attempt=attempt, error=str(exc)[:160])
            await asyncio.sleep(backoff)
            backoff *= 2
    raise RuntimeError(f"Gemini call failed after 5 attempts: {last_exc}")


# ── Juge ──────────────────────────────────────────────────────

JUDGE_PROMPT_TEMPLATE = """Tu es un juge indépendant et rigoureux en droit \
(Cameroun / OHADA / CIMA). Compare deux réponses (A et B) à une même question \
juridique et retourne UNIQUEMENT un objet JSON valide (pas de markdown).

Critères de notation, par ordre d'importance :
1. EXACTITUDE DE LA CITATION : l'article cité existe-t-il et est-il le BON \
(bon numéro + bon code camerounais/OHADA/CIMA) ? Une référence inventée, \
approximative, ou tirée du droit français quand le droit camerounais/OHADA \
s'applique doit être lourdement pénalisée.
2. EXACTITUDE JURIDIQUE DE FOND : la règle énoncée est-elle correcte ?
3. UTILITÉ PRATIQUE : conséquences concrètes, démarches, délais.
4. PRUDENCE : rappel de consulter un professionnel pour un cas concret ; \
ne pas rédiger d'acte engageant.
Pour une question HORS-DOMAINE (`out_of_scope`) : valorise la RÉORIENTATION \
explicite ; une réponse qui détaille le contenu hors-droit perd des points.

Question : {question}
Domaine : {domain}

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
  "reasoning": "<1-3 phrases, mentionne si une référence est inventée>"}}
"""


async def judge(question: Question, answer_a: str, answer_b: str) -> JudgeVerdict:
    criteria_block = "\n".join(f"- {c}" for c in question.expected_criteria)
    prompt = JUDGE_PROMPT_TEMPLATE.format(
        question=question.question_fr,
        domain=question.domain,
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
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(\{.+?\})\s*```", text, flags=re.DOTALL)
    if m:
        text = m.group(1)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"(\{.*\})", text, flags=re.DOTALL)
        if m:
            try:
                return json.loads(m.group(1))
            except json.JSONDecodeError:
                pass
    return {"winner": "tie", "score_a": 0, "score_b": 0, "reasoning": "parse_failed"}


# ── Rapport ───────────────────────────────────────────────────


def render_report(
    verdicts: list[tuple[Question, JudgeVerdict]], out_path: Path, threshold: int
) -> None:
    wins_a = sum(1 for _, v in verdicts if v.winner == "A")
    wins_b = sum(1 for _, v in verdicts if v.winner == "B")
    ties = sum(1 for _, v in verdicts if v.winner not in ("A", "B"))
    total = len(verdicts)
    pct = (wins_a / total * 100) if total else 0

    by_domain: dict[str, dict[str, int]] = {}
    for q, v in verdicts:
        d = by_domain.setdefault(q.domain, {"A": 0, "B": 0, "TIE": 0})
        key = v.winner if v.winner in ("A", "B") else "TIE"
        d[key] += 1

    lines: list[str] = []
    lines.append("# Blind test G3 — Expert Juridique NEXYA (RAG 14 codes CM/OHADA/CIMA)")
    lines.append("")
    lines.append(f"- Date : {datetime.now(UTC).isoformat()}")
    lines.append(f"- Questions : **{total}**")
    lines.append(f"- Victoires A (NEXYA RAG legal) : **{wins_a}** ({pct:.1f} %)")
    lines.append(f"- Victoires B (Gemini Pro brut) : {wins_b}")
    lines.append(f"- Égalités : {ties}")
    lines.append(
        f"- Seuil : >= {threshold}/{total} → **{'PASS ✅' if wins_a >= threshold else 'FAIL ❌'}**"
    )
    lines.append("")
    lines.append("## Breakdown par domaine")
    lines.append("")
    lines.append("| Domaine | A | B | Tie |")
    lines.append("|---------|---|---|-----|")
    for d, c in sorted(by_domain.items()):
        lines.append(f"| {d} | {c['A']} | {c['B']} | {c['TIE']} |")
    lines.append("")
    lines.append("## Détail")
    lines.append("")
    lines.append("| ID | Domaine | Winner | Score A | Score B | Raison |")
    lines.append("|----|---------|--------|---------|---------|--------|")
    for q, v in verdicts:
        reason = v.reasoning.replace("\n", " ").replace("|", "/")[:130]
        lines.append(
            f"| {q.id} | {q.domain} | {v.winner} | {v.score_a:.1f} | {v.score_b:.1f} | {reason} |"
        )
    out_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("eval_g3.report.written", path=str(out_path))


# ── Checkpoint reprenable ─────────────────────────────────────

_CKPT = Path(__file__).parent / "_progress.jsonl"


def _load_ckpt() -> dict[str, JudgeVerdict]:
    """Verdicts déjà obtenus (clé = id question), pour reprendre après crash."""
    done: dict[str, JudgeVerdict] = {}
    if _CKPT.exists():
        for line in _CKPT.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            done[d["id"]] = JudgeVerdict(
                winner=d["winner"],
                score_a=d["score_a"],
                score_b=d["score_b"],
                reasoning=d["reasoning"],
            )
    return done


def _append_ckpt(qid: str, v: JudgeVerdict) -> None:
    with _CKPT.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "id": qid,
                    "winner": v.winner,
                    "score_a": v.score_a,
                    "score_b": v.score_b,
                    "reasoning": v.reasoning,
                },
                ensure_ascii=False,
            )
            + "\n"
        )


# ── Main ──────────────────────────────────────────────────────


async def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--question-id", type=str, default=None)
    parser.add_argument("--no-report", action="store_true")
    args = parser.parse_args(argv)

    from app.config import settings

    has_vertex = settings.gemini_use_vertex and settings.gcp_project_id
    has_api_key = bool(settings.gemini_api_key)
    if not has_vertex and not has_api_key:
        print("SKIP: ni Vertex AI ni GEMINI_API_KEY — éval G3 impossible.", file=sys.stderr)
        return 0

    questions = load_questions(Path(__file__).parent / "questions.yaml")
    if args.question_id:
        questions = [q for q in questions if q.id == args.question_id]
        if not questions:
            print(f"ERROR: question_id={args.question_id!r} introuvable.", file=sys.stderr)
            return 2
    elif args.limit:
        questions = questions[: args.limit]

    threshold = max(1, round(len(questions) * PASS_RATE))

    from app.ai.embeddings import get_embeddings_provider
    from app.ai.experts import get_expert_config
    from app.core.database.postgres import AsyncSessionLocal

    provider_embed = get_embeddings_provider()
    cfg = get_expert_config(EXPERT_SLUG)
    corpus_kwargs = {
        "k": cfg.corpus_k,
        "min_similarity": cfg.corpus_min_similarity,
        "max_chars": cfg.corpus_max_chars,
    }

    # Reprise après crash : on saute les questions déjà jugées (sauf run
    # mono-question explicite).
    done = {} if args.question_id else _load_ckpt()
    if done:
        log.info("eval_g3.resume", already_done=len(done))

    verdicts: list[tuple[Question, JudgeVerdict]] = []
    async with AsyncSessionLocal() as db:
        for i, q in enumerate(questions, start=1):
            if q.id in done:
                verdicts.append((q, done[q.id]))
                log.info("eval_g3.question.cached", id=q.id, idx=i, total=len(questions))
                continue
            log.info("eval_g3.question.start", id=q.id, idx=i, total=len(questions))
            answer_a = await answer_with_rag(q, db, provider_embed, corpus_kwargs)
            answer_b = await answer_raw_gemini(q)
            verdict = await judge(q, answer_a, answer_b)
            verdicts.append((q, verdict))
            _append_ckpt(q.id, verdict)
            log.info(
                "eval_g3.question.done",
                id=q.id,
                winner=verdict.winner,
                score_a=verdict.score_a,
                score_b=verdict.score_b,
            )

    wins_a = sum(1 for _, v in verdicts if v.winner == "A")
    pct = wins_a / len(verdicts) * 100 if verdicts else 0
    log.info(
        "eval_g3.summary",
        total=len(verdicts),
        wins_a=wins_a,
        pct=round(pct, 1),
        threshold=threshold,
        pass_=wins_a >= threshold,
    )
    print(
        f"\nRESULTAT : {wins_a}/{len(verdicts)} victoires A ({pct:.1f} %) - "
        f"seuil {threshold} -> {'PASS' if wins_a >= threshold else 'FAIL'}"
    )

    if not args.no_report:
        today = datetime.now(UTC).strftime("%Y-%m-%d")
        render_report(verdicts, Path(__file__).parent / f"report_{today}.md", threshold)

    # Run complet terminé → on efface le checkpoint pour repartir propre.
    if not args.question_id:
        _CKPT.unlink(missing_ok=True)

    return 0 if wins_a >= threshold else 1


if __name__ == "__main__":  # pragma: no cover
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    raise SystemExit(asyncio.run(main()))
