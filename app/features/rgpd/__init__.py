"""RGPD compliance — Article 7 (consent) + 15 (access) + 17 (erasure)
+ 20 (portability) + AI Act EU Article 13 (registry).

Session J1 — 2026-04-26.
"""

from app.features.rgpd.models import ConsentLog, DeletionRequest

__all__ = ["ConsentLog", "DeletionRequest"]
