"""L1 — Validation structure du Makefile."""

from __future__ import annotations

import re
from pathlib import Path

MAKEFILE = Path(__file__).resolve().parents[1] / "Makefile"

# Tranche 1 actée 2026-04-26 : 16 targets confirmés.
EXPECTED_TARGETS = {
    "help",
    "install",
    "test",
    "test-fast",
    "lint",
    "format",
    "typecheck",
    "security",
    "build",
    "run",
    "migrate",
    "seed",
    "coverage",
    "clean",
    "ci",
    "check",
}

TARGET_REGEX = re.compile(r"^([a-zA-Z][a-zA-Z_-]*?):\s*(?:[^=]|$)")


def _read_makefile() -> str:
    return MAKEFILE.read_text(encoding="utf-8")


def _extract_targets() -> set[str]:
    """Parse les targets `name:` (en début de ligne)."""
    targets: set[str] = set()
    for line in _read_makefile().splitlines():
        # Ignore les lignes qui ressemblent à des targets dans .PHONY ou commentaires
        if line.startswith("#") or line.startswith("\t"):
            continue
        match = TARGET_REGEX.match(line)
        if match:
            name = match.group(1)
            # Ignore les variables Make (X = Y) — déjà filtré par regex
            # mais double-check.
            if "=" not in line.split(":", 1)[0]:
                targets.add(name)
    return targets


def test_makefile_exists():
    assert MAKEFILE.is_file(), "Makefile manquant à la racine"


def test_makefile_has_exactly_16_targets():
    """Tranche 1 (2026-04-26) : 16 targets, ni plus ni moins."""
    targets = _extract_targets()
    # On exclut les targets implicites qui auraient pu être capturés
    # par la regex sur des lignes mal parsées (`.PHONY:` etc.).
    relevant = targets - {".PHONY", ".DEFAULT_GOAL"}
    assert relevant == EXPECTED_TARGETS, (
        f"Targets mismatch: actual={relevant} expected={EXPECTED_TARGETS}"
    )


def test_each_target_has_help_comment():
    """Chaque target doit avoir un commentaire `## ...` pour `make help`."""
    content = _read_makefile()
    for target in EXPECTED_TARGETS:
        # Pattern : `target:\s+## help text`
        pattern = re.compile(
            rf"^{re.escape(target)}:\s*[^#]*##\s+\S",
            re.MULTILINE,
        )
        assert pattern.search(content), f"Target '{target}' n'a pas de commentaire ## help"


def test_phony_declared():
    """`.PHONY:` doit déclarer les targets non-fichiers (anti-collision
    avec un fichier homonyme dans le repo)."""
    content = _read_makefile()
    assert ".PHONY:" in content


def test_make_ci_chains_lint_typecheck_security_test():
    """`make ci` doit enchaîner les 4 checks via $(MAKE) <target>."""
    content = _read_makefile()
    # Trouve le bloc de la target `ci:`
    ci_block_match = re.search(
        r"^ci:.*?(?=^[a-zA-Z][a-zA-Z_-]*:|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    assert ci_block_match
    ci_block = ci_block_match.group(0)
    for sub_target in ("lint", "typecheck", "security", "test"):
        assert sub_target in ci_block, f"`make ci` n'invoque pas '{sub_target}'"


def test_default_goal_is_help():
    """`make` (sans argument) doit afficher l'aide, pas lancer une
    target destructive par accident."""
    content = _read_makefile()
    assert ".DEFAULT_GOAL := help" in content


def test_help_uses_grep_awk_for_auto_doc():
    """La target `help` génère l'aide automatiquement depuis les
    commentaires `## ...` (pas une liste hardcodée à maintenir)."""
    content = _read_makefile()
    help_block_match = re.search(
        r"^help:.*?(?=^[a-zA-Z][a-zA-Z_-]*:|\Z)",
        content,
        re.MULTILINE | re.DOTALL,
    )
    assert help_block_match
    help_block = help_block_match.group(0)
    assert "grep" in help_block
    assert "awk" in help_block
