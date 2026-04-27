"""
Évals IA — Juges (ABC + GeminiJudge + MockJudge).

Un juge transforme un triplet `(question, candidate_answer, criteria)` en
un `Verdict(score, passed, reasoning)`.

Deux implémentations :

1. **`GeminiJudge`** (vrai juge sémantique) — Gemini 2.5 Pro, structured
   output JSON, fallback parser tolérant 3 passes. Coûte des tokens.
   Utilisé en nightly schedule + en local quand on push une vraie eval.

2. **`MockJudge`** (juge déterministe) — SHA-256(question + answer)[:16]
   converti en score [0,10]. Pas de sémantique, mais reproductible et
   gratuit. Utilisé pour :
   - Tests pytest du harness (pas de clé API requise en CI)
   - Job CI `evals-pr` (anti-régression de pipeline, pas de qualité)

Le `score` est un float [0,10]. `passed = score >= question.expected_pass_score`
(défaut 7.0). Le harness consomme `passed` pour calculer `pass_rate`.
"""

from __future__ import annotations

import hashlib
import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any

import structlog

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class Verdict:
    """Verdict d'un juge sur une réponse candidate.

    - `score` : note [0.0, 10.0], 0=mauvaise, 10=parfaite.
    - `passed` : bool, dérivé de `score >= question.expected_pass_score`.
    - `reasoning` : 1-3 phrases du juge expliquant la note (pour debug).
    """

    score: float
    passed: bool
    reasoning: str


# ═══════════════════════════════════════════════════════════════════
# ABC
# ═══════════════════════════════════════════════════════════════════


class JudgeBase(ABC):
    """Contrat qu'un juge doit remplir.

    Sous-classe obligatoire pour `judge()`. `name` sert au logging et
    à l'identification dans la baseline (`baseline.judge_name`).
    """

    name: str = ""

    @abstractmethod
    async def judge(
        self,
        *,
        question: str,
        answer: str,
        criteria: list[str],
        pass_score: float,
    ) -> Verdict:
        """Note la réponse selon les critères. Jamais lève — fail-safe."""
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════════════
# MOCK JUDGE — déterministe SHA-256
# ═══════════════════════════════════════════════════════════════════


class MockJudge(JudgeBase):
    """Juge déterministe pour tests harness + CI sans clé API.

    Algo :
        h = SHA-256(f"{question}|||{answer}").hexdigest()
        score = (int(h[:8], 16) % 1100) / 100   # [0.0, 11.0)
        score = min(score, 10.0)                # clamp

    Garantie : même couple (question, answer) → même score, à jamais.
    Permet de tester le runner/baseline/report sans appeler de LLM réel.

    **Limite** : aucune sémantique. Un mock-pass != un vrai-pass. Le
    job CI `evals-pr` utilise MockJudge pour catcher les régressions de
    *pipeline* (le runner crash, le YAML est cassé, le baseline diverge),
    pas les régressions de qualité (qui demandent un vrai juge).
    """

    name: str = "mock"

    async def judge(
        self,
        *,
        question: str,
        answer: str,
        criteria: list[str],
        pass_score: float,
    ) -> Verdict:
        # Critères ignorés volontairement — déterminisme strict sur (question, answer).
        _ = criteria
        score = self._score_for(question, answer)
        passed = score >= pass_score
        return Verdict(
            score=score,
            passed=passed,
            reasoning=f"mock(sha={self._hash_prefix(question, answer)})",
        )

    @staticmethod
    def _score_for(question: str, answer: str) -> float:
        h = hashlib.sha256(f"{question}|||{answer}".encode()).hexdigest()
        raw = int(h[:8], 16) % 1100  # [0, 1099]
        return min(raw / 100.0, 10.0)

    @staticmethod
    def _hash_prefix(question: str, answer: str) -> str:
        h = hashlib.sha256(f"{question}|||{answer}".encode()).hexdigest()
        return h[:8]


# ═══════════════════════════════════════════════════════════════════
# GEMINI JUDGE — vrai juge sémantique
# ═══════════════════════════════════════════════════════════════════


