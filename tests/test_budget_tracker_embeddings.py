"""
Tests unitaires — extension `BudgetTracker.check_and_consume_embeddings`
(Session D1).

Valide que le nouveau compteur `user_embeddings_day` s'incrémente
correctement, applique le plafond, et expose une `RateLimitExceeded`
quand dépassé.

Utilise un `FakeRedis` léger (in-memory) pour isoler du Redis réel —
même pattern que les tests `B2/B3`.
"""

from __future__ import annotations

import pytest

from app.ai.budget_tracker import BudgetTracker
from app.core.errors.exceptions import RateLimitExceededException

# ══════════════════════════════════════════════════════════════
# Fake Redis in-memory
# ══════════════════════════════════════════════════════════════


class _FakeRedis:
    """Redis factice — juste assez pour simuler INCR/EXPIRE/TTL."""

    def __init__(self) -> None:
        self.store: dict[str, int] = {}
        self.ttls: dict[str, int] = {}

    async def incrby(self, key: str, amount: int) -> int:
        self.store[key] = self.store.get(key, 0) + amount
        return self.store[key]

    async def decrby(self, key: str, amount: int) -> int:
        """Requis pour le rollback atomique sur dépassement plafond."""
        self.store[key] = self.store.get(key, 0) - amount
        return self.store[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.ttls[key] = seconds
        return True

    async def ttl(self, key: str) -> int:
        return self.ttls.get(key, -1)

    async def get(self, key: str):
        value = self.store.get(key)
        return str(value).encode() if value is not None else None

    async def mget(self, keys):
        return [str(self.store[k]).encode() if k in self.store else None for k in keys]


@pytest.fixture
def tracker() -> tuple[BudgetTracker, _FakeRedis]:
    fake = _FakeRedis()
    # Plafond bas pour tester le dépassement rapide.
    t = BudgetTracker(user_embeddings_per_day=3, redis_client=fake)
    return t, fake


# ══════════════════════════════════════════════════════════════
# 1. Incrément normal
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_embeddings_counter_increments_correctly(
    tracker: tuple[BudgetTracker, _FakeRedis],
) -> None:
    t, fake = tracker
    n1 = await t.check_and_consume_embeddings("user-1")
    assert n1 == 1
    n2 = await t.check_and_consume_embeddings("user-1")
    assert n2 == 2


# ══════════════════════════════════════════════════════════════
# 2. Plafond atteint → RateLimitExceededException
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_embeddings_counter_raises_when_over_cap(
    tracker: tuple[BudgetTracker, _FakeRedis],
) -> None:
    t, _ = tracker  # cap=3
    await t.check_and_consume_embeddings("user-1")
    await t.check_and_consume_embeddings("user-1")
    await t.check_and_consume_embeddings("user-1")  # 3 atteint
    with pytest.raises(RateLimitExceededException):
        await t.check_and_consume_embeddings("user-1")  # 4ème refusé


# ══════════════════════════════════════════════════════════════
# 3. Cost > 1 décrémente d'autant
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_embeddings_counter_consumes_cost_N(
    tracker: tuple[BudgetTracker, _FakeRedis],
) -> None:
    t, _ = tracker  # cap=3
    n = await t.check_and_consume_embeddings("user-1", cost=2)
    assert n == 2
    with pytest.raises(RateLimitExceededException):
        await t.check_and_consume_embeddings("user-1", cost=2)  # 2+2=4 > 3


# ══════════════════════════════════════════════════════════════
# 4. Isolation par user
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_embeddings_counters_isolated_per_user(
    tracker: tuple[BudgetTracker, _FakeRedis],
) -> None:
    """Le compteur user-1 ne contamine pas user-2."""
    t, _ = tracker
    await t.check_and_consume_embeddings("user-1")
    await t.check_and_consume_embeddings("user-1")
    await t.check_and_consume_embeddings("user-1")
    # user-2 doit pouvoir consommer normalement.
    n = await t.check_and_consume_embeddings("user-2")
    assert n == 1
