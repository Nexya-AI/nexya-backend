"""
Router Notifications — F3.

Trois zones d'endpoints sous un seul `APIRouter` :

1. `/notifications/*` (auth requis) — timeline in-app : GET list,
   POST /read, DELETE /{id}.
2. `/user/notification-preferences` (auth requis) — GET/PUT préférences
   par catégorie × canal. Alignement avec le namespace `/user/profile`,
   `/user/device-token` déjà en place côté auth.
3. `/notifications/unsubscribe/{token}` (public, sans auth) — décode
   le JWT + pose `channel='none'` + rate limit IP anti-brute-force.

Chaque endpoint délègue à `NotificationService` / `NotificationDispatcher`
/ `NotificationPreferencesService`. Aucune logique métier dans le router.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, Depends, Path, Query, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth.guards import get_current_user
from app.core.auth.unsubscribe_tokens import decode_unsubscribe_token
from app.core.database.postgres import get_db
from app.core.errors.exceptions import UnsubscribeSecurityRefusedException
from app.core.security.rate_limiter import check_ip_rate_limit
from app.features.auth.models import User
from app.features.notifications.preferences import (
    NotificationPreferencesService,
    PreferenceEntry,
)
from app.features.notifications.schemas import (
    MarkReadRequest,
    MarkReadResponse,
    NotificationListItem,
    NotificationPreferenceItem,
    NotificationPreferencesResponse,
    NotificationsPage,
    UnsubscribeConfirmationResponse,
    UpdatePreferencesRequest,
)
from app.features.notifications.service import (
    NotificationService,
    apply_unsubscribe,
)
from app.shared.schemas import NexyaResponse

log = structlog.get_logger()

router = APIRouter(tags=["notifications"])


# ═══════════════════════════════════════════════════════════════════
# TIMELINE — /notifications/*
# ═══════════════════════════════════════════════════════════════════


@router.get(
    "/notifications",
    response_model=NexyaResponse[NotificationsPage],
)
async def list_notifications(
    cursor: str | None = Query(default=None, max_length=256),
    limit: int = Query(default=20, ge=1, le=50),
    unread_only: bool = Query(default=False),
    category: str | None = Query(
        default=None,
        min_length=1,
        max_length=32,
        description=(
            "Filtre par catégorie. Valeurs : tasks, payments, security, "
            "digest, product. Absent = toutes catégories."
        ),
    ),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[NotificationsPage]:
    """Timeline paginée keyset — tri `sent_at DESC`.

    Le client (Flutter) charge la première page sans `cursor`, puis
    renvoie `next_cursor` tel quel pour les pages suivantes. `next_cursor=null`
    signale la fin. Le filtre `category` accepte les 5 valeurs RGPD ; le
    Flutter mappe via `data.subtype` si affichage par sous-type produit.
    """
    page = await NotificationService.list_for_user(
        current_user,
        db,
        cursor=cursor,
        limit=limit,
        unread_only=unread_only,
        category=category,
    )
    return NexyaResponse(
        success=True,
        data=NotificationsPage(
            items=[NotificationListItem.model_validate(n) for n in page.items],
            next_cursor=page.next_cursor,
        ),
    )


@router.post(
    "/notifications/read",
    response_model=NexyaResponse[MarkReadResponse],
)
async def mark_notifications_read(
    body: MarkReadRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[MarkReadResponse]:
    """Marque un lot de notifications comme lues.

    IDOR-safe : le UPDATE SQL filtre `user_id` dans le WHERE, seules
    les rows de l'user courant sont touchées. Idempotent : les rows
    déjà lues ne sont pas ré-écrites (leur `read_at` original est
    préservé).
    """
    marked = await NotificationService.mark_read(current_user, body.notification_ids, db)
    return NexyaResponse(success=True, data=MarkReadResponse(marked=marked))


@router.delete(
    "/notifications/{notification_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_class=Response,
)
async def delete_notification(
    notification_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Soft-delete — 204 + réponse vide. 404 IDOR-safe si non propriétaire
    ou déjà supprimée (anti-énumération d'UUIDs)."""
    await NotificationService.soft_delete(current_user, notification_id, db)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ═══════════════════════════════════════════════════════════════════
