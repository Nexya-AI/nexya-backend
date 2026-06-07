"""
Tests Bug Image célébrités (2026-06-06) — `ReplicateImageProvider`
fallback Flux 1.1 Pro avec httpx mocké.

Couvre 7 scénarios critiques :
1. Token absent → ProviderAuthError fail-fast
2. Happy path : POST → prediction succeeded → download → GeneratedImage
3. 401 / 403 → ProviderAuthError (token invalide)
4. 429 + Retry-After → ProviderRateLimitError avec retry_after_seconds
5. Prediction status=failed avec "safety" → ProviderContentFilteredError
6. Prediction status=failed sans safety → ProviderUnavailableError
7. 5xx → ProviderUnavailableError
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest

from app.ai.providers.base import (
    GeneratedImage,
    ImageGenerationRequest,
    ProviderAuthError,
    ProviderContentFilteredError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)
from app.ai.providers.replicate_provider import ReplicateImageProvider


def _make_request(prompt: str = "Mark Zuckerberg in a Maybach") -> ImageGenerationRequest:
    return ImageGenerationRequest(
        prompt=prompt,
        count=1,
        aspect_ratio="1:1",
        user_id="user-test",
        trace_id="trace-test",
        expert_id="studio",
    )


def _fake_response(
    status_code: int,
    json_body: Any = None,
    headers: dict[str, str] | None = None,
    content: bytes = b"",
) -> MagicMock:
    """Construit une réponse httpx mockée."""
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status_code
    resp.json = MagicMock(return_value=json_body or {})
    resp.headers = headers or {}
    resp.text = str(json_body) if json_body else ""
    resp.content = content
    resp.raise_for_status = MagicMock()
    if status_code >= 400:
        resp.raise_for_status.side_effect = httpx.HTTPStatusError(
            "boom", request=MagicMock(), response=resp
        )
    return resp


class _FakeAsyncClient:
    """Fake httpx.AsyncClient qui queue des réponses."""

    def __init__(self, responses: list[MagicMock]) -> None:
        self._responses = list(responses)
        self.calls: list[tuple[str, str]] = []  # (method, url)

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *args: Any) -> None:
        pass

    async def post(self, url: str, **kwargs: Any) -> MagicMock:
        self.calls.append(("POST", url))
        if not self._responses:
            raise AssertionError(f"No more responses queued for POST {url}")
        return self._responses.pop(0)

    async def get(self, url: str, **kwargs: Any) -> MagicMock:
        self.calls.append(("GET", url))
        if not self._responses:
            raise AssertionError(f"No more responses queued for GET {url}")
        return self._responses.pop(0)


def _patch_httpx_with_responses(
    monkeypatch: pytest.MonkeyPatch,
    responses: list[MagicMock],
) -> _FakeAsyncClient:
    """Patche `httpx.AsyncClient` dans le module replicate_provider."""
    fake = _FakeAsyncClient(responses)

    def _factory(*args: Any, **kwargs: Any) -> _FakeAsyncClient:
        return fake

    monkeypatch.setattr(
        "app.ai.providers.replicate_provider.httpx.AsyncClient",
        _factory,
    )

    # Court-circuite asyncio.sleep pour ne pas attendre le backoff polling.
    async def _instant_sleep(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(
        "app.ai.providers.replicate_provider.asyncio.sleep",
        _instant_sleep,
    )
    return fake


# ══════════════════════════════════════════════════════════════
# 1. Token absent → fail-fast
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_no_token_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sans REPLICATE_API_TOKEN, le provider lève ProviderAuthError clair."""
    from app.config import settings

    monkeypatch.setattr(settings, "replicate_api_token", "", raising=False)

    provider = ReplicateImageProvider()
    with pytest.raises(ProviderAuthError) as exc_info:
        await provider.generate_images(_make_request())

    assert "REPLICATE_API_TOKEN" in str(exc_info.value)
    assert exc_info.value.provider == "replicate-flux"


