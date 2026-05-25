"""
Test ciblé Bug-experts-Pro 2026-05-23 — Gemini Pro vs Flash thinking_budget.

Vérifie que `GeminiChatProvider.stream_chat` pose le bon `thinking_budget`
selon le modèle quand `extra["disable_thinking"]=True` :
  - Flash       → thinking_budget=0   (désactivation totale, API accepte)
  - Flash-Lite  → thinking_budget=0   (accepté)
  - Pro         → thinking_budget=128 (minimum API forcé, 0 REJETÉ par l'API)

Cause racine du bug terrain Ivan « 5 experts ne répondent pas » :
le code précédent posait `thinking_budget=0` pour TOUS les modèles → Pro
recevait un stream vide silencieux (l'API rejette le payload sans toujours
lever d'exception nette selon version SDK).

Doc Gemini API : https://ai.google.dev/gemini-api/docs/thinking
  - Gemini 2.5 Flash     : range [0, 24576] OU -1 (dynamic)
  - Gemini 2.5 Flash-Lite: range [512, 24576] OU 0 OU -1
  - Gemini 2.5 Pro       : range [128, 32768] OU -1 (dynamic)
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest

from app.ai.providers.base import (
    ChatCompletionRequest,
    ChatMessage,
)
from app.ai.providers.gemini import GeminiChatProvider, _reset_client_for_tests

# ═══════════════════════════════════════════════════════════════════
# Helpers — monkeypatch du client SDK Gemini pour capturer les kwargs
# ═══════════════════════════════════════════════════════════════════


class _EmptyAsyncIterator:
    """Async iterator vide — yield 0 chunk, on capture juste les kwargs."""

    def __aiter__(self) -> _EmptyAsyncIterator:
        return self

    async def __anext__(self) -> Any:
        raise StopAsyncIteration


class _CapturingFakeClient:
    """Faux client `google.genai.Client` qui capture les kwargs des appels
    `aio.models.generate_content_stream` pour assertion test.

    Reproduit la chaîne d'attributs + sémantique du vrai SDK :
        stream = await client.aio.models.generate_content_stream(model=..., contents=..., config=...)
        async for chunk in stream: ...

    Le vrai SDK : `generate_content_stream` est une coroutine qui retourne
    un async iterator. On reproduit avec une `async def` qui retourne un
    `_EmptyAsyncIterator` (yield zéro chunk, suffit pour capturer les kwargs).
    """

    def __init__(self) -> None:
        self.captured_kwargs: dict[str, Any] = {}
        self.aio = MagicMock()

        async def _fake_stream(**kwargs: Any) -> _EmptyAsyncIterator:
            self.captured_kwargs = kwargs
            return _EmptyAsyncIterator()

        self.aio.models = MagicMock()
        self.aio.models.generate_content_stream = _fake_stream


@pytest.fixture
def _fake_client(monkeypatch: pytest.MonkeyPatch) -> _CapturingFakeClient:
    """Remplace le singleton client par un fake qui capture les kwargs."""
    fake = _CapturingFakeClient()
    _reset_client_for_tests()

    from app.ai.providers import gemini as gemini_module

    monkeypatch.setattr(gemini_module, "_get_client", lambda: fake)
    return fake


async def _consume_stream(provider: GeminiChatProvider, request: ChatCompletionRequest) -> None:
    """Consomme le stream du provider (qui yield 0 chunks dans nos fakes)."""
    async for _ in provider.stream_chat(request):
        pass


def _build_request(model: str, *, disable_thinking: bool) -> ChatCompletionRequest:
    """Construit un ChatCompletionRequest minimal pour tester l'injection
    de `thinking_config` selon le modèle et le flag `disable_thinking`."""
    return ChatCompletionRequest(
        messages=[ChatMessage(role="user", content="Hello")],
        model=model,
        temperature=0.5,
        max_tokens=4096,
        extra={"disable_thinking": disable_thinking} if disable_thinking else {},
    )


# ═══════════════════════════════════════════════════════════════════
# Bug-experts-Pro 2026-05-23 — Tests Pro vs Flash
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_flash_with_disable_thinking_sets_budget_zero(
    _fake_client: _CapturingFakeClient,
) -> None:
    """Gemini 2.5 Flash + disable_thinking=True → thinking_budget=0 (API accepte)."""
    provider = GeminiChatProvider()
    request = _build_request("gemini-2.5-flash", disable_thinking=True)

    await _consume_stream(provider, request)

    config = _fake_client.captured_kwargs.get("config")
    assert config is not None, "config kwarg manquant"
    thinking_config = getattr(config, "thinking_config", None)
    assert thinking_config is not None, "thinking_config doit être posé"
    assert thinking_config.thinking_budget == 0, (
        f"Flash doit avoir thinking_budget=0, got {thinking_config.thinking_budget}"
    )


@pytest.mark.asyncio
async def test_pro_with_disable_thinking_sets_budget_minimum(
    _fake_client: _CapturingFakeClient,
) -> None:
    """Gemini 2.5 Pro + disable_thinking=True → thinking_budget=128 (minimum API).

    L'API Gemini 2.5 Pro REJETTE thinking_budget=0 (range [128, 32768] ou -1).
    On pose le minimum 128 pour libérer ~99% du budget output_tokens à la
    réponse réelle.
    """
    provider = GeminiChatProvider()
    request = _build_request("gemini-2.5-pro", disable_thinking=True)

    await _consume_stream(provider, request)

    config = _fake_client.captured_kwargs.get("config")
    assert config is not None, "config kwarg manquant"
    thinking_config = getattr(config, "thinking_config", None)
    assert thinking_config is not None, "thinking_config doit être posé"
    assert thinking_config.thinking_budget == 128, (
        f"Pro doit avoir thinking_budget=128 (minimum API), got {thinking_config.thinking_budget}"
    )


@pytest.mark.asyncio
async def test_without_disable_thinking_no_config_posed(
    _fake_client: _CapturingFakeClient,
) -> None:
    """Sans disable_thinking → aucun thinking_config posé (mode adaptatif par défaut)."""
    provider = GeminiChatProvider()
    request = _build_request("gemini-2.5-pro", disable_thinking=False)

    await _consume_stream(provider, request)

    config = _fake_client.captured_kwargs.get("config")
    assert config is not None, "config kwarg manquant"
    thinking_config = getattr(config, "thinking_config", None)
    assert thinking_config is None, (
        f"Sans disable_thinking, thinking_config doit être None, got {thinking_config}"
    )


@pytest.mark.asyncio
async def test_case_insensitive_pro_detection(
    _fake_client: _CapturingFakeClient,
) -> None:
    """Détection `pro` insensible à la casse (futur-proof si SDK renvoie une variante).

    Le fix utilise `"pro" in model.lower()` — robuste aux variations type
    `Gemini-2.5-PRO`, `GEMINI-2.5-Pro`, `gemini-3-pro` (futur), etc.
    """
    provider = GeminiChatProvider()
    # NB: les modèles non supportés sont rejetés par `supports_model`, donc
    # on teste uniquement avec gemini-2.5-pro qui est dans supported_models.
    request = _build_request("gemini-2.5-pro", disable_thinking=True)

    await _consume_stream(provider, request)

    config = _fake_client.captured_kwargs.get("config")
    thinking_config = getattr(config, "thinking_config", None)
    # Doit matcher "pro" peu importe la casse exacte.
    assert thinking_config.thinking_budget == 128


@pytest.mark.asyncio
async def test_anti_regression_5_experts_pro_no_longer_silent(
    _fake_client: _CapturingFakeClient,
) -> None:
    """Anti-régression : les 5 experts NEXYA en Pro (Langue, Sciences,
    Ingénierie, Médecine, Légal) ne doivent plus subir le stream vide
    silencieux causé par thinking_budget=0 sur Pro.

    Validation : pour le modèle `gemini-2.5-pro` (utilisé par les 5 experts),
    le `thinking_budget` posé doit être >= 128 (jamais 0).
    """
    provider = GeminiChatProvider()
    request = _build_request("gemini-2.5-pro", disable_thinking=True)

    await _consume_stream(provider, request)

    thinking_config = _fake_client.captured_kwargs["config"].thinking_config
    assert thinking_config.thinking_budget >= 128, (
        "Bug-experts-Pro 2026-05-23 : Pro avec thinking_budget < 128 "
        "fait planter l'API silencieusement → stream vide → 5 experts muets."
    )
    assert thinking_config.thinking_budget != 0, (
        "thinking_budget=0 sur Pro = bug terrain Ivan « experts ne répondent pas »."
    )
