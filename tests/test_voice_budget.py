"""
Tests unitaires — extensions `BudgetTracker` voice_minutes + tts_chars (E1).

Fake Redis in-memory pour simuler le rollback atomique et les compteurs
journaliers. Pattern miroir `test_budget_tracker_embeddings.py`.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.ai.budget_tracker import BudgetTracker
from app.core.errors.exceptions import RateLimitExceededException


class _FakeRedis:
    def __init__(self) -> None:
        self._s: dict[str, int] = {}

    async def incrby(self, key: str, n: int) -> int:
        new = self._s.get(key, 0) + n
        self._s[key] = new
        return new

    async def decrby(self, key: str, n: int) -> int:
        new = self._s.get(key, 0) - n
        self._s[key] = new
        return new

    async def expire(self, key: str, ttl: int) -> bool:
        return True

    async def set(self, key: str, value: int) -> bool:
        self._s[key] = value
        return True

    async def ttl(self, key: str) -> int:
        return 3600

    async def mget(self, *keys: str) -> list[Any]:
        return [self._s.get(k) for k in keys]


# ══════════════════════════════════════════════════════════════
# 1. voice_minutes — incrément normal + cap
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_voice_minutes_increments_and_caps() -> None:
    redis = _FakeRedis()
    tracker = BudgetTracker(user_voice_minutes_per_day=3, redis_client=redis)
    assert await tracker.check_and_consume_voice_minutes("u1", minutes=1) == 1
    assert await tracker.check_and_consume_voice_minutes("u1", minutes=1) == 2
    assert await tracker.check_and_consume_voice_minutes("u1", minutes=1) == 3
    # 4ᵉ minute → cap atteint, raise.
    with pytest.raises(RateLimitExceededException):
        await tracker.check_and_consume_voice_minutes("u1", minutes=1)


# ══════════════════════════════════════════════════════════════
# 2. voice_minutes — cost N décrémente de N
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_voice_minutes_cost_n_consumes_n() -> None:
    redis = _FakeRedis()
    tracker = BudgetTracker(user_voice_minutes_per_day=10, redis_client=redis)
    val = await tracker.check_and_consume_voice_minutes("u1", minutes=5)
    assert val == 5
    val2 = await tracker.check_and_consume_voice_minutes("u1", minutes=3)
    assert val2 == 8


# ══════════════════════════════════════════════════════════════
# 3. voice_minutes — refund rembourse l'estimation excédentaire
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_voice_minutes_refund_reduces_counter() -> None:
    redis = _FakeRedis()
    tracker = BudgetTracker(user_voice_minutes_per_day=120, redis_client=redis)
    # Consomme 10 min (estimation haute).
    await tracker.check_and_consume_voice_minutes("u1", minutes=10)
    # Durée réelle = 3 min → rembourser 7 min.
    await tracker.refund_voice_minutes("u1", minutes=7)
    # Consommer encore 115 doit passer (on n'a plus que 3 consommées).
    assert (await tracker.check_and_consume_voice_minutes("u1", minutes=115)) == 118


# ══════════════════════════════════════════════════════════════
# 4. tts_chars — incrément + cap
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tts_chars_increments_and_caps() -> None:
    redis = _FakeRedis()
    tracker = BudgetTracker(user_tts_chars_per_day=1000, redis_client=redis)
    await tracker.check_and_consume_tts_chars("u1", chars=500)
    await tracker.check_and_consume_tts_chars("u1", chars=400)
    with pytest.raises(RateLimitExceededException):
        await tracker.check_and_consume_tts_chars("u1", chars=200)


# ══════════════════════════════════════════════════════════════
# 5. Isolation par user
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_voice_budget_isolated_per_user() -> None:
    redis = _FakeRedis()
    tracker = BudgetTracker(
        user_voice_minutes_per_day=2,
        user_tts_chars_per_day=100,
        redis_client=redis,
    )
    # user-a sature voice.
    await tracker.check_and_consume_voice_minutes("user-a", minutes=2)
    with pytest.raises(RateLimitExceededException):
        await tracker.check_and_consume_voice_minutes("user-a", minutes=1)
    # user-b démarre à 0 indépendamment.
    assert (await tracker.check_and_consume_voice_minutes("user-b", minutes=1)) == 1