# ══════════════════════════════════════════════════════════════
# 2. Happy path : succeeded → download → GeneratedImage
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_happy_path_returns_generated_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Pipeline complet : POST 201 succeeded inline + download → GeneratedImage."""
    from app.config import settings

    monkeypatch.setattr(settings, "replicate_api_token", "r8_fake_token", raising=False)
    monkeypatch.setattr(settings, "replicate_safety_tolerance", 6, raising=False)

    # POST returns prediction déjà succeeded (Prefer: wait a marché).
    post_resp = _fake_response(
        201,
        json_body={
            "id": "pred-123",
            "status": "succeeded",
            "output": "https://replicate.delivery/img-123.jpg",
        },
    )
    # GET image bytes.
    fake_image_bytes = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
    img_resp = _fake_response(200, content=fake_image_bytes)
    img_resp.raise_for_status = MagicMock()  # 200 → pas de raise

    _patch_httpx_with_responses(monkeypatch, [post_resp, img_resp])

    provider = ReplicateImageProvider()
    images = await provider.generate_images(_make_request())

    assert len(images) == 1
    assert isinstance(images[0], GeneratedImage)
    assert images[0].mime_type == "image/jpeg"
    # base64 decode du résultat doit donner les bytes originaux
    import base64

    decoded = base64.b64decode(images[0].base64_data)
    assert decoded == fake_image_bytes


# ══════════════════════════════════════════════════════════════
# 3. 401 / 403 → ProviderAuthError
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_401_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "replicate_api_token", "r8_bad_token", raising=False)
    monkeypatch.setattr(settings, "replicate_safety_tolerance", 6, raising=False)

    post_resp = _fake_response(401, json_body={"detail": "invalid token"})
    _patch_httpx_with_responses(monkeypatch, [post_resp])

    provider = ReplicateImageProvider()
    with pytest.raises(ProviderAuthError):
        await provider.generate_images(_make_request())


@pytest.mark.asyncio
async def test_403_raises_auth_error(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "replicate_api_token", "r8_fake", raising=False)
    monkeypatch.setattr(settings, "replicate_safety_tolerance", 6, raising=False)

    post_resp = _fake_response(403, json_body={"detail": "forbidden"})
    _patch_httpx_with_responses(monkeypatch, [post_resp])

    provider = ReplicateImageProvider()
    with pytest.raises(ProviderAuthError):
        await provider.generate_images(_make_request())


# ══════════════════════════════════════════════════════════════
# 4. 429 + Retry-After → ProviderRateLimitError avec retry_after
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_429_raises_rate_limit_with_retry_after(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "replicate_api_token", "r8_fake", raising=False)
    monkeypatch.setattr(settings, "replicate_safety_tolerance", 6, raising=False)

    post_resp = _fake_response(
        429,
        json_body={"detail": "rate limit exceeded"},
        headers={"Retry-After": "30"},
    )
    _patch_httpx_with_responses(monkeypatch, [post_resp])

    provider = ReplicateImageProvider()
    with pytest.raises(ProviderRateLimitError) as exc_info:
        await provider.generate_images(_make_request())

    assert exc_info.value.retry_after_seconds == 30.0


# ══════════════════════════════════════════════════════════════
# 5. Prediction failed avec safety → ProviderContentFilteredError
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_prediction_failed_safety_raises_content_filtered(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "replicate_api_token", "r8_fake", raising=False)
    monkeypatch.setattr(settings, "replicate_safety_tolerance", 6, raising=False)

    # POST returns prediction failed avec mention safety dans error.
    post_resp = _fake_response(
        201,
        json_body={
            "id": "pred-xyz",
            "status": "failed",
            "error": "NSFW content detected by safety filter — blocked.",
        },
    )
    _patch_httpx_with_responses(monkeypatch, [post_resp])

    provider = ReplicateImageProvider()
    with pytest.raises(ProviderContentFilteredError):
        await provider.generate_images(_make_request())


# ══════════════════════════════════════════════════════════════
# 6. Prediction failed sans safety → ProviderUnavailableError
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_prediction_failed_other_raises_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "replicate_api_token", "r8_fake", raising=False)
    monkeypatch.setattr(settings, "replicate_safety_tolerance", 6, raising=False)

    post_resp = _fake_response(
        201,
        json_body={
            "id": "pred-xyz",
            "status": "failed",
            "error": "Out of memory on GPU node — model crashed.",
        },
    )
    _patch_httpx_with_responses(monkeypatch, [post_resp])

    provider = ReplicateImageProvider()
    with pytest.raises(ProviderUnavailableError):
        await provider.generate_images(_make_request())


# ══════════════════════════════════════════════════════════════
# 7. 5xx → ProviderUnavailableError
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_5xx_raises_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "replicate_api_token", "r8_fake", raising=False)
    monkeypatch.setattr(settings, "replicate_safety_tolerance", 6, raising=False)

    post_resp = _fake_response(503, json_body={"detail": "service unavailable"})
    _patch_httpx_with_responses(monkeypatch, [post_resp])

    provider = ReplicateImageProvider()
    with pytest.raises(ProviderUnavailableError) as exc_info:
        await provider.generate_images(_make_request())

    assert exc_info.value.status_code == 503


# ══════════════════════════════════════════════════════════════
# Bonus : provider attributes & metadata
# ══════════════════════════════════════════════════════════════


def test_provider_identity() -> None:
    """Le provider expose les attributs attendus par le LlmRouter."""
    provider = ReplicateImageProvider()
    assert provider.name == "replicate-flux"
    assert provider.default_model == "black-forest-labs/flux-1.1-pro"
    assert "black-forest-labs/flux-1.1-pro" in provider.supported_models
    assert "black-forest-labs/flux-schnell" in provider.supported_models
    assert provider.max_images_per_call == 4


def test_provider_accepts_model_override() -> None:
    """Le provider accepte un model override au constructor (swap flux-schnell)."""
    provider = ReplicateImageProvider(
        api_token="r8_override",
        default_model="black-forest-labs/flux-schnell",
    )
    assert provider.default_model == "black-forest-labs/flux-schnell"