# PRÉFÉRENCES — /user/notification-preferences
# ═══════════════════════════════════════════════════════════════════
# Namespace aligné sur /user/profile + /user/device-token (singulier).


@router.get(
    "/user/notification-preferences",
    response_model=NexyaResponse[NotificationPreferencesResponse],
)
async def get_notification_preferences(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[NotificationPreferencesResponse]:
    """Retourne les 5 catégories RGPD, avec defaults injectés pour
    celles sans row (pattern cohérent : l'user ne voit jamais un
    état incomplet)."""
    entries = await NotificationPreferencesService.get_for_user(current_user.id, db)
    return NexyaResponse(
        success=True,
        data=NotificationPreferencesResponse(
            preferences=[
                NotificationPreferenceItem(category=e.category, channel=e.channel) for e in entries
            ],
        ),
    )


@router.put(
    "/user/notification-preferences",
    response_model=NexyaResponse[NotificationPreferencesResponse],
)
async def update_notification_preferences(
    body: UpdatePreferencesRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[NotificationPreferencesResponse]:
    """PATCH partial : seules les catégories envoyées sont UPSERT,
    les autres gardent leur valeur actuelle (ou leur default).

    Le validator Pydantic rejette 422 les doublons de catégorie dans
    la même requête.
    """
    entries = await NotificationPreferencesService.set_for_user(
        current_user.id,
        [
            PreferenceEntry(category=item.category, channel=item.channel)
            for item in body.preferences
        ],
        db,
    )
    return NexyaResponse(
        success=True,
        data=NotificationPreferencesResponse(
            preferences=[
                NotificationPreferenceItem(category=e.category, channel=e.channel) for e in entries
            ],
        ),
    )


# ═══════════════════════════════════════════════════════════════════
# UNSUBSCRIBE one-click — /notifications/unsubscribe/{token}
# ═══════════════════════════════════════════════════════════════════
# Endpoint PUBLIC (sans auth) : le token JWT est lui-même l'autorisation.
# Rate limit IP anti-brute-force (10/h/IP par défaut).


@router.post(
    "/notifications/unsubscribe/{token}",
    response_model=NexyaResponse[UnsubscribeConfirmationResponse],
)
async def unsubscribe_one_click(
    request: Request,
    token: str = Path(..., min_length=20, max_length=4096),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[UnsubscribeConfirmationResponse]:
    """Désinscription one-click RGPD/CAN-SPAM.

    Pipeline :
    1. Rate limit IP (`unsubscribe_rate_limit_per_hour`, défaut 10/h/IP) —
       coupe un script qui brute-force des tokens.
    2. Décode le JWT RS256 (purpose=email_unsubscribe, 365j TTL) → raise
       `UnsubscribeTokenExpiredException` (400) ou
       `UnsubscribeTokenInvalidException` (400).
    3. Refuse la catégorie `security` → 400 `UNSUBSCRIBE_SECURITY_REFUSED`
       (obligation légale de notifier les événements sécurité).
    4. UPSERT `notification_preferences(user_id, category).channel='none'`.
    5. Retour 200 avec confirmation.

    Aucune leak sur l'existence du user : un token forgé ou expiré reçoit
    un 400 générique, jamais un 404 ou 500.
    """
    await check_ip_rate_limit(
        request,
        action="unsubscribe",
        max_requests=settings.unsubscribe_rate_limit_per_hour,
        window_seconds=3600,
    )

    payload = decode_unsubscribe_token(token)
    category = payload["cat"]
    user_id = uuid.UUID(payload["sub"])

    if category == "security":
        raise UnsubscribeSecurityRefusedException()

    await apply_unsubscribe(user_id, category, db)

    log.info(
        "notifications.unsubscribed_one_click",
        user_id=str(user_id),
        category=category,
    )

    return NexyaResponse(
        success=True,
        data=UnsubscribeConfirmationResponse(category=category),
    )
