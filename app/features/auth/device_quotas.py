"""
Device quota service — plafond journalier d'inscriptions par appareil.

Complémentaire au rate limit IP (`rate_limit_register` 5/min + 5/jour) :
un attaquant qui tourne sur un réseau de proxies (IP différentes à
chaque requête) passerait sous le rate limit IP. Si ces requêtes
viennent toutes du même device_id (header X-Device-Id émis par le
Flutter au premier lancement et persisté en local), on le voit et
on bloque à N inscriptions/jour.

Comportement :
- Header X-Device-Id présent et valide → compteur sur cet ID.
- Header absent → compteur sur la sentinelle "unknown" (limite
  partagée — rend l'inscription sans header très peu pratique,
  ce qui incite le Flutter à toujours l'envoyer).
- Header malformé (pas [A-Za-z0-9_-]+) → considéré "unknown".

Implémentation atomique via UPSERT Postgres avec `ON CONFLICT ... DO
UPDATE` et `RETURNING count`. Pas de SELECT préalable (TOCTOU-safe,
un seul aller-retour DB).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.errors.exceptions import DeviceQuotaExceededException
from app.core.security.sanitizer import is_safe_identifier

log = structlog.get_logger()

# Sentinelle pour les requêtes sans header — limite partagée entre
# tous les clients qui n'envoient pas d'ID. C'est *volontaire* : force
# le Flutter à envoyer l'ID pour bénéficier de son propre quota.
UNKNOWN_DEVICE_SENTINEL = "unknown"


def normalize_device_id(raw: str | None) -> str:
    """Normalise un header `X-Device-Id` vers une clé de quota sûre.

    - `None` ou vide → sentinelle "unknown".
    - Format invalide (caractères hors [A-Za-z0-9_-]) → sentinelle,
      pour éviter qu'un attaquant injecte des \\r\\n ou des
      caractères de contrôle dans la clé DB.
    - Trop long (> 128 chars) → sentinelle (une clé saine tient en
      ~40 chars, au-delà c'est suspect).
    - Sinon → la valeur telle quelle (on ne lowercase pas — les UUIDs
      Flutter sont case-sensitive ; deux IDs qui diffèrent par la
      casse sont volontairement comptés séparément).
    """
    if not raw:
        return UNKNOWN_DEVICE_SENTINEL
    if not is_safe_identifier(raw, max_length=128):
        return UNKNOWN_DEVICE_SENTINEL
    return raw


async def check_and_consume_device_quota(
    device_id: str,
    db: AsyncSession,
    *,
    ip: str | None = None,
    daily_limit: int | None = None,
) -> int:
    """Incrémente le compteur journalier pour ce device et vérifie le quota.

    Args:
        device_id:    identifiant normalisé (via `normalize_device_id`).
        db:           session SQLAlchemy async.
        ip:           dernière IP observée (stockée dans `last_ip`).
                      Utile pour investiguer une attaque a posteriori.
        daily_limit:  override du quota (défaut : `settings.
                      device_registration_daily_limit`).

    Returns:
        Le nombre d'inscriptions cumulées pour ce device aujourd'hui
        *après* incrémentation (donc >= 1).

    Raises:
        DeviceQuotaExceededException: si le post-incrément > limit.
        Dans ce cas, **le compteur est bien incrémenté** (c'est OK :
        un quota dépassé doit rester comptabilisé pour la détection
        de spam persistant — un attaquant qui continue à spammer
        malgré les 429 reste visible dans la table).

    Note importante :
        Cette fonction **commit** la transaction en cas de succès
        comme en cas d'échec du quota. On ne veut PAS qu'un rollback
        ultérieur efface l'incrément — sinon un attaquant qui fait
        planter une étape suivante (ex. email malformé) rollbackerait
        son compteur et pourrait retenter à l'infini. L'upsert est
        donc commité **avant** tout autre SQL sensible du register.
    """
    limit = daily_limit if daily_limit is not None else settings.device_registration_daily_limit

    today: date = datetime.now(UTC).date()

    # UPSERT atomique — `RETURNING count` nous donne le compteur post-incrément.
    # INSERT + ON CONFLICT est critique pour éviter la TOCTOU race :
    # deux requêtes parallèles voient count=0 en même temps, insèrent deux
    # fois, et ont chacune un count=1 au lieu de count=2.
    result = await db.execute(
        text(
            """
            INSERT INTO device_quotas (device_id, day, count, last_ip, created_at, updated_at)
            VALUES (:device_id, :day, 1, :ip, NOW(), NOW())
            ON CONFLICT (device_id, day) DO UPDATE
                SET count = device_quotas.count + 1,
                    last_ip = COALESCE(EXCLUDED.last_ip, device_quotas.last_ip),
                    updated_at = NOW()
            RETURNING count
            """
        ),
        {"device_id": device_id, "day": today, "ip": ip},
    )
    new_count = int(result.scalar_one())

    # Commit explicite — on ne veut PAS que cet incrément soit rollbacké
    # par une erreur ultérieure dans le service register (voir docstring).
    await db.commit()

    if new_count > limit:
        log.warning(
            "device_quota.exceeded",
            device_id=device_id,
            day=str(today),
            count=new_count,
            limit=limit,
            ip=ip,
        )
        raise DeviceQuotaExceededException()

    log.debug(
        "device_quota.consumed",
        device_id=device_id,
        day=str(today),
        count=new_count,
        limit=limit,
    )
    return new_count
