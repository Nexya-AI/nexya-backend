"""Schémas Pydantic — Inventaire modèles IA (Session N1)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

ModelTier = Literal["flash", "pro", "ultra"]
ModelCapability = Literal[
    "text_chat",
    "image_generation",
    "vision",
    "audio_transcription",
    "text_to_speech",
    "function_calling",
    "json_mode",
]


class ModelInfo(BaseModel):
    """Métadonnées exposées d'un modèle IA disponible côté NEXYA.

    Aggrégé runtime depuis `provider.{name, supported_models, capabilities,
    max_context_tokens}` + croisement avec `_EXPERT_CONFIGS` pour
    `is_default_for`.
    """

    provider: str
    model_id: str
    display_name: str
    tier: ModelTier
    capabilities: list[ModelCapability]
    max_context_tokens: int
    is_default_for: list[str]  # liste des `expert_id` qui pointent dessus
    is_available: bool  # False si le provider est en mode Mock (clé absente)


class ModelsListResponse(BaseModel):
    """Réponse GET /models — inventaire complet."""

    models: list[ModelInfo]
    experts_routing: dict[str, str]  # expert_id → primary_model_id
