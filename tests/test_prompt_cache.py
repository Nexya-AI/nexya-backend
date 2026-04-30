"""
Tests — PromptCache Redis (brique B2).

Couverture adversariale :
1. `build_key` est déterministe et collision-aware (même payload → même clé,
   payload différent → clé différente, ordre kwargs ne compte pas).
2. `is_cacheable` respecte : kill-switch, safety-critical, historique multi-tours.
3. `get` renvoie None fail-open sur Redis KO, JSON corrompu, clé absente.
4. `put` rejette les payloads dangereux : texte vide, status KO, error_code,
   finish_reason=LENGTH (troncature).
5. `invalidate` est silencieux même si Redis est down.

Discipline : pas de Redis réel, tout en AsyncMock sur `get_redis()`.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.ai import cache as cache_module
from app.ai.cache import (
    CachedResponse,
    PromptCache,
    _reset_cache_for_tests,
    get_prompt_cache,
)
from app.ai.experts import get_expert_config
from app.ai.providers.base import ChatMessage, ChatUsage

# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════


def _fake_redis(*, get_return=None, get_raise=None, set_raise=None, delete_raise=None):
    """Fabrique un fake Redis avec get/set/delete AsyncMock paramétrables."""
    redis = MagicMock()
    redis.get = AsyncMock(
        return_value=get_return,
        side_effect=get_raise,
    )
    redis.set = AsyncMock(side_effect=set_raise)
    redis.delete = AsyncMock(side_effect=delete_raise)
    return redis


@pytest.fixture(autouse=True)
def _reset_singleton():
    _reset_cache_for_tests()
    yield
    _reset_cache_for_tests()


# ══════════════════════════════════════════════════════════════
# build_key — déterminisme
# ══════════════════════════════════════════════════════════════


def test_build_key_is_deterministic_for_same_payload():
    messages = [ChatMessage(role="user", content="Bonjour")]
    k1 = PromptCache.build_key(
        model="gemini-2.5-flash",
        messages=messages,
        system_prompt="Tu es NEXYA.",
        temperature=0.7,
        max_tokens=1024,
        expert_id="general",
    )
    k2 = PromptCache.build_key(
        model="gemini-2.5-flash",
        messages=messages,
        system_prompt="Tu es NEXYA.",
        temperature=0.7,
        max_tokens=1024,
        expert_id="general",
    )
    assert k1 == k2
    assert k1.startswith("prompt_cache:v1:")


def test_build_key_differs_on_message_content():
    m_a = [ChatMessage(role="user", content="Bonjour")]
    m_b = [ChatMessage(role="user", content="Hello")]
    k_a = PromptCache.build_key(
        model="gemini-2.5-flash",
        messages=m_a,
        system_prompt=None,
        temperature=0.7,
        max_tokens=None,
        expert_id="general",
    )
    k_b = PromptCache.build_key(
        model="gemini-2.5-flash",
        messages=m_b,
        system_prompt=None,
        temperature=0.7,
        max_tokens=None,
        expert_id="general",
    )
    assert k_a != k_b


def test_build_key_differs_on_model():
    messages = [ChatMessage(role="user", content="Bonjour")]
    k_a = PromptCache.build_key(
        model="gemini-2.5-flash",
        messages=messages,
        system_prompt=None,
        temperature=0.7,
        max_tokens=None,
        expert_id="general",
    )
    k_b = PromptCache.build_key(
        model="gemini-2.5-pro",
        messages=messages,
        system_prompt=None,
        temperature=0.7,
        max_tokens=None,
        expert_id="general",
    )
    assert k_a != k_b


def test_build_key_differs_on_expert_id():
    messages = [ChatMessage(role="user", content="Bonjour")]
    k_a = PromptCache.build_key(
        model="gemini-2.5-flash",
        messages=messages,
        system_prompt=None,
        temperature=0.7,
        max_tokens=None,
        expert_id="general",
    )
    k_b = PromptCache.build_key(
        model="gemini-2.5-flash",
        messages=messages,
        system_prompt=None,
        temperature=0.7,
        max_tokens=None,
        expert_id="computer",
    )
    assert k_a != k_b


def test_build_key_stable_over_temperature_rounding():
    """La température est arrondie à 4 décimales — 0.70001 et 0.7 collident."""
    messages = [ChatMessage(role="user", content="Bonjour")]
    k_a = PromptCache.build_key(
        model="gemini-2.5-flash",
        messages=messages,
        system_prompt=None,
        temperature=0.7,
        max_tokens=None,
        expert_id="general",
    )
    k_b = PromptCache.build_key(
        model="gemini-2.5-flash",
        messages=messages,
        system_prompt=None,
        temperature=0.70001,
        max_tokens=None,
        expert_id="general",
    )
    # 4 décimales : 0.7000 vs 0.7000 → même clé
    assert k_a == k_b


# ══════════════════════════════════════════════════════════════
# is_cacheable — garde-fous métier
# ══════════════════════════════════════════════════════════════


def test_is_cacheable_false_when_disabled():
    cache = PromptCache(enabled=False)
    config = get_expert_config("general")
    messages = [ChatMessage(role="user", content="Bonjour")]
    assert cache.is_cacheable(config, messages) is False


def test_is_cacheable_false_for_safety_critical_medicine():
    cache = PromptCache(enabled=True)
    config = get_expert_config("medicine")
    messages = [ChatMessage(role="user", content="J'ai mal à la tête.")]
    assert cache.is_cacheable(config, messages) is False


def test_is_cacheable_false_for_safety_critical_legal():
    cache = PromptCache(enabled=True)
    config = get_expert_config("legal")
    messages = [ChatMessage(role="user", content="C'est quoi l'OHADA ?")]
    assert cache.is_cacheable(config, messages) is False


def test_is_cacheable_true_for_general_single_turn():
    cache = PromptCache(enabled=True)
    config = get_expert_config("general")
    messages = [ChatMessage(role="user", content="Bonjour")]
    assert cache.is_cacheable(config, messages) is True


def test_is_cacheable_false_for_multi_turn_conversation():
    cache = PromptCache(enabled=True)
    config = get_expert_config("general")
    messages = [
        ChatMessage(role="user", content="Hello"),
        ChatMessage(role="assistant", content="Salut"),
        ChatMessage(role="user", content="Et toi ?"),
    ]
    assert cache.is_cacheable(config, messages) is False


# ══════════════════════════════════════════════════════════════
# get — lecture Redis + fail-open
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_returns_none_when_disabled(monkeypatch):
    cache = PromptCache(enabled=False)
    monkeypatch.setattr(cache_module, "get_redis", lambda: _fake_redis())
    assert await cache.get("prompt_cache:v1:deadbeef") is None


@pytest.mark.asyncio
async def test_get_returns_cached_response_on_hit(monkeypatch):
    cache = PromptCache(enabled=True)
    payload = {
        "text": "Bonjour !",
        "provider": "gemini",
        "model": "gemini-2.5-flash",
        "prompt_tokens": 12,
        "completion_tokens": 4,
        "total_tokens": 16,
    }
    fake = _fake_redis(get_return=json.dumps(payload))
    monkeypatch.setattr(cache_module, "get_redis", lambda: fake)
    hit = await cache.get("prompt_cache:v1:abc")
    assert hit is not None
    assert isinstance(hit, CachedResponse)
    assert hit.text == "Bonjour !"
    assert hit.total_tokens == 16
    assert hit.usage.prompt_tokens == 12


@pytest.mark.asyncio
async def test_get_returns_none_on_miss(monkeypatch):
    cache = PromptCache(enabled=True)
    fake = _fake_redis(get_return=None)
    monkeypatch.setattr(cache_module, "get_redis", lambda: fake)
    assert await cache.get("prompt_cache:v1:missing") is None


@pytest.mark.asyncio
async def test_get_fail_open_on_redis_exception(monkeypatch):
    cache = PromptCache(enabled=True)
    fake = _fake_redis(get_raise=RuntimeError("Redis down"))
    monkeypatch.setattr(cache_module, "get_redis", lambda: fake)
    # Ne lève pas — fail-open obligatoire
    result = await cache.get("prompt_cache:v1:abc")
    assert result is None


@pytest.mark.asyncio
async def test_get_fail_open_on_corrupted_json(monkeypatch):
    cache = PromptCache(enabled=True)
    fake = _fake_redis(get_return="not-valid-json{{{")
    monkeypatch.setattr(cache_module, "get_redis", lambda: fake)
    result = await cache.get("prompt_cache:v1:abc")
    assert result is None
    # Et on a tenté de supprimer l'entrée corrompue
    fake.delete.assert_awaited_once()


# ══════════════════════════════════════════════════════════════
# put — rejet des payloads dangereux
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_put_rejects_empty_text(monkeypatch):
    cache = PromptCache(enabled=True)
    fake = _fake_redis()
    monkeypatch.setattr(cache_module, "get_redis", lambda: fake)
    ok = await cache.put("prompt_cache:v1:abc", text="", provider="gemini", model="m")
    assert ok is False
    fake.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_rejects_whitespace_only_text(monkeypatch):
    cache = PromptCache(enabled=True)
    fake = _fake_redis()
    monkeypatch.setattr(cache_module, "get_redis", lambda: fake)
    ok = await cache.put("prompt_cache:v1:abc", text="   \n\t  ", provider="gemini", model="m")
    assert ok is False
    fake.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_rejects_non_completed_status(monkeypatch):
    cache = PromptCache(enabled=True)
    fake = _fake_redis()
    monkeypatch.setattr(cache_module, "get_redis", lambda: fake)
    ok = await cache.put(
        "prompt_cache:v1:abc",
        text="Réponse",
        provider="gemini",
        model="m",
        status="failed",
    )
    assert ok is False
    fake.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_rejects_error_code_present(monkeypatch):
    cache = PromptCache(enabled=True)
    fake = _fake_redis()
    monkeypatch.setattr(cache_module, "get_redis", lambda: fake)
    ok = await cache.put(
        "prompt_cache:v1:abc",
        text="Réponse",
        provider="gemini",
        model="m",
        error_code="LLM_UNAVAILABLE",
    )
    assert ok is False
    fake.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_rejects_length_truncation(monkeypatch):
    cache = PromptCache(enabled=True)
    fake = _fake_redis()
    monkeypatch.setattr(cache_module, "get_redis", lambda: fake)
    ok = await cache.put(
        "prompt_cache:v1:abc",
        text="Réponse tronquée",
        provider="gemini",
        model="m",
        finish_reason="length",
    )
    assert ok is False
    fake.set.assert_not_awaited()


@pytest.mark.asyncio
async def test_put_stores_valid_response(monkeypatch):
    cache = PromptCache(enabled=True, ttl_seconds=3600)
    fake = _fake_redis()
    monkeypatch.setattr(cache_module, "get_redis", lambda: fake)
    usage = ChatUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30)
    ok = await cache.put(
        "prompt_cache:v1:abc",
        text="Bonjour !",
        provider="gemini",
        model="gemini-2.5-flash",
        usage=usage,
        status="completed",
        finish_reason="stop",
    )
    assert ok is True
    fake.set.assert_awaited_once()
    call_kwargs = fake.set.await_args.kwargs
    assert call_kwargs["ex"] == 3600
    stored = json.loads(fake.set.await_args.args[1])
    assert stored["text"] == "Bonjour !"
    assert stored["total_tokens"] == 30


@pytest.mark.asyncio
async def test_put_fail_open_on_redis_exception(monkeypatch):
    cache = PromptCache(enabled=True)
    fake = _fake_redis(set_raise=RuntimeError("Redis down"))
    monkeypatch.setattr(cache_module, "get_redis", lambda: fake)
    ok = await cache.put(
        "prompt_cache:v1:abc",
        text="Réponse",
        provider="gemini",
        model="m",
    )
    # Pas d'exception propagée
    assert ok is False


# ══════════════════════════════════════════════════════════════
# invalidate
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_invalidate_calls_delete(monkeypatch):
    cache = PromptCache(enabled=True)
    fake = _fake_redis()
    monkeypatch.setattr(cache_module, "get_redis", lambda: fake)
    await cache.invalidate("prompt_cache:v1:abc")
    fake.delete.assert_awaited_once()


@pytest.mark.asyncio
async def test_invalidate_fail_open_on_exception(monkeypatch):
    cache = PromptCache(enabled=True)
    fake = _fake_redis(delete_raise=RuntimeError("Redis down"))
    monkeypatch.setattr(cache_module, "get_redis", lambda: fake)
    # Ne lève pas
    await cache.invalidate("prompt_cache:v1:abc")


# ══════════════════════════════════════════════════════════════
# Singleton
# ══════════════════════════════════════════════════════════════


def test_get_prompt_cache_returns_same_instance():
    a = get_prompt_cache()
    b = get_prompt_cache()
    assert a is b
