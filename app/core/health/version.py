"""
Détection de la version de l'app — 3 fallbacks (Session O1 volet B).

Pipeline de détection :
1. **`subprocess git describe --dirty --always --tags`** — meilleur en
   dev où le binaire `git` est dispo + le repo est complet.
2. **Lecture `.git/HEAD` + `.git/refs/heads/<branch>`** — marche dans
   un container Docker qui copie le repo SANS le binaire git mais avec
   le dossier `.git/` (rare en prod, courant en dev).
3. **Env vars `APP_VERSION` + `APP_COMMIT_SHA`** — posées par le
   pipeline CI/CD release (`scripts/release.sh` L1 + workflow
   release.yml). Mode prod typique.

Tous les niveaux sont best-effort + fail-safe : exception → fallback
silencieux. Le résultat final ne lève jamais.

Le `dirty` flag indique si l'arbre courant a des modifications non
commitées (`git status --porcelain` non vide). Utile pour détecter un
hotfix appliqué directement sur la prod (anti-pattern).
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import structlog

log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class VersionInfo:
    """Triplet version_string + commit_sha (court) + dirty + source.

    `source` ∈ {"git", "git_head_file", "env", "unknown"} — utile pour
    tracer en log d'où vient la version (debug ops).
    """

    version: str
    commit_sha: str
    tag: str | None
    dirty: bool
    source: str


_UNKNOWN = VersionInfo(
    version="unknown",
    commit_sha="unknown",
    tag=None,
    dirty=False,
    source="unknown",
)


def detect_version(repo_root: Path | None = None) -> VersionInfo:
    """Tente les 3 fallbacks dans l'ordre, retourne le 1er qui marche."""
    root = repo_root or _find_repo_root()

    # Fallback 1 — env vars (CI/CD release set these)
    env_v = _from_env_vars()
    if env_v is not None:
        return env_v

    # Fallback 2 — subprocess git
    git_v = _from_git_subprocess(root)
    if git_v is not None:
        return git_v

    # Fallback 3 — lecture .git/HEAD
    head_v = _from_git_head_file(root)
    if head_v is not None:
        return head_v

    return _UNKNOWN


# ═══════════════════════════════════════════════════════════════════
# FALLBACK 1 — env vars (prod)
# ═══════════════════════════════════════════════════════════════════


def _from_env_vars() -> VersionInfo | None:
    """Lit `APP_VERSION` + `APP_COMMIT_SHA` posées par CI/CD."""
    app_version = os.environ.get("APP_VERSION", "").strip()
    commit_sha = os.environ.get("APP_COMMIT_SHA", "").strip()
    if not app_version and not commit_sha:
        return None
    return VersionInfo(
        version=app_version or commit_sha[:8] or "unknown",
        commit_sha=commit_sha[:40] if commit_sha else "unknown",
        tag=app_version or None,
        dirty=False,  # CI/CD release → toujours clean
        source="env",
    )


# ═══════════════════════════════════════════════════════════════════
# FALLBACK 2 — git subprocess
# ═══════════════════════════════════════════════════════════════════


def _from_git_subprocess(root: Path | None) -> VersionInfo | None:
    """Appelle `git describe` + `git rev-parse HEAD` + `git status`."""
    if root is None:
        return None
    git_bin = shutil.which("git")
    if git_bin is None:
        return None
    try:
        sha = _run_git(["rev-parse", "HEAD"], cwd=root)
        if not sha:
            return None
        tag = _run_git(["describe", "--tags", "--exact-match", "HEAD"], cwd=root)
        # `--exact-match` retourne non-zero si pas de tag exact ; on l'absorbe.
        dirty_out = _run_git(["status", "--porcelain"], cwd=root)
        dirty = bool(dirty_out and dirty_out.strip())
        version = tag or sha[:8]
        return VersionInfo(
            version=version + ("-dirty" if dirty else ""),
            commit_sha=sha[:40],
            tag=tag,
            dirty=dirty,
            source="git",
        )
    except Exception as exc:  # noqa: BLE001 — fail-safe
        log.debug("version.git_subprocess_failed", error=str(exc))
        return None


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Exécute `git <args>` avec timeout 2 s. Retourne stdout strip ou None."""
    try:
        result = subprocess.run(  # noqa: S603 — input figé
            ["git", *args],
            capture_output=True,
            text=True,
            cwd=str(cwd),
            timeout=2,
            check=False,
        )
        if result.returncode != 0:
            return None
        return result.stdout.strip() or None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


# ═══════════════════════════════════════════════════════════════════
# FALLBACK 3 — lecture .git/HEAD direct
# ═══════════════════════════════════════════════════════════════════


def _from_git_head_file(root: Path | None) -> VersionInfo | None:
    """Lit `.git/HEAD` + `.git/refs/heads/<branch>` sans appeler git.

    Utile dans un container Docker qui a copié `.git/` mais pas le
    binaire git (ex: image Alpine slim sans git).
    """
    if root is None:
        return None
    git_dir = root / ".git"
    if not git_dir.is_dir():
        return None
    try:
        head_raw = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if head_raw.startswith("ref: "):
            ref_path = git_dir / head_raw[5:].strip()
            if not ref_path.exists():
                return None
            sha = ref_path.read_text(encoding="utf-8").strip()
        else:
            # detached HEAD — sha direct
            sha = head_raw
        if not sha or len(sha) < 8:
            return None
        return VersionInfo(
            version=sha[:8],
            commit_sha=sha[:40],
            tag=None,
            dirty=False,
            source="git_head_file",
        )
    except Exception as exc:  # noqa: BLE001 — fail-safe
        log.debug("version.head_file_failed", error=str(exc))
        return None


# ═══════════════════════════════════════════════════════════════════
# REPO ROOT DISCOVERY
# ═══════════════════════════════════════════════════════════════════


def _find_repo_root() -> Path | None:
    """Cherche le `.git/` en remontant depuis ce fichier (max 12 niveaux).

    Marche en dev (repo cloné) et en CI (workspace GitHub Actions).
    Retourne None si aucun `.git/` trouvé (cas Docker prod sans `.git/`).
    """
    cur = Path(__file__).resolve()
    for _ in range(12):
        if (cur.parent / ".git").exists():
            return cur.parent
        if cur.parent == cur:
            break
        cur = cur.parent
    return None
