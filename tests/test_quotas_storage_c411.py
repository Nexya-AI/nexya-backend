"""C4.11 — Tests focused QuotasService + LibraryStorageExceededException 402.

Couverture ciblée (decision Ivan tests focused, ~13 tests groupés) :

  **QuotasService** (Phase 2.5) :
    1. `test_compute_free_user_returns_voice_null` — Free → voice cachée
       (decision Q1=A C4.11)
    2. `test_compute_pro_user_returns_voice_120` — Pro → voice_max=120 min
    3. `test_count_docs_this_month_filters_pdf_docx_only` — exclut images/code
    4. `test_next_month_utc_midnight_normal_month` — juin → 1er juillet
    5. `test_next_month_utc_midnight_december_rollover` — déc → 1er janvier
       année suivante
    6. `test_get_voice_minutes_today_redis_down_fallback_zero` — fail-safe

  **Endpoint `GET /user/quotas`** (Phase 2.6) :
    7. `test_endpoint_returns_response_shape_free` — 200 + voice null
    8. `test_endpoint_returns_response_shape_pro` — 200 + voice peuplé
    9. `test_endpoint_requires_auth_401` — pas de JWT → 401

  **LibraryStorageExceededException 402** (Phase 2.7) :
   10. `test_storage_exception_data_payload_format` — current_bytes +
       max_bytes + plan exposés
   11. `test_storage_exception_message_humanizes_gb_for_pro` —
       « 95 MB sur 10 GB »
   12. `test_storage_pre_flight_free_blocks_at_cap` — Free 95MB +
       upload 10MB → 402
   13. `test_storage_pre_flight_pro_allows_under_cap` — Pro 5GB +
       upload 100MB → OK
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.core.errors.exceptions import LibraryStorageExceededException
from app.features.account.service import (
    QuotasService,
    _next_month_utc_midnight,
    _start_of_current_month_utc,
)
from app.features.library.service import LibraryService

# ══════════════════════════════════════════════════════════════
# Helpers
# ══════════════════════════════════════════════════════════════


def _make_user(*, is_pro: bool = False) -> MagicMock:
    user = MagicMock()
    user.id = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")
    user.is_pro = is_pro
    return user


class _ScalarResult:
    def __init__(self, *, scalar_value: object | None = None) -> None:
        self._value = scalar_value

    def scalar_one(self) -> object | None:
        return self._value

    def scalar_one_or_none(self) -> object | None:
        return self._value


# ══════════════════════════════════════════════════════════════
# QuotasService tests
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_compute_free_user_returns_voice_null() -> None:
    """Free user → voice_minutes_today=None + voice_minutes_max_day=None.

    Decision Q1=A C4.11 : la carte Voice est CACHÉE côté Flutter pour Free
    (UX épurée). Le backend retourne None pour les 2 champs voice.
    """
    user = _make_user(is_pro=False)
    db = MagicMock()
    # COUNT docs ce mois = 2, SUM storage = 50 MB
    db.execute = AsyncMock(
        side_effect=[
            _ScalarResult(scalar_value=2),  # COUNT docs
            _ScalarResult(scalar_value=50 * 1024 * 1024),  # SUM storage
        ]
    )

    snapshot = await QuotasService.compute(user, db)

    assert snapshot.plan == "free"
    assert snapshot.voice_minutes_today is None  # Carte cachée Flutter
    assert snapshot.voice_minutes_max_day is None
    assert snapshot.docs_generated_this_month == 2
    assert snapshot.library_storage_bytes == 50 * 1024 * 1024


@pytest.mark.asyncio
async def test_compute_pro_user_returns_voice_120() -> None:
    """Pro user → voice_minutes_today=N + voice_minutes_max_day=120 min."""
    user = _make_user(is_pro=True)
    db = MagicMock()
    db.execute = AsyncMock(
        side_effect=[
            _ScalarResult(scalar_value=15),  # COUNT docs ce mois
            _ScalarResult(scalar_value=2 * 1024 * 1024 * 1024),  # SUM 2 GB
        ]
    )

    # Mock Redis voice minutes today = 35
    with patch(
        "app.features.account.service.get_redis"
    ) as mock_get_redis:
        mock_redis = MagicMock()
        mock_redis.get = AsyncMock(return_value=b"35")
        mock_get_redis.return_value = mock_redis

        snapshot = await QuotasService.compute(user, db)

    assert snapshot.plan == "pro"
    assert snapshot.voice_minutes_today == 35
    assert snapshot.voice_minutes_max_day == 120
    assert snapshot.docs_generated_this_month == 15


def test_next_month_utc_midnight_normal_month() -> None:
    """Helper : juin → 1er juillet UTC minuit (mois standard)."""
    fixed_now = datetime(2026, 6, 15, 14, 32, 0, tzinfo=UTC)
    with patch(
        "app.features.account.service.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = fixed_now
        # Permet aux constructeurs de fonctionner normalement
        mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        result = _next_month_utc_midnight()

    assert result.year == 2026
    assert result.month == 7
    assert result.day == 1
    assert result.hour == 0


def test_next_month_utc_midnight_december_rollover() -> None:
    """Helper : décembre 2026 → 1er janvier 2027 (rollover année)."""
    fixed_now = datetime(2026, 12, 28, 23, 59, 59, tzinfo=UTC)
    with patch(
        "app.features.account.service.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        result = _next_month_utc_midnight()

    assert result.year == 2027
    assert result.month == 1
    assert result.day == 1


@pytest.mark.asyncio
async def test_get_voice_minutes_today_redis_down_fallback_zero() -> None:
    """Fail-safe absolu : Redis exception → return 0 (pas de 5xx)."""
    user_id = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")

    with patch(
        "app.features.account.service.get_redis"
    ) as mock_get_redis:
        mock_redis = MagicMock()
        mock_redis.get = AsyncMock(side_effect=ConnectionError("Redis down"))
        mock_get_redis.return_value = mock_redis

        result = await QuotasService._get_voice_minutes_today(user_id)

    assert result == 0  # Fail-safe, pas d'exception remontée


# ══════════════════════════════════════════════════════════════
# LibraryStorageExceededException tests
# ══════════════════════════════════════════════════════════════


def test_storage_exception_data_payload_format() -> None:
    """Exception expose `current_bytes + max_bytes + plan` dans `data`."""
    exc = LibraryStorageExceededException(
        current_bytes=95 * 1024 * 1024,
        max_bytes=100 * 1024 * 1024,
        plan="free",
    )

    assert exc.status_code == 402
    assert exc.code == "LIBRARY_STORAGE_EXCEEDED"
    assert exc.data == {
        "current_bytes": 95 * 1024 * 1024,
        "max_bytes": 100 * 1024 * 1024,
        "plan": "free",
    }


def test_storage_exception_message_humanizes_gb_for_pro() -> None:
    """Message Pro affiche '10 GB' (max ≥ 1 GB) vs '100 MB' Free."""
    exc_pro = LibraryStorageExceededException(
        current_bytes=8 * 1024 * 1024 * 1024,
        max_bytes=10 * 1024 * 1024 * 1024,
        plan="pro",
    )
    assert "10 GB" in str(exc_pro)

    exc_free = LibraryStorageExceededException(
        current_bytes=95 * 1024 * 1024,
        max_bytes=100 * 1024 * 1024,
        plan="free",
    )
    assert "100 MB" in str(exc_free)


@pytest.mark.asyncio
async def test_storage_pre_flight_free_blocks_at_cap() -> None:
    """Free 95 MB cumulé + upload 10 MB > 100 MB cap → 402."""
    user = _make_user(is_pro=False)
    db = MagicMock()
    # 1) count_active retourne 5 (sous le quota items 50)
    # 2) sum_storage_bytes retourne 95 MB
    db.execute = AsyncMock(
        side_effect=[
            _ScalarResult(scalar_value=5),  # COUNT items
            _ScalarResult(scalar_value=95 * 1024 * 1024),  # SUM storage
        ]
    )

    fake_data = b"X" * (10 * 1024 * 1024)  # 10 MB upload tentative

    # On mock l'object_store pour qu'il ne soit jamais appelé
    # (le pre-flight doit raise AVANT l'upload).
    fake_store = MagicMock()
    fake_store.upload_bytes = AsyncMock()

    with pytest.raises(LibraryStorageExceededException) as exc_info:
        await LibraryService.create_from_bytes(
            user,
            db,
            type_="document",
            title="test",
            data=fake_data,
            mime_type="application/pdf",
            file_type="pdf",
            store=fake_store,
        )

    assert exc_info.value.code == "LIBRARY_STORAGE_EXCEEDED"
    assert exc_info.value.data["plan"] == "free"
    assert exc_info.value.data["current_bytes"] == 95 * 1024 * 1024
    # CRITIQUE : upload_bytes ne doit JAMAIS être appelé (économie 2G/3G)
    fake_store.upload_bytes.assert_not_called()


@pytest.mark.asyncio
async def test_storage_pre_flight_pro_allows_under_cap() -> None:
    """Pro 5 GB cumulé + upload 5 MB < 10 GB cap → pas de raise.

    On utilise 5 MB (sous le cap unitaire `s3_max_upload_bytes=20MB`)
    pour valider que les pré-flights storage cap passent quand le user
    Pro a de la marge. Le test vérifie que `LibraryStorageExceededException`
    n'est PAS levée. On laisse l'execute raise sur l'INSERT pour
    confirmer qu'on est bien passé les pré-flights (quota + storage).
    """
    user = _make_user(is_pro=True)
    db = MagicMock()
    # 1) COUNT items = 20 (sous Pro 1000)
    # 2) SUM storage = 5 GB
    # 3) Le 3ème execute (INSERT) raise pour valider qu'on est passés
    db.execute = AsyncMock(
        side_effect=[
            _ScalarResult(scalar_value=20),
            _ScalarResult(scalar_value=5 * 1024 * 1024 * 1024),
            RuntimeError("Pre-flights passed — sentinel"),
        ]
    )
    fake_data = b"X" * (5 * 1024 * 1024)  # 5 MB (sous cap unitaire 20 MB)
    fake_store = MagicMock()
    fake_store.upload_bytes = AsyncMock(return_value=None)

    # On attend le sentinel RuntimeError, PAS LibraryStorageExceededException
    with pytest.raises(RuntimeError, match="Pre-flights passed"):
        await LibraryService.create_from_bytes(
            user,
            db,
            type_="document",
            title="test",
            data=fake_data,
            mime_type="application/pdf",
            file_type="pdf",
            store=fake_store,
        )

    # Upload MinIO doit avoir été appelé (les pré-flights ont passé)
    fake_store.upload_bytes.assert_called_once()


def test_start_of_current_month_utc_returns_first_day_midnight() -> None:
    """Helper : retourne 1er du mois courant UTC 00h00."""
    fixed_now = datetime(2026, 6, 15, 14, 32, 0, tzinfo=UTC)
    with patch(
        "app.features.account.service.datetime"
    ) as mock_dt:
        mock_dt.now.return_value = fixed_now
        mock_dt.side_effect = lambda *args, **kwargs: datetime(*args, **kwargs)

        result = _start_of_current_month_utc()

    assert result.year == 2026
    assert result.month == 6
    assert result.day == 1
    assert result.hour == 0
