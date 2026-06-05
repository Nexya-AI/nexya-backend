"""Schémas Pydantic Account — `UserQuotasResponse` (Session C4.11).

Contrat strict aligné Flutter `UserQuotas` domain Equatable (cf.
`nexya_front_end/lib/features/account/domain/user_quotas.dart`).

Champs Voice nullables : un user Free n'a PAS accès à `/voice/*` (gated
`require_pro` E1). Côté backend on retourne `voice_minutes_today_*=null`
pour Free → côté front, la carte Voix est CACHÉE (decision Ivan C4.11
options Q1=A « UX épurée pas de frustration »).

`reset_at` global = 1er du mois suivant UTC minuit (les docs sont
mensuels). Le voice reset journalier UTC est implicite (pas exposé,
le client peut le déduire si besoin V2).
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class UserQuotasResponse(BaseModel):
    """Snapshot complet des quotas user pour le dashboard Account.

    Tous les compteurs sont `int >= 0`. Les caps `*_max_*` reflètent le
    plan actif (Free vs Pro). `reset_at` = prochain reset mensuel UTC
    pour les compteurs docs/storage. Les compteurs voice resettent à
    minuit UTC (pas exposé V1).
    """

    # ── Documents PDF/DOCX générés ce mois (reset 1er UTC) ─────
    docs_generated_this_month: int = Field(
        ...,
        ge=0,
        description="Nombre de docs PDF+DOCX générés ce mois UTC.",
    )
    docs_max_month: int = Field(
        ...,
        ge=0,
        description="Cap mensuel selon le plan (Free=5, Pro=100, TODO Ivan).",
    )

    # ── Voice minutes today (Pro only — null pour Free) ────────
    # Voice STT Whisper est require_pro E1. Free utilise speech_to_text
    # natif Flutter (offline, 0 quota backend). Carte CACHÉE côté UI
    # pour Free (decision Ivan Q1=A C4.11).
    voice_minutes_today: int | None = Field(
        default=None,
        ge=0,
        description="Minutes Whisper consommées today UTC. Null pour Free.",
    )
    voice_minutes_max_day: int | None = Field(
        default=None,
        ge=0,
        description="Cap journalier Pro (120 min). Null pour Free.",
    )

    # ── Library storage cumulé ─────────────────────────────────
    library_storage_bytes: int = Field(
        ...,
        ge=0,
        description="SOMME des size_bytes des items actifs (bytes).",
    )
    library_storage_max_bytes: int = Field(
        ...,
        ge=0,
        description="Cap selon le plan (Free 100 MB, Pro 10 GB).",
    )

    # ── Reset mensuel (1er du mois suivant UTC minuit) ─────────
    # Côté UI : ICU `quotasResetIn` calcule jours+heures depuis `now`.
    reset_at: datetime = Field(
        ...,
        description="Prochain reset mensuel UTC (1er du mois suivant minuit).",
    )

    # ── Plan utilisateur (info UI pour CTA paywall conditionnel) ─
    plan: str = Field(
        ...,
        min_length=1,
        max_length=16,
        description="Plan actif user : 'free' ou 'pro'.",
    )

    model_config = {"from_attributes": True}
