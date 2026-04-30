"""
Schémas Pydantic Files — response `UploadedFileResponse`.

Pas de `Create` body : l'upload passe par `UploadFile` + query params
(multipart/form-data), pas par un JSON body. Le request schema vit
directement dans la signature du handler FastAPI.

Convention :
- `storage_key` exposé dans la réponse UNIQUEMENT pour permettre au
  client d'enchaîner avec `POST /projects/{id}/files` via `upload_id`
  — le client n'est pas obligé de passer `storage_key` à la main, il
  peut juste passer `upload_id`. On expose quand même `storage_key`
  en info pour les débogages + tracking client-side.
- `content_sha256` exposé aussi pour permettre au client de faire des
  vérifs d'intégrité côté Flutter (recalcul SHA local → comparaison).
- `extracted_text_preview` limite à 500 premiers chars pour éviter de
  transférer 500k chars à chaque réponse. Le texte complet est accessible
  via `GET /files/{id}/text` (non livré en E3, prévu futur).
- `url` presigned (30 min) pour preview/download direct depuis MinIO.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

VirusScanStatusAlias = Literal["pending", "clean", "suspicious", "failed", "skipped"]
ExtractionStatusAlias = Literal["pending", "ok", "empty", "unsupported", "failed", "skipped"]


class UploadedFileResponse(BaseModel):
    """Item complet renvoyé par `POST /files/upload`."""

    id: uuid.UUID
    user_id: uuid.UUID

    storage_key: str
    content_sha256: str
    size_bytes: int
    mime_type: str
    original_filename: str | None
    extension: str | None

    virus_scan_status: VirusScanStatusAlias
    virus_scan_signature: str | None
    virus_scan_scanner: str | None
    virus_scanned_at: datetime | None

    extraction_status: ExtractionStatusAlias
    extracted_text_preview: str | None
    extracted_text_length: int | None
    page_count: int | None
    extraction_truncated: bool
    extracted_at: datetime | None

    url: str  # presigned MinIO URL TTL 30 min

    created_at: datetime
    updated_at: datetime
    deleted_at: datetime | None = None

    model_config = {"from_attributes": True}
