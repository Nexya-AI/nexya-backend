"""MermaidRenderer — délègue le rendu Mermaid à Kroki.io.

Pipeline strict 4 étapes :
1. Cache Redis `mermaid:{sha256(source)}` TTL 7j → si HIT retourne direct.
2. httpx async POST `https://kroki.io/mermaid/svg` avec body=source plain
   text, timeout 10s, content-type `text/plain`.
3. Cache Redis (set + TTL).
4. Return SVG string.

**Fail-safe** : Kroki down / timeout / 5xx → raise `MermaidRenderFailedError`
(le router le mappe en 503 `MERMAID_RENDER_FAILED`).
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Final

import httpx
import structlog

from app.config import settings
from app.core.database.redis import get_redis
from app.features.rendering.schemas import MermaidRenderResponse

log = structlog.get_logger(__name__)

_RENDER_TIMEOUT_SECONDS: Final[float] = 10.0
_CACHE_TTL_SECONDS: Final[int] = 7 * 24 * 3600
_CACHE_KEY_PREFIX: Final[str] = "mermaid:"


class MermaidRenderFailedError(Exception):
    """Levée si Kroki est inaccessible / retourne une erreur / timeout."""


class MermaidRenderer:
    """Renderer stateless qui délègue à Kroki.io avec cache Redis."""

    @staticmethod
    async def render(source: str) -> MermaidRenderResponse:
        """Rend un diagramme Mermaid en SVG.

        Pipeline strict — voir docstring module. Raise
        `MermaidRenderFailedError` sur tout échec irrécupérable.
        """
        sha = _sha256_hex(source)
        cache_key = _CACHE_KEY_PREFIX + sha

        # 1. Cache lookup
        cached = await _cache_get(cache_key)
        if cached is not None:
            try:
                response = MermaidRenderResponse(**cached)
                response = response.model_copy(update={"from_cache": True})
                return response
            except Exception:  # noqa: BLE001 — cache corrompu
                log.warning("mermaid.cache_corrupted", sha256=sha[:8])

        # 2. Fetch Kroki.io
        url = f"{settings.kroki_base_url.rstrip('/')}/mermaid/svg"
        try:
            async with httpx.AsyncClient(timeout=_RENDER_TIMEOUT_SECONDS) as client:
                response_kroki = await client.post(
                    url,
                    content=source,
                    headers={"Content-Type": "text/plain"},
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            log.warning(
                "mermaid.kroki_unreachable",
                error_type=type(exc).__name__,
                error=str(exc)[:200],
            )
            raise MermaidRenderFailedError("Kroki.io unreachable") from exc

        if response_kroki.status_code != 200:
            log.warning(
                "mermaid.kroki_error",
                status=response_kroki.status_code,
                body_preview=response_kroki.text[:200],
            )
            raise MermaidRenderFailedError(f"Kroki returned {response_kroki.status_code}")

        svg = response_kroki.text
        if not svg or "<svg" not in svg.lower():
            raise MermaidRenderFailedError("Kroki returned empty/invalid SVG")

        # 3. Build response + cache
        result = MermaidRenderResponse(
            svg=svg,
            sha256=sha,
            fetched_at=datetime.now(UTC),
            from_cache=False,
        )
        await _cache_set(cache_key, result.model_dump(mode="json"))

        log.info("mermaid.rendered", sha256=sha[:8], size_bytes=len(svg))
        return result


def _sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ──────────────────────────────────────────────────────────────────
# Cache Redis (fail-safe absolu)
# ──────────────────────────────────────────────────────────────────


async def _cache_get(key: str) -> dict | None:
    if not settings.mermaid_cache_enabled:
        return None
    try:
        redis = get_redis()
        raw = await redis.get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception:  # noqa: BLE001 — Redis flap ne bloque pas
        return None


async def _cache_set(key: str, value: dict) -> None:
    if not settings.mermaid_cache_enabled:
        return
    try:
        redis = get_redis()
        await redis.set(
            key,
            json.dumps(value, default=str),
            ex=_CACHE_TTL_SECONDS,
        )
    except Exception:  # noqa: BLE001
        pass
