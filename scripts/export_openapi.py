"""
NEXYA Backend — Export OpenAPI schema (Session O2).

Dump le schéma OpenAPI 3.1 enrichi (post-O1) dans `docs/api/openapi.json`
pour audit DD, ingestion Postman/Insomnia, génération SDK clients V2,
et anti-drift CI (cf. `.github/workflows/dd-exports-fresh.yml`).

Usage :
    python -m scripts.export_openapi
    python -m scripts.export_openapi --out custom/path.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path


def _default_out_path() -> Path:
    return Path(__file__).resolve().parent.parent / "docs" / "api" / "openapi.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="export_openapi")
    parser.add_argument(
        "--out",
        type=Path,
        default=_default_out_path(),
        help="Chemin de sortie (défaut: docs/api/openapi.json).",
    )
    args = parser.parse_args(argv)

    # Windows : psycopg async refuse ProactorEventLoop (pattern aligné main.py)
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

    # Import différé pour ne pas charger l'app sans avoir parsé argv
    from app.main import app  # noqa: PLC0415

    schema = app.openapi()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(schema, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    paths_count = len(schema.get("paths", {}))
    tags_count = len(schema.get("tags", []))
    print(
        f"✅ OpenAPI exporté → {args.out} "
        f"({paths_count} endpoints, {tags_count} tags, "
        f"version {schema.get('openapi')})",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
