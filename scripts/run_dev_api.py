"""Wrapper dev Windows-friendly pour uvicorn.

Corrige deux pieges Windows + Python 3.14 :
  1. Sur Windows + Py 3.14, asyncio.run cree son propre loop via Runner
     interne et ignore set_event_loop_policy. uvicorn appelle
     asyncio.run(server.serve(), loop_factory=config.get_loop_factory()),
     donc on doit lui passer notre propre loop_factory pour forcer
     SelectorEventLoop (psycopg async refuse ProactorEventLoop par defaut).
  2. stdout/stderr cp1252 ne supportent pas les emojis des logs structlog
     (UnicodeEncodeError). On force UTF-8.

Usage : .venv/Scripts/python.exe -m scripts.run_dev_api
        (mode -m obligatoire pour que workers/ soit dans sys.path)
"""

from __future__ import annotations

import asyncio
import selectors
import sys


def _patch_windows_io() -> None:
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass


_patch_windows_io()


def _selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Force SelectorEventLoop sur Windows pour psycopg async."""
    return asyncio.SelectorEventLoop(selectors.SelectSelector())


def main() -> None:
    import uvicorn

    config = uvicorn.Config(
        "app.main:app",
        host="0.0.0.0",
        port=8000,
        log_level="info",
        loop="asyncio",
    )
    server = uvicorn.Server(config)

    if sys.platform == "win32":
        asyncio.run(server.serve(), loop_factory=_selector_loop_factory)
    else:
        server.run()


if __name__ == "__main__":
    main()
