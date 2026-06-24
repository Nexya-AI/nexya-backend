"""Quota rework 2026-06-24 (décision Ivan) — tests focused.

Couverture (Redis + DB entièrement mockés → robuste CI, pas de service réel) :

  **Exceptions paywall 402**
    1. ChatMessageQuotaExceededException — payload {current,max,plan,retry_after}
    2. ImageQuotaExceededException — payload {current,max,plan,reset_at}

  **Config (valeurs cibles)**
    3. storage Pro 5Go, vision Free 7, docs Free 7, image 7/21, chat 30/10800

  **QuotasService.compute enrichi**
    4. Free → chat 12/30 + reset, images 4/7, vision 2/7, voice None
    5. Pro  → chat None (illimité), images 10/21, vision 8/50, voice 30/120

  **Enforcement image par plan (BudgetTracker)**
    6. Free limit 7 dépassé → ImageQuotaExceededException (402) + rollback
    7. Pro limit 21 sous le cap → pas d'exception

  **Fail-safe Redis**
    8. _get_chat_usage Redis down → (0, 0)
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.ai.budget_tracker import BudgetTracker
from app.config import settings
from app.core.errors.exceptions import (
    ChatMessageQuotaExceededException,
    ImageQuotaExceededException,
)
from app.features.account.service import QuotasService

_UID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_user(*, is_pro: bool = False) -> MagicMock:
    user = MagicMock()
    user.id = _UID
    user.is_pro = is_pro
    return user


class _ScalarResult:
    def __init__(self, *, scalar_value: object | None = None) -> None:
        self._value = scalar_value

    def scalar_one(self) -> object | None:
        return self._value


class _RoutingRedis:
    """Mock Redis async qui route `.get`/`.ttl` selon un fragment de clé."""

    def __init__(
        self,
        values: dict[str, bytes],
        ttls: dict[str, int] | None = None,
    ) -> None:
        self._values = values
        self._ttls = ttls or {}

    async def get(self, key: str) -> bytes | None:
        for frag, val in self._values.items():
            if frag in key:
                return val
        return None

    async def ttl(self, key: str) -> int:
        for frag, t in self._ttls.items():
            if frag in key:
                return t
        return -2


# ══════════════════════════════════════════════════════════════
# 1-2. Exceptions paywall
# ══════════════════════════════════════════════════════════════


def test_chat_quota_exception_payload() -> None:
    exc = ChatMessageQuotaExceededException(retry_after=7200)
    assert exc.status_code == 402
    assert exc.code == "CHAT_MESSAGE_QUOTA_EXCEEDED"
    assert exc.data["plan"] == "free"
    assert exc.data["retry_after"] == 7200
    # current == max == cap Free (le user a atteint le plafond)
    assert exc.data["max"] == settings.chat_messages_free_per_window
    assert exc.data["current"] == settings.chat_messages_free_per_window


def test_image_quota_exception_payload() -> None:
    reset = datetime(2026, 6, 25, 0, 0, 0, tzinfo=UTC)
    exc = ImageQuotaExceededException(current=7, max_=7, plan="free", reset_at=reset)
    assert exc.status_code == 402
    assert exc.code == "IMAGE_QUOTA_EXCEEDED"
    assert exc.data["current"] == 7
    assert exc.data["max"] == 7
    assert exc.data["plan"] == "free"
    assert exc.data["reset_at"] == reset.isoformat()


# ══════════════════════════════════════════════════════════════
# 3. Config — valeurs cibles
# ══════════════════════════════════════════════════════════════


def test_config_values_quota_rework() -> None:
    assert settings.library_storage_max_bytes_pro == 5 * 1024 * 1024 * 1024
    assert settings.library_storage_max_bytes_free == 100 * 1024 * 1024
    assert settings.vision_images_free_per_day == 7
    assert settings.documents_quota_max_free == 7
    assert settings.image_gen_max_free == 7
    assert settings.image_gen_max_pro == 21
    assert settings.chat_messages_free_per_window == 30
    assert settings.chat_quota_window_seconds == 10_800


# ══════════════════════════════════════════════════════════════
# 4-5. QuotasService.compute enrichi
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_compute_free_includes_chat_image_vision() -> None:
    user = _make_user(is_pro=False)
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _ScalarResult(scalar_value=3),  # COUNT docs
            _ScalarResult(scalar_value=10 * 1024 * 1024),  # SUM storage
        ]
    )
    routing = _RoutingRedis(
        values={"chat_message": b"12", ":image:": b"4", ":vision_images:": b"2"},
        ttls={"chat_message": 7200},
    )
    with patch("app.features.account.service.get_redis", return_value=routing):
        snap = await QuotasService.compute(user, db)

    assert snap.plan == "free"
    assert snap.chat_messages_used == 12
    assert snap.chat_messages_max == 30
    assert snap.chat_reset_at is not None
    assert snap.images_used_today == 4
    assert snap.images_max_day == 7
    assert snap.vision_used_today == 2
    assert snap.vision_max_day == 7
    assert snap.voice_minutes_today is None  # Free → carte voix cachée
    assert snap.daily_reset_at is not None


@pytest.mark.asyncio
async def test_compute_pro_chat_none_caps() -> None:
    user = _make_user(is_pro=True)
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _ScalarResult(scalar_value=5),  # COUNT docs
            _ScalarResult(scalar_value=1 * 1024 * 1024 * 1024),  # SUM 1 GB
        ]
    )
    routing = _RoutingRedis(
        values={
            "voice_minutes": b"30",
            ":image:": b"10",
            ":vision_images:": b"8",
        },
    )
    with patch("app.features.account.service.get_redis", return_value=routing):
        snap = await QuotasService.compute(user, db)

    assert snap.plan == "pro"
    assert snap.chat_messages_used is None  # Pro = illimité
    assert snap.chat_messages_max is None
    assert snap.chat_reset_at is None
    assert snap.images_used_today == 10
    assert snap.images_max_day == 21
    assert snap.vision_used_today == 8
    assert snap.vision_max_day == 50
    assert snap.voice_minutes_today == 30
    assert snap.voice_minutes_max_day == 120


# ══════════════════════════════════════════════════════════════
# 6-7. Enforcement image par plan
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_image_budget_free_limit_blocks() -> None:
    """Free cap 7 dépassé → ImageQuotaExceededException 402 + rollback."""
    mock_redis = MagicMock()
    mock_redis.incrby = AsyncMock(return_value=8)  # 8 > limit 7
    mock_redis.expire = AsyncMock()
    mock_redis.decrby = AsyncMock()
    with patch("app.ai.budget_tracker.get_redis", return_value=mock_redis):
        tracker = BudgetTracker()
        with pytest.raises(ImageQuotaExceededException) as ei:
            await tracker.check_and_consume_image(str(_UID), cost=1, limit=7, plan="free")
    assert ei.value.status_code == 402
    assert ei.value.data["max"] == 7
    assert ei.value.data["plan"] == "free"
    mock_redis.decrby.assert_awaited_once()  # rollback atomique


@pytest.mark.asyncio
async def test_image_budget_pro_under_cap_ok() -> None:
    """Pro cap 21, 8e image → pas d'exception, retourne la nouvelle valeur."""
    mock_redis = MagicMock()
    mock_redis.incrby = AsyncMock(return_value=8)  # 8 <= limit 21
    mock_redis.expire = AsyncMock()
    mock_redis.decrby = AsyncMock()
    with patch("app.ai.budget_tracker.get_redis", return_value=mock_redis):
        tracker = BudgetTracker()
        result = await tracker.check_and_consume_image(str(_UID), cost=1, limit=21, plan="pro")
    assert result == 8
    mock_redis.decrby.assert_not_awaited()


# ══════════════════════════════════════════════════════════════
# 8. Fail-safe Redis
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_get_chat_usage_redis_down_fallback() -> None:
    mock_redis = MagicMock()
    mock_redis.get = AsyncMock(side_effect=ConnectionError("Redis down"))
    with patch("app.features.account.service.get_redis", return_value=mock_redis):
        count, ttl = await QuotasService._get_chat_usage(_UID)
    assert (count, ttl) == (0, 0)
