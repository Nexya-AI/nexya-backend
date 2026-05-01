"""
Rate limiter IP + user — sliding window via Redis.

Deux familles de limites coexistent :

1. **IP-scoped** (`check_ip_rate_limit`) : protège les endpoints publics
   non authentifiés contre le brute-force (login, register).

2. **User-scoped** (`check_user_rate_limit`) : protège les actions
   authentifiées sensibles contre l'abus (signalements, par exemple).
   Le compteur est porté par l'`user_id`, pas par l'IP — un même user
   derrière un proxy mobile (NAT carrier) ne peut pas contourner sa
   limite en sautant d'IP.

Limites (CLAUDE.md section 13) :
- Login    : 10 tentatives / minute par IP
- Register : 5 tentatives / minute par IP
- Signalements abus : 10 / heure / utilisateur (anti-spam UI)

Algorithme commun : sliding window counter (Redis INCR + EXPIRE atomique
au premier hit). Simple, performant, suffisant pour notre cas d'usage.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import Request

from app.core.database.redis import get_redis
from app.core.errors.exceptions import RateLimitAbuseException, RateLimitIPException

log = structlog.get_logger()

# ── Préfixes Redis ─────────────────────────────────────────────
RATE_LIMIT_PREFIX = "rate:ip:"
USER_RATE_LIMIT_PREFIX = "rate:user:"


def _get_client_ip(request: Request) -> str:
    """Extrait l'IP du client, en tenant compte des proxies (X-Forwarded-For)."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def check_ip_rate_limit(
    request: Request,
    action: str,
    max_requests: int,
    window_seconds: int = 60,
) -> None:
    """Vérifie le rate limit IP pour une action donnée.

    Args:
        request: requête FastAPI (pour extraire l'IP)
        action: identifiant de l'action ('login', 'register')
        max_requests: nombre maximum de requêtes dans la fenêtre
        window_seconds: taille de la fenêtre en secondes (défaut 60s)

    Raises:
        RateLimitIPException: si le seuil est dépassé
    """
    ip = _get_client_ip(request)
    key = f"{RATE_LIMIT_PREFIX}{action}:{ip}"

    redis = get_redis()

    # INCR atomique + TTL au premier appel
    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, window_seconds)

    if current > max_requests:
        ttl = await redis.ttl(key)
        log.warning(
            "rate_limit.ip.exceeded",
            action=action,
            ip=ip,
            current=current,
            max=max_requests,
            retry_after=ttl,
        )
        raise RateLimitIPException(retry_after=max(ttl, 1))


async def rate_limit_login(request: Request) -> None:
    """Rate limit pour POST /auth/login — 10 requêtes/minute par IP."""
    await check_ip_rate_limit(request, action="login", max_requests=10)


async def rate_limit_register(request: Request) -> None:
    """Rate limit pour POST /auth/register — 5 requêtes/minute par IP."""
    await check_ip_rate_limit(request, action="register", max_requests=5)


async def rate_limit_refresh(request: Request) -> None:
    """Rate limit pour POST /auth/refresh — 20 requêtes/minute par IP.

    Protège contre un attaquant qui obtient un refresh token leaké (XSS,
    vol device, MITM, log fuite) et spamme la rotation JWT pour obtenir
    N access tokens. Sans plafond IP, un seul refresh token compromis
    permet une exploitation continue ; avec ce rate limit, l'attaquant
    est borné à 20 rotations par minute par IP source.

    Calibration `20/min` (vs `10/min` sur `/auth/login`) :
    - Un refresh est moins coûteux qu'un login (pas de bcrypt, pas de
      SELECT user complet — juste un SELECT refresh_tokens + une
      rotation).
    - Les apps mobiles peuvent légitimement faire plusieurs refresh
      rapprochés sur changement de réseau (WiFi → 4G), hot-reload
      Flutter, ou recovery après suspension iOS/Android.
    - 20/min couvre largement un usage humain ; au-delà = abus évident.
    """
    await check_ip_rate_limit(request, action="refresh", max_requests=20)


# ══════════════════════════════════════════════════════════════
# RATE LIMIT — user-scoped
# ══════════════════════════════════════════════════════════════


async def check_user_rate_limit(
    user_id: uuid.UUID | str,
    action: str,
    max_requests: int,
    window_seconds: int,
    *,
    on_exceeded: type[Exception] = RateLimitAbuseException,
) -> None:
    """Vérifie le rate limit user pour une action donnée.

    Args:
        user_id: identifiant utilisateur (UUID ou str)
        action: identifiant de l'action ('abuse_report', etc.)
        max_requests: nombre maximum de requêtes dans la fenêtre
        window_seconds: taille de la fenêtre en secondes
        on_exceeded: classe d'exception à lever (doit accepter `retry_after=int`)

    Raises:
        on_exceeded: si le seuil est dépassé (retry_after = TTL de la clé)
    """
    key = f"{USER_RATE_LIMIT_PREFIX}{action}:{user_id}"
    redis = get_redis()

    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, window_seconds)

    if current > max_requests:
        ttl = await redis.ttl(key)
        log.warning(
            "rate_limit.user.exceeded",
            action=action,
            user_id=str(user_id),
            current=current,
            max=max_requests,
            retry_after=ttl,
        )
        raise on_exceeded(retry_after=max(ttl, 1))


