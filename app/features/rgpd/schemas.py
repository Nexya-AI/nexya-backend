"""Schémas Pydantic — RGPD + AI Act endpoints.

Session J1 — 2026-04-26.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ConsentType = Literal[
    "tos",
    "privacy_policy",
    "ai_processing",
    "ai_training_data",
    "marketing_email",
    "analytics",
    "cookies",
]
ConsentStatus = Literal["granted", "revoked"]
ConsentSource = Literal["register", "settings_screen", "api", "cookies_banner", "admin_grant"]
DeletionStatus = Literal["pending", "cancelled", "processing", "completed", "failed"]
LegalBasis = Literal["contract", "legitimate_interest", "consent", "legal_obligation"]
DataCategory = Literal[
    "user_input",
    "prompt_history",
    "file_content",
    "voice_audio",
    "image_content",
    "profile_data",
]
RegistryFormat = Literal["csv", "json"]


# ── Consent ─────────────────────────────────────────────────────


class ConsentRecordRequest(BaseModel):
    """Payload du body POST /rgpd/user/consent (action=record)."""

    action: Literal["record"] = "record"
    consent_type: ConsentType
    document_version: str = Field(min_length=1, max_length=32)
    document_hash: str = Field(min_length=64, max_length=64)
    source: ConsentSource = "settings_screen"


class ConsentRevokeRequest(BaseModel):
    """Payload du body POST /rgpd/user/consent (action=revoke)."""

    action: Literal["revoke"] = "revoke"
    consent_type: ConsentType


class ConsentResponse(BaseModel):
    """Une entrée du registre de consentements (export user)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    consent_type: ConsentType
    status: ConsentStatus
    granted_at: datetime
    revoked_at: datetime | None
    document_version: str
    document_hash: str
    source: ConsentSource


class ConsentListResponse(BaseModel):
    items: list[ConsentResponse]


# ── Deletion request ────────────────────────────────────────────


class DeletionRequestCreate(BaseModel):
    """Body optionnel pour POST /rgpd/user/account/delete-request."""

    reason: str | None = Field(default=None, max_length=1000)


class DeletionRequestResponse(BaseModel):
    """Renvoyé après création / annulation d'une demande de suppression."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    status: DeletionStatus
    requested_at: datetime
    scheduled_purge_at: datetime
    purged_at: datetime | None
    reason: str | None


# ── Data export ─────────────────────────────────────────────────


class DataExportSummary(BaseModel):
    """Résumé du contenu d'un export RGPD (manifest.json)."""

    user_id: str
    exported_at: datetime
    schema_version: str = "1.0"
    record_counts: dict[str, int]
    truncated: bool = False
    truncated_reason: str | None = None


# ── AI Act registry ─────────────────────────────────────────────


class AIActRegistryFilters(BaseModel):
    """Query params pour GET /rgpd/admin/ai-act-registry."""

    date_from: datetime | None = None
    date_to: datetime | None = None
    format: RegistryFormat = "csv"
    limit: int = Field(default=10_000, ge=1, le=100_000)


class AIActRegistryRow(BaseModel):
    """Une ligne du registre AI Act exporté."""

    created_at: datetime
    user_id: str | None
    expert_id: str
    provider: str
    model: str
    legal_basis: str | None
    data_categories: str | None
    retention_until: datetime | None
    prompt_tokens: int
    completion_tokens: int
    cost_usd: str
    outcome: str
