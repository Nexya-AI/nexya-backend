"""J1 — AIActRegistryService unit tests (~7 tests)."""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.features.rgpd.ai_act_registry_service import (
    _CSV_COLUMNS,
    AIActRegistryService,
)
from app.features.rgpd.schemas import AIActRegistryFilters


def _mk_aicall(**overrides):
    row = MagicMock()
    row.created_at = overrides.get("created_at", datetime(2026, 4, 26, 12, 0, 0, tzinfo=UTC))
    row.user_id = overrides.get("user_id", uuid.uuid4())
    row.expert_id = overrides.get("expert_id", "general")
    row.provider = overrides.get("provider", "gemini")
    row.model = overrides.get("model", "gemini-2.5-flash")
    row.legal_basis = overrides.get("legal_basis", "contract")
    row.data_categories = overrides.get("data_categories", "user_input")
    row.retention_until = overrides.get(
        "retention_until",
        datetime(2026, 7, 25, tzinfo=UTC),
    )
    row.prompt_tokens = overrides.get("prompt_tokens", 100)
    row.completion_tokens = overrides.get("completion_tokens", 50)
    row.total_tokens = overrides.get("total_tokens", 150)
    row.cost_usd = overrides.get("cost_usd", Decimal("0.00125"))
    row.outcome = overrides.get("outcome", "completed")
    row.failure_code = overrides.get("failure_code")
    return row


def test_export_csv_has_utf8_bom_for_excel():
    rows = [_mk_aicall()]
    out = AIActRegistryService.export_csv(rows)
    assert out.startswith(b"\xef\xbb\xbf"), "BOM UTF-8 manquant pour Excel"


def test_export_csv_columns_in_expected_order():
    rows = [_mk_aicall()]
    out = AIActRegistryService.export_csv(rows)
    text = out.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    assert header == _CSV_COLUMNS


def test_export_csv_handles_french_accents():
    rows = [_mk_aicall(provider="gémeéni-fr", failure_code="erreur_facteur")]
    out = AIActRegistryService.export_csv(rows)
    text = out.decode("utf-8-sig")
    assert "gémeéni-fr" in text


def test_export_csv_includes_legal_basis():
    rows = [
        _mk_aicall(legal_basis="contract"),
        _mk_aicall(legal_basis="consent"),
        _mk_aicall(legal_basis="legitimate_interest"),
    ]
    out = AIActRegistryService.export_csv(rows)
    text = out.decode("utf-8-sig")
    assert "contract" in text
    assert "consent" in text
    assert "legitimate_interest" in text


def test_export_json_structure():
    rows = [_mk_aicall(), _mk_aicall(expert_id="medicine")]
    out = AIActRegistryService.export_json(rows)
    payload = json.loads(out)
    assert "exported_at" in payload
    assert payload["row_count"] == 2
    assert isinstance(payload["items"], list)
    assert payload["items"][0]["expert_id"] == "general"
    assert payload["items"][1]["expert_id"] == "medicine"


def test_export_handles_anonymous_ai_calls():
    """ai_calls.user_id IS NULL (post-RGPD purge) → exporté tel quel."""
    rows = [_mk_aicall(user_id=None)]
    out = AIActRegistryService.export_json(rows)
    payload = json.loads(out)
    assert payload["items"][0]["user_id"] is None


def test_export_csv_empty_rows():
    out = AIActRegistryService.export_csv([])
    text = out.decode("utf-8-sig")
    # Doit contenir au moins le header
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    assert header == _CSV_COLUMNS
    # Et zéro row au-delà
    rows = list(reader)
    assert rows == []


@pytest.mark.asyncio
async def test_fetch_rows_forwards_date_filters():
    db = MagicMock()
    db.execute = AsyncMock()
    scalar = MagicMock()
    scalar.scalars = MagicMock(return_value=MagicMock(all=lambda: []))
    db.execute.return_value = scalar
    filters = AIActRegistryFilters(
        date_from=datetime(2026, 4, 1, tzinfo=UTC),
        date_to=datetime(2026, 4, 30, tzinfo=UTC),
        format="json",
        limit=100,
    )
    rows = await AIActRegistryService.fetch_rows(filters, db)
    assert rows == []
    # Vérifie qu'execute a bien été appelé une fois (avec un select)
    db.execute.assert_awaited_once()
