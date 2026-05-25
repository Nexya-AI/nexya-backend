"""UrlPreviewService — fetch Open Graph tags d'une URL côté serveur.

Pipeline strict 8 étapes :
1. Validation URL Pydantic (scheme http/https uniquement, déjà fait par
   `UrlPreviewRequest`).
2. Cache Redis `url_preview:{sha256(url)}` TTL 7j → si HIT retourne direct.
3. Anti-SSRF : résolution DNS + check IP non-privée
   (10.x/192.168.x/127.x/169.254.x/fe80::/fc00::/etc.).
4. httpx async GET avec timeout 5s + max 3 redirects + User-Agent NEXYA.
5. Cap content-length 500 KB (anti-DoS sur pages géantes).
6. Parse Open Graph + <title> + <link rel='icon'> via regex strict
   (pas de BeautifulSoup, anti-dep + parsing déterministe).
7. Sanitize HTML strip + cap len 200/300 chars (anti-XSS).
8. Cache Redis + return.

**Fail-safe absolu** : toute exception (DNS, timeout, parsing) → return None
+ log warning. Le router relance `UrlPreviewUnavailableException(503)`.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import json
import re
import socket
from datetime import UTC, datetime
from typing import Final
from urllib.parse import urljoin, urlparse

import httpx
import structlog

from app.config import settings
from app.core.database.redis import get_redis
from app.features.metadata.schemas import UrlPreviewResponse

log = structlog.get_logger(__name__)

# ── Constantes anti-SSRF + cache ──────────────────────────────────
_FETCH_TIMEOUT_SECONDS: Final[float] = 5.0
_MAX_CONTENT_BYTES: Final[int] = 500 * 1024  # 500 KB
_MAX_REDIRECTS: Final[int] = 3
_CACHE_TTL_SECONDS: Final[int] = 7 * 24 * 3600  # 7 jours
_CACHE_KEY_PREFIX: Final[str] = "url_preview:"
_USER_AGENT: Final[str] = "Mozilla/5.0 (compatible; NEXYA-URLPreview/1.0; +https://nexya.ai/bot)"

# ── Regex parsing OG (strict, anti-greedy, case-insensitive) ──────
_META_TAG_RE = re.compile(
    r"<meta\s+[^>]*?>",
    re.IGNORECASE | re.DOTALL,
)
_PROPERTY_RE = re.compile(
    r"""(?:property|name)\s*=\s*["']([^"']+)["']""",
    re.IGNORECASE,
)
_CONTENT_RE = re.compile(
    r"""content\s*=\s*["']([^"']*)["']""",
    re.IGNORECASE,
)
_TITLE_RE = re.compile(
    r"<title[^>]*>(.*?)</title>",
    re.IGNORECASE | re.DOTALL,
)
_LINK_ICON_RE = re.compile(
    r"""<link\s+[^>]*?rel\s*=\s*["'](?:icon|shortcut\s+icon|apple-touch-icon)["'][^>]*?>""",
    re.IGNORECASE | re.DOTALL,
)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_HTML_STRIP_RE = re.compile(r"<[^>]+>")


