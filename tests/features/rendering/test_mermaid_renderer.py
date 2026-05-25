"""Tests unitaires — `MermaidRenderer.render` (C4.3).

Mock httpx + Redis pour tester sans réseau ni cache externe.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from app.features.rendering import mermaid_renderer as renderer_module
from app.features.rendering.mermaid_renderer import (
    MermaidRenderer,
    MermaidRenderFailedError,
)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ──────────────────────────────────────────────────────────────────
# Happy path — Kroki répond 200 + SVG valide
# ──────────────────────────────────────────────────────────────────


class TestRenderHappy:
    @pytest.mark.asyncio
    async def test_happy_path_returns_svg_and_caches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        source = "graph TD; A-->B;"
        fake_svg = "<svg xmlns='http://www.w3.org/2000/svg'><g/></svg>"

        async def fake_cache_get(_k: str) -> dict | None:
            return None

        cache_writes: list[tuple[str, dict]] = []

        async def fake_cache_set(key: str, value: dict) -> None:
            cache_writes.append((key, value))

        class _FakeResponse:
            status_code = 200
            text = fake_svg

        class _FakeAsyncClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, _url: str, **_kwargs) -> _FakeResponse:
                return _FakeResponse()

        monkeypatch.setattr(renderer_module, "_cache_get", fake_cache_get)
        monkeypatch.setattr(renderer_module, "_cache_set", fake_cache_set)
        monkeypatch.setattr(renderer_module.httpx, "AsyncClient", _FakeAsyncClient)

        result = await MermaidRenderer.render(source)
        assert result.svg == fake_svg
        assert result.sha256 == _sha(source)
        assert result.from_cache is False
        assert len(cache_writes) == 1
        assert cache_writes[0][0].startswith("mermaid:")

    @pytest.mark.asyncio
    async def test_cache_hit_returns_svg_without_kroki_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        source = "sequenceDiagram; A->>B: hi"
        cached_svg = "<svg>cached</svg>"

        async def fake_cache_get(_k: str) -> dict | None:
            return {
                "svg": cached_svg,
                "sha256": _sha(source),
                "fetched_at": "2026-05-24T12:00:00+00:00",
                "from_cache": False,
            }

        class _BoomClient:
            def __init__(self, **_kwargs):
                raise AssertionError("Kroki must NOT be called on cache hit")

        monkeypatch.setattr(renderer_module, "_cache_get", fake_cache_get)
        monkeypatch.setattr(renderer_module.httpx, "AsyncClient", _BoomClient)

        result = await MermaidRenderer.render(source)
        assert result.svg == cached_svg
        assert result.from_cache is True
        assert result.sha256 == _sha(source)


# ──────────────────────────────────────────────────────────────────
# Fail-safe — Kroki down / 5xx / invalid SVG
# ──────────────────────────────────────────────────────────────────


class TestRenderFailSafe:
    @pytest.mark.asyncio
    async def test_kroki_timeout_raises_failed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_cache_get(_k: str) -> dict | None:
            return None

        class _TimeoutClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, _url: str, **_kwargs):
                raise httpx.TimeoutException("timeout")

        monkeypatch.setattr(renderer_module, "_cache_get", fake_cache_get)
        monkeypatch.setattr(renderer_module.httpx, "AsyncClient", _TimeoutClient)

        with pytest.raises(MermaidRenderFailedError):
            await MermaidRenderer.render("graph TD; A-->B")

    @pytest.mark.asyncio
    async def test_kroki_500_raises_failed_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_cache_get(_k: str) -> dict | None:
            return None

        class _FakeResponse:
            status_code = 500
            text = "Internal Server Error"

        class _FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, _url: str, **_kwargs):
                return _FakeResponse()

        monkeypatch.setattr(renderer_module, "_cache_get", fake_cache_get)
        monkeypatch.setattr(renderer_module.httpx, "AsyncClient", _FakeClient)

        with pytest.raises(MermaidRenderFailedError):
            await MermaidRenderer.render("graph TD; A-->B")

    @pytest.mark.asyncio
    async def test_kroki_returns_invalid_svg_raises_failed_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        async def fake_cache_get(_k: str) -> dict | None:
            return None

        class _FakeResponse:
            status_code = 200
            text = "not an svg"

        class _FakeClient:
            def __init__(self, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def post(self, _url: str, **_kwargs):
                return _FakeResponse()

        monkeypatch.setattr(renderer_module, "_cache_get", fake_cache_get)
        monkeypatch.setattr(renderer_module.httpx, "AsyncClient", _FakeClient)

        with pytest.raises(MermaidRenderFailedError):
            await MermaidRenderer.render("graph TD; A-->B")


# ──────────────────────────────────────────────────────────────────
# SHA256 déterministe
# ──────────────────────────────────────────────────────────────────


class TestSha256:
    @pytest.mark.asyncio
    async def test_sha256_is_deterministic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Même source → même sha256 → même clé de cache."""
        source = "graph TD; A-->B; B-->C;"

        async def fake_cache_get(_k: str) -> dict | None:
            return {
                "svg": "<svg/>",
                "sha256": _sha(source),
                "fetched_at": "2026-05-24T12:00:00+00:00",
                "from_cache": False,
            }

        class _BoomClient:
            def __init__(self, **_kwargs):
                raise AssertionError("should not be called")

        monkeypatch.setattr(renderer_module, "_cache_get", fake_cache_get)
        monkeypatch.setattr(renderer_module.httpx, "AsyncClient", _BoomClient)

        r1 = await MermaidRenderer.render(source)
        r2 = await MermaidRenderer.render(source)
        assert r1.sha256 == r2.sha256
