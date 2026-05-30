"""Schémas Pydantic pour POST /code-projects/build-zip (C4.6)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from app.features.rich_content.schemas import CodeProjectDraftData


class BuildZipRequest(BaseModel):
    """Request body POST /code-projects/build-zip.

    Réutilise directement `CodeProjectDraftData` (rich_content C4.6).
    Validation Pydantic stricte : 2-50 fichiers, filename path-safe,
    cap 5 MB total, dédup filenames.
    """

    payload: CodeProjectDraftData


class BuildZipResponse(BaseModel):
    """Response body POST /code-projects/build-zip.

    `download_url` : presigned URL MinIO TTL 24h (HMAC local, pas de
    round-trip réseau côté backend). Le client Dart télécharge les
    bytes via Dio puis utilise share_plus pour partager le .zip via
    le menu natif.

    `filename` : nom suggéré du fichier .zip côté client (`project_name`
    sanitizé filesystem-safe + extension `.zip`).

    `size_bytes` : taille en bytes du .zip généré (informatif UI).

    `expires_at` : ISO datetime UTC d'expiration de la presigned URL
    (le client peut afficher un toast « lien valide 24h » optionnel).
    """

    download_url: str = Field(description="Presigned URL MinIO TTL 24h")
    filename: str = Field(description="Nom suggéré côté client (avec .zip)")
    size_bytes: int = Field(ge=0, description="Taille du .zip en bytes")
    expires_at: datetime = Field(description="ISO UTC datetime d'expiration")