class UrlPreviewService:
    """Service stateless de preview d'URLs avec anti-SSRF + cache Redis."""

    @staticmethod
    async def preview(url: str) -> UrlPreviewResponse | None:
        """Récupère le preview d'une URL. Retourne None sur échec.

        Pipeline strict — voir docstring module.
        """
        # 1. Cache lookup
        cache_key = _CACHE_KEY_PREFIX + _sha256_hex(url)
        cached = await _cache_get(cache_key)
        if cached is not None:
            cached["from_cache"] = True
            try:
                return UrlPreviewResponse(**cached)
            except Exception:  # noqa: BLE001 — corrupted cache entry
                log.warning("url_preview.cache_corrupted", url=url)

        # 2. Anti-SSRF check
        if not await _is_url_safe(url):
            log.warning("url_preview.ssrf_rejected", url=url)
            return None

        # 3. Fetch
        try:
            html, final_url = await _fetch_html(url)
        except Exception as exc:  # noqa: BLE001 — fail-safe absolu
            log.warning(
                "url_preview.fetch_failed",
                url=url,
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            return None

        if not html:
            log.info("url_preview.empty_html", url=url)
            return None

        # 4. Parse OG tags + title + favicon
        og_tags = _parse_og_tags(html)
        title_html = og_tags.get("og:title") or _parse_title_fallback(html)
        description = og_tags.get("og:description")
        og_image = og_tags.get("og:image")
        favicon_rel = _parse_favicon(html)

        # 5. Resolve relative URLs to absolute (vs final_url after redirects)
        og_image_abs = _resolve_url(og_image, final_url) if og_image else None
        favicon_abs = (
            _resolve_url(favicon_rel, final_url)
            if favicon_rel
            else _resolve_url("/favicon.ico", final_url)
        )

        # 6. Sanitize + cap
        title_clean = _sanitize_text(title_html, max_chars=200)
        desc_clean = _sanitize_text(description, max_chars=300)

        response = UrlPreviewResponse(
            url=url,
            title=title_clean,
            description=desc_clean,
            og_image_url=og_image_abs,
            favicon_url=favicon_abs,
            fetched_at=datetime.now(UTC),
            from_cache=False,
        )

        # 7. Cache
        await _cache_set(cache_key, response.model_dump(mode="json"))

        log.info(
            "url_preview.fetched",
            url=url,
            has_title=title_clean is not None,
            has_image=og_image_abs is not None,
        )
        return response


# ──────────────────────────────────────────────────────────────────
# Helpers anti-SSRF
# ──────────────────────────────────────────────────────────────────


async def _is_url_safe(url: str) -> bool:
    """Vérifie qu'une URL ne pointe pas vers une IP privée/loopback.

    Anti-SSRF strict :
      * Scheme ∈ {http, https} uniquement (déjà filtré par Pydantic HttpUrl).
      * Hostname résolu via socket.getaddrinfo (toutes les IPs, IPv4+IPv6).
      * Refus si TOUTE IP est privée/loopback/link-local/reserved.
    """
    try:
        parsed = urlparse(url)
    except Exception:  # noqa: BLE001
        return False

    if parsed.scheme not in ("http", "https"):
        return False

    hostname = parsed.hostname
    if not hostname:
        return False

    # Resolve DNS — getaddrinfo() runs in default executor (non-blocking)
    try:
        loop = asyncio.get_running_loop()
        infos = await loop.run_in_executor(None, _resolve_addresses_blocking, hostname)
    except Exception:  # noqa: BLE001 — DNS failure
        return False

    if not infos:
        return False

    # If ANY resolved IP is private/loopback/etc., reject the whole URL
    for ip_str in infos:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            return False

    return True


def _resolve_addresses_blocking(hostname: str) -> list[str]:
    """Résout un hostname vers toutes ses IPs (sync, à wrapper en executor)."""
    addresses: list[str] = []
    for family, _socktype, _proto, _canon, sockaddr in socket.getaddrinfo(hostname, None):
        if family in (socket.AF_INET, socket.AF_INET6):
            ip = sockaddr[0]
            addresses.append(ip)
    return addresses


# ──────────────────────────────────────────────────────────────────
# Helpers fetch + parsing
# ──────────────────────────────────────────────────────────────────


async def _fetch_html(url: str) -> tuple[str, str]:
    """Fetch HTML d'une URL avec cap content-length + timeout.

    Retourne (html, final_url) après les redirections (utile pour résoudre
    les og:image relatives).
    """
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.5",
        "Accept-Language": "fr,en;q=0.8",
    }
    timeout = httpx.Timeout(_FETCH_TIMEOUT_SECONDS, connect=2.0)
    async with httpx.AsyncClient(
        timeout=timeout,
        follow_redirects=True,
        max_redirects=_MAX_REDIRECTS,
        headers=headers,
    ) as client:
        async with client.stream("GET", url) as response:
            # Cap content-length declared by the server (anti-DoS).
            declared = response.headers.get("content-length")
            if declared and declared.isdigit() and int(declared) > _MAX_CONTENT_BYTES:
                return ("", str(response.url))

            content_type = response.headers.get("content-type", "")
            if "html" not in content_type.lower():
                return ("", str(response.url))

            # Stream body with hard cap
            buffer = bytearray()
            async for chunk in response.aiter_bytes(chunk_size=8192):
                buffer.extend(chunk)
                if len(buffer) > _MAX_CONTENT_BYTES:
                    buffer = buffer[:_MAX_CONTENT_BYTES]
                    break

            # Decode using server-declared charset, fallback utf-8
            charset = "utf-8"
            if "charset=" in content_type.lower():
                charset = content_type.lower().split("charset=", 1)[1].strip()
            try:
                html = buffer.decode(charset, errors="replace")
            except LookupError:
                html = buffer.decode("utf-8", errors="replace")

            return (html, str(response.url))