async def rate_limit_forgot_password_ip(request: Request) -> None:
    """Rate limit IP pour POST /auth/forgot-password — 10 requêtes/heure/IP.

    Protection anti-enumeration (un attaquant ne peut pas scanner 1000 emails
    à la minute depuis une seule IP).
    """
    await check_ip_rate_limit(
        request,
        action="forgot_password",
        max_requests=10,
        window_seconds=3600,
    )


async def rate_limit_reset_password_ip(request: Request) -> None:
    """Rate limit IP pour POST /auth/reset-password — 5 requêtes/heure/IP.

    Un token valide = un reset. Au-delà de 5 tentatives/heure, c'est
    forcément un bruteforce de token.
    """
    await check_ip_rate_limit(
        request,
        action="reset_password",
        max_requests=5,
        window_seconds=3600,
    )


async def rate_limit_forgot_password_email(email: str) -> None:
    """Rate limit email-scoped pour POST /auth/forgot-password — 3/heure/email.

    Même si l'IP change (attaquant distribué), un email donné ne peut
    déclencher plus de 3 envois par heure. Évite qu'une victime reçoive
    un flot d'emails de reset depuis plusieurs IPs.

    Clé : SHA-256 de l'email (on ne stocke pas les emails en clair dans Redis).
    Ne lève PAS d'exception visible côté client — on laisse l'endpoint
    retourner son 200 générique anti-enumeration. Le caller lit le retour
    booléen pour décider s'il envoie ou pas.
    """
    import hashlib

    digest = hashlib.sha256(email.strip().lower().encode("utf-8")).hexdigest()[:16]
    key = f"{USER_RATE_LIMIT_PREFIX}forgot_password:{digest}"
    redis = get_redis()

    current = await redis.incr(key)
    if current == 1:
        await redis.expire(key, 3600)
    if current > 3:
        log.warning("rate_limit.email.forgot_password_exceeded", email_hash=digest, current=current)
        raise _ForgotPasswordEmailThrottled()


class _ForgotPasswordEmailThrottled(Exception):
    """Sentinelle interne — le caller la catch pour skipper l'envoi silencieusement."""


async def rate_limit_abuse_reports(user_id: uuid.UUID | str) -> None:
    """Rate limit pour POST /chat/reports — 10 signalements/heure/utilisateur.

    Code dédié (`RATE_LIMIT_ABUSE`) plutôt que `RATE_LIMIT_IP` : le Flutter
    distingue un IP pénalisée (brute-force auth) d'un user qui spamme
    le bouton « Signaler » (mécanisme anti-abus du modération).
    """
    await check_user_rate_limit(
        user_id,
        action="abuse_report",
        max_requests=10,
        window_seconds=3600,
    )


async def rate_limit_register_daily_ip(request: Request, *, max_per_day: int = 5) -> None:
    """Rate limit IP pour POST /auth/register — fenêtre 24 h (couche 2).

    Couche 1 : `rate_limit_register` (sliding window 5/min) — bloque les
    raffales courtes de type brute-force.
    Couche 2 (ici) : 5/jour/IP — bloque le cas où un attaquant espace
    ses requêtes dans le temps pour passer sous le radar du rate limit
    minute mais continue à spammer des comptes. Complémentaire, pas
    redondant : une IP légitime ne crée pas 6 comptes/jour.

    Sur NAT carrier (beaucoup de users derrière une même IP mobile),
    5/jour est suffisamment haut pour ne pas faire de faux positifs —
    c'est la raison pour laquelle on combine avec un quota device-level
    plus strict côté `device_quotas`.
    """
    await check_ip_rate_limit(
        request,
        action="register_daily",
        max_requests=max_per_day,
        window_seconds=86400,
    )


async def rate_limit_chat_messages(user_id: uuid.UUID | str, *, max_per_minute: int = 100) -> None:
    """Rate limit user-scoped pour les messages de chat — 100/min/user.

    **Important** : 100/min est une limite anti-bot, PAS une limite UX.
    Un utilisateur humain tape au mieux 2-3 messages/min. Si on voit
    100 msg/min sur un user, c'est forcément un compte automatisé —
    on bloque avant d'appeler le LLM pour ne pas brûler des tokens
    inutiles. Le message d'erreur côté Flutter peut être discret
    ("Trop de messages, ralentissez un peu") pour ne pas alarmer un
    user légitime qui aurait atteint la limite par hasard.

    Clé : `rate:user:chat_msg:{uid}:{YYYY-MM-DDTHH:MM}` (fenêtre fixe
    à la minute via le TTL, pas sliding — suffisant pour ce volume).
    """
    await check_user_rate_limit(
        user_id,
        action="chat_msg",
        max_requests=max_per_minute,
        window_seconds=60,
    )
