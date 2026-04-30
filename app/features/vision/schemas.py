"""
Schémas Pydantic — endpoint public `/vision/analyze` (E2).

Contrat API :
- `VisionAnalyzeRequest` : prompt + source d'image (3 modes mutex) +
  tier (flash/pro) + max_output_tokens + additional_images (cap 4 total).
- `VisionAnalysisResponse` : mappé depuis ORM `VisionAnalysis`.

Discipline :
- `image_source` Literal explicite + model_validator mutex : exactement
  **une** source fournie (upload_id OU library_id OU image_base64).
  Les 3 champs sont mutually exclusive, 422 sinon.
- N'expose PAS `image_sha256` / `prompt_sha256` côté client (internes
  dédup, fuite d'empreinte sémantique).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# ══════════════════════════════════════════════════════════════
# Types partagés
# ══════════════════════════════════════════════════════════════

VisionTier = Literal["flash", "pro"]
ImageSource = Literal["upload_id", "library_id", "image_base64"]


# ══════════════════════════════════════════════════════════════
# Requête
# ══════════════════════════════════════════════════════════════


class VisionAnalyzeRequest(BaseModel):
    """Body de `POST /vision/analyze`.

    3 modes d'entrée mutuellement exclusifs :
    - `upload_id` : UUID d'un UploadedFile déjà uploadé via `/files/upload`.
    - `library_id` : UUID d'un LibraryItem image (C3 Library).
    - `image_base64` : data URL directe `data:image/png;base64,...`.

    `additional_images` (optionnel, liste d'UUIDs UploadedFile) : pour
    comparer plusieurs images en un seul appel. Cap 3 supplémentaires
    = 4 images totales (image principale + 3 additional).
    """

    prompt: str = Field(min_length=1, max_length=4000)
    image_source: ImageSource
    upload_id: uuid.UUID | None = None
    library_id: uuid.UUID | None = None
    image_base64: str | None = Field(default=None, max_length=20_000_000)
    model_tier: VisionTier = "flash"
    max_output_tokens: int = Field(default=1024, ge=64, le=8192)
    additional_images: list[uuid.UUID] | None = Field(default=None, max_length=3)

    @field_validator("prompt", mode="before")
    @classmethod
    def _strip_prompt(cls, v):
        return v.strip() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _validate_source_consistency(self) -> VisionAnalyzeRequest:
        """Vérifie qu'exactement UNE source est fournie, cohérente avec
        `image_source`."""
        provided = {
            "upload_id": self.upload_id is not None,
            "library_id": self.library_id is not None,
            "image_base64": self.image_base64 is not None,
        }
        n_provided = sum(provided.values())
        if n_provided != 1:
            raise ValueError(
                "Exactement une source d'image est requise "
                "(upload_id, library_id ou image_base64), "
                f"{n_provided} fournie(s)."
            )
        if not provided[self.image_source]:
            raise ValueError(
                f"`image_source='{self.image_source}'` mais aucun champ correspondant n'est fourni."
            )
        return self


# ══════════════════════════════════════════════════════════════
# Réponse
# ══════════════════════════════════════════════════════════════


class VisionAnalysisResponse(BaseModel):
    """Analyse vision sérialisée pour le client.

    Mappée depuis `VisionAnalysis` ORM via `from_attributes=True`.
    N'expose PAS `image_sha256` / `prompt_sha256` (internes dédup) ni
    `metadata_json` (réservé admin).
    """

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    prompt: str
    analysis_text: str
    model: str
    provider: str
    tokens_input: int
    tokens_output: int
    cost_usd: Decimal
    image_width: int | None = None
    image_height: int | None = None
    source_file_id: uuid.UUID | None = None
    source_library_id: uuid.UUID | None = None
    created_at: datetime
