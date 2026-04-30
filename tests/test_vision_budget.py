"""Tests unitaires — extensions `BudgetTracker.vision_images` (E2)."""

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


@pytest.mark.asyncio
async def test_vision_images_increments_and_caps() -> None:
    redis = _FakeRedis()
    tracker = BudgetTracker(user_vision_images_per_day=3, redis_client=redis)
    assert await tracker.check_and_consume_vision_images("u1", images=1) == 1
    assert await tracker.check_and_consume_vision_images("u1", images=1) == 2
    assert await tracker.check_and_consume_vision_images("u1", images=1) == 3
    with pytest.raises(RateLimitExceededException):
        await tracker.check_and_consume_vision_images("u1", images=1)


@pytest.mark.asyncio
async def test_vision_images_cost_n_consumes_n() -> None:
    redis = _FakeRedis()
    tracker = BudgetTracker(user_vision_images_per_day=10, redis_client=redis)
    val = await tracker.check_and_consume_vision_images("u1", images=4)
    assert val == 4


@pytest.mark.asyncio
async def test_vision_images_refund_reduces_counter() -> None:
    redis = _FakeRedis()
    tracker = BudgetTracker(user_vision_images_per_day=50, redis_client=redis)
    await tracker.check_and_consume_vision_images("u1", images=5)
    await tracker.refund_vision_images("u1", images=3)
    # Il reste 2 consommées → on peut encore 48.
    assert (await tracker.check_and_consume_vision_images("u1", images=48)) == 50


@pytest.mark.asyncio
async def test_vision_images_isolated_per_user() -> None:
    redis = _FakeRedis()
    tracker = BudgetTracker(user_vision_images_per_day=2, redis_client=redis)
    await tracker.check_and_consume_vision_images("u-a", images=2)
    with pytest.raises(RateLimitExceededException):
        await tracker.check_and_consume_vision_images("u-a", images=1)
    # u-b indépendant.
    assert (await tracker.check_and_consume_vision_images("u-b", images=1)) == 1
