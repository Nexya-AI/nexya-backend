"""Tests unitaires — `GeminiImageProvider` (Imagen 4) avec SDK `google.genai` mocké.

Couvre le fix terrain 2026-06-22 :
- `safetyFilterLevel` adapté au mode (AI Studio n'accepte QUE
  `block_low_and_above` ; les niveaux permissifs sont réservés à Vertex AI).
- Un 400 INVALID_ARGUMENT « ... is supported for safetySetting » est classé
  comme erreur de config (`ProviderInvalidRequestError`), PAS comme filtre de
  contenu (sinon fallback Replicate trompeur + log `imagen_filtered_no_fallback`).
"""

from __future__ import annotations

import sys
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai.providers import gemini as gemini_module
from app.ai.providers.base import (
    ImageGenerationRequest,
    ProviderContentFilteredError,
    ProviderInvalidRequestError,
)
from app.ai.providers.gemini import GeminiImageProvider, _map_sdk_exception


@pytest.fixture(autouse=True)
def _reset_gemini_client():
    """Isolation stricte du singleton client gemini.py (partagé chat + image)."""
    gemini_module._reset_client_for_tests()
    yield
    gemini_module._reset_client_for_tests()


def _install_fake_genai(monkeypatch: pytest.MonkeyPatch, *, gemini_use_vertex: bool = False):
    """Installe un faux module google.genai + types qui capture la config Imagen."""
    types_mod = MagicMock()
    # GenerateImagesConfig enregistre ses kwargs pour qu'on inspecte
    # safetyFilterLevel après l'appel.
    types_mod.GenerateImagesConfig = MagicMock(side_effect=lambda **kwargs: ("config", kwargs))

    # Fausse réponse : 1 image avec des bytes décodables en base64.
    fake_img = MagicMock()
    fake_img.image = MagicMock()
    fake_img.image.image_bytes = b"\x89PNG-fake-bytes"
    response = MagicMock()
    response.generated_images = [fake_img]

    client = MagicMock()
    client.aio = MagicMock()
    client.aio.models = MagicMock()
    client.aio.models.generate_images = AsyncMock(return_value=response)

    genai_mod = MagicMock()
    genai_mod.Client = MagicMock(return_value=client)
    genai_mod.types = types_mod

    google_mod = MagicMock()
    google_mod.genai = genai_mod

    monkeypatch.setitem(sys.modules, "google", google_mod)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", types_mod)

    gemini_module._reset_client_for_tests()

    from app.config import settings

    monkeypatch.setattr(settings, "gemini_api_key", "fake-key", raising=False)
    monkeypatch.setattr(settings, "gemini_use_vertex", gemini_use_vertex, raising=False)
    monkeypatch.setattr(settings, "gcp_project_id", "nexya-ai", raising=False)
    monkeypatch.setattr(settings, "gcp_region", "us-central1", raising=False)

    return client, types_mod


def _img_request() -> ImageGenerationRequest:
    return ImageGenerationRequest(
        prompt="un chat roux dans un jardin",
        count=1,
        user_id="u",
        trace_id="t",
        expert_id="studio",
    )


@pytest.mark.asyncio
async def test_ai_studio_uses_block_low_and_above(monkeypatch: pytest.MonkeyPatch) -> None:
    """AI Studio (gemini_use_vertex=False) → safetyFilterLevel=block_low_and_above.

    C'est le cas prod NEXYA. Avant le fix, `block_only_high` provoquait un 400
    INVALID_ARGUMENT et toute génération échouait.
    """
    _client, types_mod = _install_fake_genai(monkeypatch, gemini_use_vertex=False)
    provider = GeminiImageProvider()
    images = await provider.generate_images(_img_request())

    assert len(images) == 1
    cfg_kwargs = types_mod.GenerateImagesConfig.call_args.kwargs
    assert cfg_kwargs["safetyFilterLevel"] == "block_low_and_above"


@pytest.mark.asyncio
async def test_vertex_uses_block_only_high(monkeypatch: pytest.MonkeyPatch) -> None:
    """Vertex AI (gemini_use_vertex=True) → safetyFilterLevel=block_only_high (permissif)."""
    _client, types_mod = _install_fake_genai(monkeypatch, gemini_use_vertex=True)
    provider = GeminiImageProvider()
    await provider.generate_images(_img_request())

    cfg_kwargs = types_mod.GenerateImagesConfig.call_args.kwargs
    assert cfg_kwargs["safetyFilterLevel"] == "block_only_high"


def test_map_400_param_error_is_invalid_request_not_content_filter() -> None:
    """Le 400 « Only block_low_and_above is supported for safetySetting » est une
    erreur de CONFIG → ProviderInvalidRequestError (pas ContentFiltered)."""

    class FakeClientError(Exception):
        code = 400

    exc = FakeClientError(
        "400 INVALID_ARGUMENT. Only block_low_and_above is supported for safetySetting."
    )
    mapped = _map_sdk_exception(exc, model="imagen-4.0-generate-001")
    assert isinstance(mapped, ProviderInvalidRequestError)


def test_map_400_real_safety_block_stays_content_filtered() -> None:
    """Un vrai blocage de contenu (« blocked by safety ») reste ContentFiltered."""

    class FakeClientError(Exception):
        code = 400

    exc = FakeClientError("400 the request was blocked by safety filters")
    mapped = _map_sdk_exception(exc, model="imagen-4.0-generate-001")
    assert isinstance(mapped, ProviderContentFilteredError)
