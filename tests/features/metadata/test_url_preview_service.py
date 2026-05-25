"""Tests unitaires — `UrlPreviewService.preview` (C4.2).

Mock `httpx.AsyncClient`, le résolveur DNS, et Redis pour tester sans réseau.
"""

from __future__ import annotations

import pytest

from app.features.metadata import url_preview_service as svc_module
from app.features.metadata.url_preview_service import (
    UrlPreviewService,
    _is_url_safe,
    _parse_favicon,
    _parse_og_tags,
    _parse_title_fallback,
    _resolve_url,
    _sanitize_text,
)

# ──────────────────────────────────────────────────────────────────
# Pure helpers — tests rapides sans mock
# ──────────────────────────────────────────────────────────────────


class TestPureHelpers:
    def test_sanitize_text_strips_html(self) -> None:
        assert _sanitize_text("<p>Hello <b>world</b></p>", max_chars=100) == "Hello world"

    def test_sanitize_text_collapses_whitespace(self) -> None:
        assert (
            _sanitize_text("  Hello   world  \n\n  again  ", max_chars=100) == "Hello world again"
        )

    def test_sanitize_text_caps_chars_with_ellipsis(self) -> None:
        result = _sanitize_text("a" * 50, max_chars=10)
        assert result is not None
        assert result.endswith("...")
        assert len(result) <= 13  # 10 + "..."

    def test_sanitize_text_returns_none_for_empty(self) -> None:
        assert _sanitize_text(None, max_chars=100) is None
        assert _sanitize_text("", max_chars=100) is None
        assert _sanitize_text("   ", max_chars=100) is None

    def test_parse_og_tags_extracts_og_only(self) -> None:
        html = """
        <head>
        <meta property="og:title" content="Hello">
        <meta property="og:description" content="World">
        <meta name="og:image" content="https://example.com/img.png">
        <meta name="description" content="Not og">
        </head>
        """
        tags = _parse_og_tags(html)
        assert tags["og:title"] == "Hello"
        assert tags["og:description"] == "World"
        assert tags["og:image"] == "https://example.com/img.png"
        assert "description" not in tags

    def test_parse_title_fallback(self) -> None:
        assert (
            _parse_title_fallback("<html><head><title>My Page</title></head></html>") == "My Page"
        )
        assert _parse_title_fallback("<html>no title</html>") is None

    def test_parse_favicon(self) -> None:
        html = '<head><link rel="icon" href="/favicon.png"></head>'
        assert _parse_favicon(html) == "/favicon.png"
        html2 = '<head><link rel="shortcut icon" href="https://cdn.example.com/fav.ico"></head>'
        assert _parse_favicon(html2) == "https://cdn.example.com/fav.ico"
        assert _parse_favicon("<head>no link</head>") is None

    def test_resolve_url_relative_to_absolute(self) -> None:
        assert _resolve_url("/img.png", "https://example.com/page") == "https://example.com/img.png"
        assert (
            _resolve_url("https://cdn.example.com/img.png", "https://example.com/page")
            == "https://cdn.example.com/img.png"
        )
        assert _resolve_url(None, "https://example.com") is None
        # Rejette scheme non-http(s)
        assert _resolve_url("javascript:alert(1)", "https://example.com") is None


# ──────────────────────────────────────────────────────────────────
# Anti-SSRF — pivot critique sécurité
# ──────────────────────────────────────────────────────────────────


