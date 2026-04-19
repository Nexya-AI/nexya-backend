"""
Tâches arq liées à l'authentification.

Pour l'instant : purge des refresh tokens périmés pour maintenir
la table `refresh_tokens` à une taille raisonnable.

Exécution : déclenchée par cron dans `workers.worker.WorkerSettings`.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import structlog
from sqlalchemy import delete, or_

from app.core.database.postgres import AsyncSessionLocal
from app.features.auth.models import RefreshToken

log = structlog.get_logger()

# Les tokens expirés sont inutiles dès leur expiration — on garde 1 jour
# de marge pour l'observabilité (détection d'anomalies).
EXPIRED_RETENTION = timedelta(days=1)
# Les tokens révoqués sont conservés 7 jours : permet de détecter un replay
# (un token déjà rotaté qui revient) et de lever l'alerte sécurité.
REVOKED_RETENTION = timedelta(days=7)


async def cleanup_refresh_tokens(ctx: dict[str, Any]) -> dict[str, int]:
    """Supprime les refresh tokens expirés ou révoqués au-delà de la rétention.

    Renvoie un dict `{"deleted": N}` remonté dans les logs arq — utile pour
    monitorer le volume purgé et repérer une anomalie (zéro suppression =
    rotation en panne, des millions = fuite mémoire côté DB).
    """
    now = datetime.now(timezone.utc)
    expired_cutoff = now - EXPIRED_RETENTION
    revoked_cutoff = now - REVOKED_RETENTION

    async with AsyncSessionLocal() as session:
        stmt = delete(RefreshToken).where(
            or_(
                RefreshToken.expires_at < expired_cutoff,
                RefreshToken.revoked_at.is_not(None)
                & (RefreshToken.revoked_at < revoked_cutoff),
            )
        )
        result = await session.execute(stmt)
        await session.commit()

    deleted = result.rowcount or 0
    log.info("worker.auth.refresh_tokens.cleaned", deleted=deleted)
    return {"deleted": deleted}
