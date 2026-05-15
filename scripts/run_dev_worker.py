"""Wrapper dev Windows-friendly pour arq worker.

Memes pieges Windows + Python 3.14 que run_dev_api.py
(SelectorEventLoop forcee via loop_factory + encoding UTF-8).

Pointe sur la classe `WorkerSettings` definie dans `workers/worker.py`.

Usage : .venv/Scripts/python.exe -m scripts.run_dev_worker
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


async def _run_worker_async() -> None:
    from arq.worker import async_check_health, create_worker
    from workers.worker import WorkerSettings

    worker = create_worker(WorkerSettings)
    try:
        await worker.async_run()
    finally:
        await worker.close()


def main() -> None:
    if sys.platform == "win32":
        asyncio.run(_run_worker_async(), loop_factory=_selector_loop_factory)
    else:
        asyncio.run(_run_worker_async())


if __name__ == "__main__":
    main()