class TestAntiSSRF:
    @pytest.mark.asyncio
    async def test_rejects_private_ipv4(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Force DNS resolution to return private IP
        def fake_resolve(hostname: str) -> list[str]:
            return ["10.0.0.1"]

        monkeypatch.setattr(svc_module, "_resolve_addresses_blocking", fake_resolve)
        assert await _is_url_safe("http://attacker.example.com") is False

    @pytest.mark.asyncio
    async def test_rejects_loopback(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(svc_module, "_resolve_addresses_blocking", lambda h: ["127.0.0.1"])
        assert await _is_url_safe("http://attacker.example.com") is False

    @pytest.mark.asyncio
    async def test_rejects_link_local(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # AWS metadata IP
        monkeypatch.setattr(
            svc_module, "_resolve_addresses_blocking", lambda h: ["169.254.169.254"]
        )
        assert await _is_url_safe("http://aws-metadata.attacker.com") is False

    @pytest.mark.asyncio
    async def test_accepts_public_ipv4(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(svc_module, "_resolve_addresses_blocking", lambda h: ["8.8.8.8"])
        assert await _is_url_safe("https://wikipedia.org") is True

    @pytest.mark.asyncio
    async def test_rejects_dns_failure(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(_h: str) -> list[str]:
            raise Exception("DNS lookup failed")

        monkeypatch.setattr(svc_module, "_resolve_addresses_blocking", boom)
        assert await _is_url_safe("https://does-not-exist.example") is False

    @pytest.mark.asyncio
    async def test_rejects_mixed_private_and_public_ips(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Si UNE IP résolue est privée, on rejette TOUT (anti rebind DNS)
        monkeypatch.setattr(
            svc_module, "_resolve_addresses_blocking", lambda h: ["8.8.8.8", "10.0.0.1"]
        )
        assert await _is_url_safe("https://attacker.example.com") is False


# ──────────────────────────────────────────────────────────────────
# Service.preview — cache + happy path + fail-safe
# ──────────────────────────────────────────────────────────────────


class TestPreviewService:
    @pytest.mark.asyncio
    async def test_returns_none_when_ssrf_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_cache_get(_k: str) -> dict | None:
            return None

        async def fake_is_safe(_url: str) -> bool:
            return False

        monkeypatch.setattr(svc_module, "_cache_get", fake_cache_get)
        monkeypatch.setattr(svc_module, "_is_url_safe", fake_is_safe)
        result = await UrlPreviewService.preview("https://attacker.example.com/path")
        assert result is None

    @pytest.mark.asyncio
    async def test_cache_hit_returns_cached_without_fetch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cached_payload = {
            "url": "https://example.com",
            "title": "Cached Title",
            "description": "Cached desc",
            "og_image_url": None,
            "favicon_url": None,
            "fetched_at": "2026-05-24T12:00:00+00:00",
            "from_cache": False,
        }

        async def fake_cache_get(_k: str) -> dict | None:
            return cached_payload

        # Fail-safe : _is_url_safe ne doit PAS être appelé si cache hit
        async def fake_is_safe(_url: str) -> bool:
            raise AssertionError("should not be called on cache hit")

        async def fake_fetch(_url: str) -> tuple[str, str]:
            raise AssertionError("should not be called on cache hit")

        monkeypatch.setattr(svc_module, "_cache_get", fake_cache_get)
        monkeypatch.setattr(svc_module, "_is_url_safe", fake_is_safe)
        monkeypatch.setattr(svc_module, "_fetch_html", fake_fetch)

        result = await UrlPreviewService.preview("https://example.com")
        assert result is not None
        assert result.title == "Cached Title"
        assert result.from_cache is True

    @pytest.mark.asyncio
    async def test_happy_path_parses_og_tags_and_caches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        html = """
        <html><head>
        <meta property="og:title" content="My Page">
        <meta property="og:description" content="A great page">
        <meta property="og:image" content="/hero.png">
        <link rel="icon" href="/favicon.ico">
        </head><body></body></html>
        """

        async def fake_cache_get(_k: str) -> dict | None:
            return None

        async def fake_is_safe(_url: str) -> bool:
            return True

        async def fake_fetch(_url: str) -> tuple[str, str]:
            return (html, "https://example.com/page")

        cache_sets: list[tuple[str, dict]] = []

        async def fake_cache_set(key: str, value: dict) -> None:
            cache_sets.append((key, value))

        monkeypatch.setattr(svc_module, "_cache_get", fake_cache_get)
        monkeypatch.setattr(svc_module, "_is_url_safe", fake_is_safe)
        monkeypatch.setattr(svc_module, "_fetch_html", fake_fetch)
        monkeypatch.setattr(svc_module, "_cache_set", fake_cache_set)

        result = await UrlPreviewService.preview("https://example.com/page")
        assert result is not None
        assert result.title == "My Page"
        assert result.description == "A great page"
        assert result.og_image_url == "https://example.com/hero.png"
        assert result.favicon_url == "https://example.com/favicon.ico"
        assert result.from_cache is False
        # Cache écrit
        assert len(cache_sets) == 1

    @pytest.mark.asyncio
    async def test_fetch_failure_returns_none_safely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def fake_cache_get(_k: str) -> dict | None:
            return None

        async def fake_is_safe(_url: str) -> bool:
            return True

        async def boom_fetch(_url: str) -> tuple[str, str]:
            raise Exception("Timeout")

        monkeypatch.setattr(svc_module, "_cache_get", fake_cache_get)
        monkeypatch.setattr(svc_module, "_is_url_safe", fake_is_safe)
        monkeypatch.setattr(svc_module, "_fetch_html", boom_fetch)

        result = await UrlPreviewService.preview("https://example.com")
        assert result is None