def _parse_og_tags(html: str) -> dict[str, str]:
    """Parse les balises <meta property='og:X' content='Y'>."""
    tags: dict[str, str] = {}
    # Limit search to <head> if present (perf + safety)
    head_match = re.search(
        r"<head[^>]*>(.*?)</head>",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    search_in = head_match.group(1) if head_match else html[:8000]
    for meta_tag in _META_TAG_RE.finditer(search_in):
        meta = meta_tag.group(0)
        prop_match = _PROPERTY_RE.search(meta)
        content_match = _CONTENT_RE.search(meta)
        if prop_match and content_match:
            prop = prop_match.group(1).lower()
            content = content_match.group(1)
            if prop.startswith("og:") and prop not in tags:
                tags[prop] = content
    return tags


def _parse_title_fallback(html: str) -> str | None:
    """Parse <title> en fallback si pas d'og:title."""
    match = _TITLE_RE.search(html)
    if not match:
        return None
    return match.group(1).strip()


def _parse_favicon(html: str) -> str | None:
    """Parse <link rel='icon' href='X'>."""
    head_match = re.search(
        r"<head[^>]*>(.*?)</head>",
        html,
        re.IGNORECASE | re.DOTALL,
    )
    search_in = head_match.group(1) if head_match else html[:8000]
    link_match = _LINK_ICON_RE.search(search_in)
    if not link_match:
        return None
    href_match = _HREF_RE.search(link_match.group(0))
    if not href_match:
        return None
    return href_match.group(1)


def _resolve_url(maybe_relative: str | None, base_url: str) -> str | None:
    """Résout une URL relative vers absolue."""
    if not maybe_relative:
        return None
    try:
        absolute = urljoin(base_url, maybe_relative)
        parsed = urlparse(absolute)
        if parsed.scheme not in ("http", "https"):
            return None
        return absolute
    except Exception:  # noqa: BLE001
        return None


def _sanitize_text(text: str | None, *, max_chars: int) -> str | None:
    """Strip HTML tags + collapse whitespace + cap chars."""
    if not text:
        return None
    stripped = _HTML_STRIP_RE.sub("", text)
    collapsed = " ".join(stripped.split())
    if not collapsed:
        return None
    if len(collapsed) > max_chars:
        return collapsed[:max_chars].rstrip() + "..."
    return collapsed


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ──────────────────────────────────────────────────────────────────
# Cache Redis (fail-safe absolu)
# ──────────────────────────────────────────────────────────────────


async def _cache_get(key: str) -> dict | None:
    if not settings.url_preview_cache_enabled:
        return None
    try:
        redis = get_redis()
        raw = await redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:  # noqa: BLE001 — Redis flap ne doit pas bloquer
        return None


async def _cache_set(key: str, value: dict) -> None:
    if not settings.url_preview_cache_enabled:
        return
    try:
        redis = get_redis()
        await redis.set(
            key,
            json.dumps(value, default=str),
            ex=_CACHE_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001 — cache rate ne bloque jamais
        pass