_JUDGE_PROMPT = """Tu es un juge indépendant et rigoureux. Note la
réponse fournie selon les critères listés. Note de 0 à 10 (0=très
mauvaise, 10=parfaite). Retourne UNIQUEMENT un objet JSON valide (pas
de markdown, pas de texte avant/après).

Question :
{question}

Critères attendus (la réponse doit les satisfaire) :
{criteria_block}

Réponse à évaluer :
{answer}

---

Retourne strictement :
{{"score": <float 0-10>, "reasoning": "<1-3 phrases en français>"}}
"""


class GeminiJudge(JudgeBase):
    """Juge sémantique via Gemini 2.5 Pro structured output.

    Fail-safe absolu : sur erreur SDK, parse échoué, ou clé manquante,
    retourne un Verdict(score=0.0, passed=False, reasoning='judge_error:...')
    pour que le run continue (anti-blocage). Le caller agrège et peut
    flagger si trop de judge_errors.
    """

    name: str = "gemini-2.5-pro"

    def __init__(self, *, model: str = "gemini-2.5-pro") -> None:
        self._model = model

    async def judge(
        self,
        *,
        question: str,
        answer: str,
        criteria: list[str],
        pass_score: float,
    ) -> Verdict:
        try:
            raw = await self._call_gemini(question, answer, criteria)
        except Exception as exc:  # noqa: BLE001 — fail-safe absolu
            log.warning("evals.judge.error", error=str(exc), exc_type=type(exc).__name__)
            return Verdict(
                score=0.0,
                passed=False,
                reasoning=f"judge_error: {type(exc).__name__}",
            )

        parsed = _parse_judge_json(raw)
        score = float(parsed.get("score", 0.0))
        score = max(0.0, min(score, 10.0))  # clamp
        return Verdict(
            score=score,
            passed=score >= pass_score,
            reasoning=str(parsed.get("reasoning", "no reasoning"))[:300],
        )

    async def _call_gemini(
        self, question: str, answer: str, criteria: list[str]
    ) -> str:
        from google import genai  # noqa: PLC0415

        from app.config import settings

        criteria_block = "\n".join(f"- {c}" for c in criteria) or "- (aucun critère explicite)"
        prompt = _JUDGE_PROMPT.format(
            question=question,
            criteria_block=criteria_block,
            answer=answer,
        )

        if settings.gemini_use_vertex:
            client = genai.Client(
                vertexai=True,
                project=settings.gcp_project_id,
                location=settings.gcp_region,
            )
        else:
            client = genai.Client(api_key=settings.gemini_api_key)

        response = await client.aio.models.generate_content(
            model=self._model,
            contents=prompt,
        )
        return getattr(response, "text", None) or ""


# ═══════════════════════════════════════════════════════════════════
# PARSER JSON TOLÉRANT — pattern aligné G1
# ═══════════════════════════════════════════════════════════════════


def _parse_judge_json(raw: str) -> dict[str, Any]:
    """Parse tolérant 3 passes :
    1. JSON direct.
    2. Bloc ```json...``` extrait via regex.
    3. Premier objet `{...}` détecté dans le texte.
    """
    text = raw.strip()

    # Passe 1 — JSON direct
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Passe 2 — Markdown wrapper ```json ... ``` ou ``` ... ```
    m = re.search(r"```(?:json)?\s*(\{.+?\})\s*```", text, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    # Passe 3 — Premier objet JSON détecté
    m = re.search(r"(\{.*\})", text, flags=re.DOTALL)
    if m:
        try:
            return json.loads(m.group(1))
        except json.JSONDecodeError:
            pass

    log.warning("evals.judge.parse_failed", raw_prefix=text[:120])
    return {"score": 0.0, "reasoning": "parse_failed"}


# ═══════════════════════════════════════════════════════════════════
# FACTORY
# ═══════════════════════════════════════════════════════════════════


def judge_factory(name: str) -> JudgeBase:
    """Dispatch nom court → instance du juge."""
    n = name.lower().strip()
    if n == "mock":
        return MockJudge()
    if n in ("gemini", "gemini-2.5-pro", "gemini-pro"):
        return GeminiJudge()
    raise ValueError(f"Juge inconnu : {name!r}. Utilise 'mock' ou 'gemini'.")
