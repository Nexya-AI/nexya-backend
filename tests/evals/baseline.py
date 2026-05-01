"""
Évals IA — Baseline gelée.

Une `Baseline` est un snapshot des `pass_rate` et `score_avg` par
catégorie, gelé dans `tests/evals/baselines/baseline.json` et committé
dans le repo. Permet de détecter les régressions de qualité IA en
comparant le run courant vs cette baseline.

Pourquoi gelée plutôt que score absolu :
- La qualité absolue est subjective (un juge à 7/10 vs un autre à 6.5/10
  ne dit rien — c'est leur **delta** qui compte).
- Un seuil absolu pousse à pumper la baseline (« on bump pour passer »).
- Le diff vs baseline est le seul signal anti-régression objectif.

Utilisation :
- Premier run : `cli.py --update-baseline` save l'état actuel.
- Runs suivants : compare current vs baseline, fail si pp_drop > seuil.
- Quand une amélioration est intentionnelle (refactor prompt qui gagne
  +5pp) : run avec `--update-baseline` pour figer le nouveau standard.

Les diffs sont en **points de pourcentage** (pp), pas en ratio relatif :
"75% → 65%" = -10 pp, lisible directement.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class Baseline:
    """Snapshot gelé d'un run réussi.

    - `commit_sha` : git SHA au moment du save (`(unknown)` si hors repo).
    - `judge_name` : `mock` ou `gemini-2.5-pro`. Une baseline mock-judge
      ne doit PAS être comparée à un run gemini-judge (signal apples vs
      oranges) — le runner émet un warning.
    - `pass_rate_per_category` : dict `{routing: 0.93, safety: 0.87, ...}`
    - `score_avg_per_category` : dict `{routing: 8.5, safety: 7.9, ...}`
    """

    commit_sha: str
    date_iso: str
    judge_name: str
    total_questions: int
    pass_rate_per_category: dict[str, float]
    score_avg_per_category: dict[str, float]

    def to_json(self) -> str:
        return json.dumps(asdict(self), indent=2, sort_keys=True, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> Baseline:
        data = json.loads(raw)
        return cls(
            commit_sha=str(data["commit_sha"]),
            date_iso=str(data["date_iso"]),
            judge_name=str(data["judge_name"]),
            total_questions=int(data["total_questions"]),
            pass_rate_per_category=dict(data["pass_rate_per_category"]),
            score_avg_per_category=dict(data["score_avg_per_category"]),
        )


@dataclass(frozen=True, slots=True)
class BaselineDiff:
    """Diff par catégorie : pp_drop > 0 = régression."""

    pp_drop_per_category: dict[str, float] = field(default_factory=dict)
    score_drop_per_category: dict[str, float] = field(default_factory=dict)
    judge_mismatch: bool = False  # True si current.judge_name ≠ baseline.judge_name

    def has_regression(self, threshold_pp: float) -> bool:
        """True s'il y a au moins une catégorie au-dessus du seuil."""
        return any(drop > threshold_pp for drop in self.pp_drop_per_category.values())

    def regressed_categories(self, threshold_pp: float) -> list[str]:
        return sorted(
            cat for cat, drop in self.pp_drop_per_category.items() if drop > threshold_pp
        )

    def total_pp_drop(self) -> float:
        return sum(self.pp_drop_per_category.values())


# ═══════════════════════════════════════════════════════════════════
# I/O
# ═══════════════════════════════════════════════════════════════════


def load_baseline(path: Path) -> Baseline | None:
    """Charge la baseline. None si le fichier n'existe pas (premier run)."""
    if not path.exists():
        log.info("evals.baseline.missing", path=str(path))
        return None
    try:
        return Baseline.from_json(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, KeyError, ValueError) as exc:
        log.warning("evals.baseline.parse_failed", path=str(path), error=str(exc))
        return None


def save_baseline(baseline: Baseline, path: Path) -> None:
    """Écrit la baseline. Crée les dossiers parents si besoin."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(baseline.to_json() + "\n", encoding="utf-8")
    log.info("evals.baseline.saved", path=str(path), commit_sha=baseline.commit_sha)


def make_baseline(
    *,
    judge_name: str,
    total_questions: int,
    pass_rate_per_category: dict[str, float],
    score_avg_per_category: dict[str, float],
    commit_sha: str | None = None,
) -> Baseline:
    """Construit une Baseline avec date courante + commit_sha auto-détecté."""
    return Baseline(
        commit_sha=commit_sha or _detect_commit_sha(),
        date_iso=datetime.now(UTC).isoformat(),
        judge_name=judge_name,
        total_questions=total_questions,
        pass_rate_per_category=dict(pass_rate_per_category),
        score_avg_per_category=dict(score_avg_per_category),
    )


# ═══════════════════════════════════════════════════════════════════
# DIFF
# ═══════════════════════════════════════════════════════════════════


def diff_vs_baseline(
    *,
    current_pass_rate: dict[str, float],
    current_score_avg: dict[str, float],
    current_judge_name: str,
    baseline: Baseline,
) -> BaselineDiff:
    """Compare un run courant à une baseline figée.

    pp_drop > 0 = régression. pp_drop < 0 = amélioration (welcome).
    Une catégorie absente de la baseline est ignorée (pp_drop = 0).
    Une catégorie absente du current mais présente dans la baseline est
    flaggée comme régression majeure (pass_rate=0).
    """
    pp_drop: dict[str, float] = {}
    score_drop: dict[str, float] = {}

    all_categories = set(current_pass_rate.keys()) | set(baseline.pass_rate_per_category.keys())
    for cat in all_categories:
        cur_pr = current_pass_rate.get(cat, 0.0)
        base_pr = baseline.pass_rate_per_category.get(cat, 0.0)
        pp_drop[cat] = (base_pr - cur_pr) * 100.0  # en pp

        cur_sc = current_score_avg.get(cat, 0.0)
        base_sc = baseline.score_avg_per_category.get(cat, 0.0)
        score_drop[cat] = base_sc - cur_sc  # en points (échelle 0-10)

    return BaselineDiff(
        pp_drop_per_category=pp_drop,
        score_drop_per_category=score_drop,
        judge_mismatch=(current_judge_name != baseline.judge_name),
    )


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════


def _detect_commit_sha() -> str:
    """Tente de récupérer le SHA du HEAD git. Fallback `(unknown)`.

    Volontairement simple — pas de dep sur GitPython, juste read du
    fichier `.git/HEAD` + `.git/refs/heads/<branch>`. Suffisant pour
    une trace forensic dans la baseline.
    """
    try:
        # Cherche le .git en remontant l'arborescence depuis ce fichier.
        cur = Path(__file__).resolve()
        for _ in range(10):
            git_dir = cur.parent / ".git"
            if git_dir.is_dir():
                head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
                if head.startswith("ref: "):
                    ref_path = git_dir / head[5:]
                    if ref_path.exists():
                        return ref_path.read_text(encoding="utf-8").strip()[:40]
                return head[:40]
            cur = cur.parent
            if cur == cur.parent:
                break
    except Exception:  # noqa: BLE001 — best-effort
        pass
    return "(unknown)"


# ═══════════════════════════════════════════════════════════════════
# CHEMIN DE LA BASELINE PAR DÉFAUT
# ═══════════════════════════════════════════════════════════════════


def default_baseline_path() -> Path:
    """`tests/evals/baselines/baseline.json` (relatif au package)."""
    return Path(__file__).parent / "baselines" / "baseline.json"
