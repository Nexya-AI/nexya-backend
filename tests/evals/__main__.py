"""Entry point for `python -m tests.evals` (delegates to cli.main)."""

from __future__ import annotations

from tests.evals.cli import main

if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
