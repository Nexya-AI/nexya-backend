"""
NotificationService + NotificationDispatcher — F3.

Deux responsabilités distinctes dans le même fichier pour éviter la
dispersion (le dispatcher consomme le service en interne) :

1. `NotificationService` — CRUD de la table `notifications` (timeline
   in-app). Méthodes statiques, pattern aligné sur les autres services
   NEXYA (ConversationService, LibraryService, ...).

2. `NotificationDispatcher` — orchestrateur dual-channel (push FCM +
   email fallback) qui est appelé par le worker Planner (et futurs
   émetteurs : webhook paiement, event sécurité, etc.). Fail-safe
   absolu : ne lève JAMAIS d'exception (appelé depuis arq qui ne doit
   pas crasher).
"""

from __future__ import annotations

import asyncio
import base64
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import func, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.fcm import (
    FCMProvider,
    FCMResult,
    FCMUnavailableError,
    FCMUnregisteredError,
    get_fcm_provider,
)
from app.config import settings
from app.core.auth.unsubscribe_tokens import create_unsubscribe_token
from app.core.email import EmailMessage, get_email_service, get_template_renderer
from app.core.email.base import EmailSendException
from app.core.errors.exceptions import (
    ResourceNotFoundException,
    ValidationException,
)
from app.core.observability import (
    get_tracer,
    record_fcm_failure,
    record_notification_dispatch,
)
from app.features.auth import service as auth_service
from app.features.auth.models import User
from app.features.notifications.models import Notification
from app.features.notifications.preferences import (
    NotificationPreferencesService,
)

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# Curseurs keyset opaques (base64url) — pattern identique aux autres services
# ═══════════════════════════════════════════════════════════════════


def _encode_cursor(ts: datetime, row_id: uuid.UUID) -> str:
    payload = f"{ts.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationException("Curseur invalide.") from exc
    if "|" not in decoded:
        raise ValidationException("Curseur malformé.")
    iso_part, id_part = decoded.split("|", 1)
    try:
        ts = datetime.fromisoformat(iso_part)
    except ValueError as exc:
        raise ValidationException("Curseur invalide.") from exc
    try:
        row_id = uuid.UUID(id_part)
    except ValueError as exc:
        raise ValidationException("Curseur UUID invalide.") from exc
    return ts, row_id


# ═══════════════════════════════════════════════════════════════════
# DTO internes
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class NotificationsPageOrm:
    items: list[Notification]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class _PushOutcome:
    success: bool
    message_id: str | None
    attempts: int
    removed_tokens: int
    all_tokens_unregistered: bool
    had_no_tokens: bool


@dataclass(frozen=True, slots=True)
class _EmailOutcome:
    success: bool
    message_id: str | None
    attempts: int
    skipped_reason: str | None = None


# ═══════════════════════════════════════════════════════════════════
# NotificationService — CRUD timeline
# ═══════════════════════════════════════════════════════════════════


class NotificationService:
    """CRUD table `notifications`. Méthodes statiques, stateless."""

    # ── Internal helper IDOR-safe ─────────────────────────────────
    @staticmethod
    async def _get_owned(notif_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession) -> Notification:
        result = await db.execute(
            select(Notification).where(
                Notification.id == notif_id,
                Notification.user_id == user_id,
                Notification.deleted_at.is_(None),
            )
        )
        notif = result.scalar_one_or_none()
        if notif is None:
            raise ResourceNotFoundException("Notification")
        return notif

    # ── CREATE (consommé par dispatcher uniquement) ──────────────
    @staticmethod
    async def create(
        *,
        user_id: uuid.UUID,
        category: str,
        title: str,
        body: str,
        data: dict[str, Any] | None,
        channel_used: str,
        source_task_id: uuid.UUID | None,
        source_kind: str,
        push_message_id: str | None,
        email_message_id: str | None,
        attempts_push: int,
        attempts_email: int,
        db: AsyncSession,
    ) -> Notification:
        notif = Notification(
            user_id=user_id,
            category=category,
            title=title,
            body=body,
            data_json=data or {},
            channel_used=channel_used,
            source_task_id=source_task_id,
            source_kind=source_kind,
            push_message_id=push_message_id,
            email_message_id=email_message_id,
            attempts_push=attempts_push,
            attempts_email=attempts_email,
        )
        db.add(notif)
        await db.commit()
        await db.refresh(notif)
        return notif

    # ── LIST paginée keyset ───────────────────────────────────────
    @staticmethod
    async def list_for_user(
        user: User,
        db: AsyncSession,
        *,
        cursor: str | None = None,
        limit: int = 20,
        unread_only: bool = False,
        category: str | None = None,
    ) -> NotificationsPageOrm:
        effective_limit = max(1, min(int(limit or 20), 50))
        conditions: list = [
            Notification.user_id == user.id,
            Notification.deleted_at.is_(None),
        ]
        if unread_only:
            conditions.append(Notification.read_at.is_(None))
        if category is not None:
            conditions.append(Notification.category == category)
        if cursor:
            cur_sent_at, cur_id = _decode_cursor(cursor)
            conditions.append(
                tuple_(Notification.sent_at, Notification.id) < tuple_(cur_sent_at, cur_id)
            )

        stmt = (
            select(Notification)
            .where(*conditions)
            .order_by(Notification.sent_at.desc(), Notification.id.desc())
            .limit(effective_limit + 1)
        )
        rows = list((await db.execute(stmt)).scalars().all())
        has_more = len(rows) > effective_limit
        items = rows[:effective_limit]
        next_cursor = None
        if has_more and items:
            last = items[-1]
            next_cursor = _encode_cursor(last.sent_at, last.id)
        return NotificationsPageOrm(items=items, next_cursor=next_cursor)

    # ── MARK READ (idempotent, bulk) ──────────────────────────────
    @staticmethod
    async def mark_read(
        user: User,
        notification_ids: list[uuid.UUID],
        db: AsyncSession,
    ) -> int:
        """UPDATE read_at=NOW() pour les IDs qui appartiennent à l'user
        et qui sont encore non-lus. Retourne le nombre effectif.

        IDOR-safe par construction (filtre `user_id` dans le WHERE).
        Idempotent : un ID déjà lu n'est pas ré-écrit (clause `read_at
        IS NULL`), ce qui préserve le `read_at` original.
        """
        if not notification_ids:
            return 0
        now = datetime.now(tz=UTC)
        result = await db.execute(
            update(Notification)
            .where(
                Notification.user_id == user.id,
                Notification.id.in_(notification_ids),
                Notification.read_at.is_(None),
                Notification.deleted_at.is_(None),
            )
            .values(read_at=now, updated_at=now)
        )
        await db.commit()
        return int(result.rowcount or 0)

    # ── SOFT DELETE ──────────────────────────────────────────────
    @staticmethod
    async def soft_delete(user: User, notification_id: uuid.UUID, db: AsyncSession) -> None:
        """404 IDOR-safe (jamais 403). Idempotent : une row déjà
        soft-deleted renvoie 404 aussi (elle n'est plus visible)."""
        notif = await NotificationService._get_owned(notification_id, user.id, db)
        notif.deleted_at = func.now()
        notif.updated_at = func.now()
        await db.commit()


# ═══════════════════════════════════════════════════════════════════
# NotificationDispatcher — orchestrateur dual-channel
# ═══════════════════════════════════════════════════════════════════


class NotificationDispatcher:
    """Dispatche une notification selon les préférences user.

    Pipeline strict (fail-safe absolu) :

    1. Lookup `channel_preference` via `NotificationPreferencesService`.
    2. Si `none` → INSERT row `channel_used='skipped'` + log + return.
    3. Si `push` ou `both` → `_try_push` (FCM) avec soft-delete auto
       des tokens UNREGISTERED.
    4. Si `email` ou `both` OU (push KO + fallback activé) → `_try_email`
       (Brevo/Mock) avec template catégorie.
    5. Calcul `channel_used` final selon les succès partiels.
    6. INSERT row `notifications` avec tous les détails tracés.
    7. Log forensic `notifications.dispatched`.

    Fail-safe : **aucune exception** ne remonte du `dispatch`. Une panne
    FCM/Brevo/DB est loggée et la fonction retourne None. Le caller
    (worker arq) peut continuer son travail sans se préoccuper de
    l'envoi.
    """

    # ── Point d'entrée principal ──────────────────────────────────
    @staticmethod
    async def dispatch(
        *,
        user: User,
        category: str,
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
        source_task_id: uuid.UUID | None = None,
        source_kind: str = "manual",
        db: AsyncSession,
    ) -> Notification | None:
        """Orchestre l'envoi push + email selon préférence user.

        Retourne la row `Notification` insérée (None en cas d'échec
        global catastrophique — DB down).
        """
        # K1 — span OTel parent qui couvre tout le pipeline notif.
        # Les spans enfants HTTP (httpx FCM, httpx Brevo) seront
        # auto-générés par les instrumentors. Les attributs détaillés
        # (channel_used, attempts) sont posés à la fin du dispatch.
        tracer = get_tracer()
        with tracer.start_as_current_span(
            "notifications.dispatch",
            attributes={
                "notif.category": category,
                "notif.source_kind": source_kind,
            },
        ):
            return await NotificationDispatcher._dispatch_inner(
                user=user,
                category=category,
                title=title,
                body=body,
                data=data,
                source_task_id=source_task_id,
                source_kind=source_kind,
                db=db,
            )

    # ── Implémentation interne (instrumentée par dispatch) ────────
    @staticmethod
    async def _dispatch_inner(
        *,
        user: User,
        category: str,
        title: str,
        body: str,
        data: dict[str, Any] | None = None,
        source_task_id: uuid.UUID | None = None,
        source_kind: str = "manual",
        db: AsyncSession,
    ) -> Notification | None:
        try:
            preferred_channel = await NotificationPreferencesService.get_channel_for_category(
                user.id, category, db
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "notifications.dispatcher.prefs_lookup_failed",
                user_id=str(user.id),
                category=category,
                error=str(exc),
            )
            preferred_channel = "none"

        data_payload = dict(data or {})

        # Cas : l'user a refusé cette catégorie → skipped.
        if preferred_channel == "none":
            return await NotificationDispatcher._persist_row(
                user=user,
                category=category,
                title=title,
                body=body,
                data=data_payload,
                channel_used="skipped",
                source_task_id=source_task_id,
                source_kind=source_kind,
                push_message_id=None,
                email_message_id=None,
                attempts_push=0,
                attempts_email=0,
                db=db,
            )

        # ── Tentative push ─────────────────────────────────────────
        want_push = preferred_channel in {"push", "both"}
        push_outcome: _PushOutcome | None = None
        if want_push:
            push_outcome = await NotificationDispatcher._try_push(
                user_id=user.id,
                title=title,
                body=body,
                data_payload=data_payload,
                db=db,
            )

        # ── Tentative email ────────────────────────────────────────
        want_email_direct = preferred_channel in {"email", "both"}
        want_email_fallback = (
            preferred_channel == "push"
            and push_outcome is not None
            and not push_outcome.success
            and settings.notification_fallback_email_enabled
        )

        email_outcome: _EmailOutcome | None = None
        if want_email_direct or want_email_fallback:
            email_outcome = await NotificationDispatcher._try_email(
                user=user,
                category=category,
                title=title,
                body=body,
                data_payload=data_payload,
                source_task_id=source_task_id,
            )

        # ── Calcul du channel_used final ──────────────────────────
        push_ok = push_outcome is not None and push_outcome.success
        email_ok = email_outcome is not None and email_outcome.success
        if push_ok and email_ok:
            channel_used = "both"
        elif push_ok:
            channel_used = "push"
        elif email_ok:
            channel_used = "email"
        else:
            channel_used = "skipped"

        attempts_push = push_outcome.attempts if push_outcome else 0
        attempts_email = email_outcome.attempts if email_outcome else 0
        push_msg_id = push_outcome.message_id if push_outcome else None
        email_msg_id = email_outcome.message_id if email_outcome else None

        log.info(
            "notifications.dispatched",
            user_id=str(user.id),
            category=category,
            preferred_channel=preferred_channel,
            channel_used=channel_used,
            attempts_push=attempts_push,
            attempts_email=attempts_email,
            push_success=push_ok,
            email_success=email_ok,
            fallback_triggered=want_email_fallback,
            source_kind=source_kind,
        )

        # K1 — span attributes + métrique Prometheus
        try:
            from opentelemetry import trace as _otel_trace

            current_span = _otel_trace.get_current_span()
            if current_span is not None:
                current_span.set_attribute("notif.category", category)
                current_span.set_attribute("notif.channel_used", channel_used)
                current_span.set_attribute("notif.attempts_push", attempts_push)
                current_span.set_attribute("notif.attempts_email", attempts_email)
                current_span.set_attribute("notif.fallback_triggered", want_email_fallback)
        except Exception:  # noqa: BLE001
            pass
        try:
            record_notification_dispatch(category, channel_used)
        except Exception:  # noqa: BLE001
            pass

        return await NotificationDispatcher._persist_row(
            user=user,
            category=category,
            title=title,
            body=body,
            data=data_payload,
            channel_used=channel_used,
            source_task_id=source_task_id,
            source_kind=source_kind,
            push_message_id=push_msg_id,
            email_message_id=email_msg_id,
            attempts_push=attempts_push,
            attempts_email=attempts_email,
            db=db,
        )

    # ── Persistance de la row (fail-safe) ──────────────────────────
    @staticmethod
    async def _persist_row(
        *,
        user: User,
        category: str,
        title: str,
        body: str,
        data: dict[str, Any],
        channel_used: str,
        source_task_id: uuid.UUID | None,
        source_kind: str,
        push_message_id: str | None,
        email_message_id: str | None,
        attempts_push: int,
        attempts_email: int,
        db: AsyncSession,
    ) -> Notification | None:
        try:
            return await NotificationService.create(
                user_id=user.id,
                category=category,
                title=title,
                body=body,
                data=data,
                channel_used=channel_used,
                source_task_id=source_task_id,
                source_kind=source_kind,
                push_message_id=push_message_id,
                email_message_id=email_message_id,
                attempts_push=attempts_push,
                attempts_email=attempts_email,
                db=db,
            )
        except Exception as exc:  # noqa: BLE001 — DB down tolérée
            log.warning(
                "notifications.dispatcher.persist_failed",
                user_id=str(user.id),
                category=category,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return None

    # ── Push ──────────────────────────────────────────────────────
    @staticmethod
    async def _try_push(
        *,
        user_id: uuid.UUID,
        title: str,
        body: str,
        data_payload: dict[str, Any],
        db: AsyncSession,
    ) -> _PushOutcome:
        """Envoie le push FCM sur chaque device_token actif de l'user.

        Logique :
        - Récupère les tokens actifs via `auth_service.list_active_device_tokens`.
        - Si 0 token → retour avec `had_no_tokens=True, success=False`.
        - Pour chaque token : tente l'envoi via `FCMProvider.send_push`
          en parallèle (`asyncio.gather` avec `return_exceptions=True`).
        - Tri des résultats :
          - Succès → `attempts++`, retient le premier `message_id`.
          - `FCMUnregisteredError` → marque le token pour soft-delete.
          - `FCMUnavailableError` / autre → `attempts++`, ne retient rien.
        - Soft-delete des tokens UNREGISTERED après (commit indépendant).
        - `success=True` dès qu'AU MOINS 1 token a réussi.
        - `all_tokens_unregistered=True` si tous les envois ont raté en
          UNREGISTERED — cas où l'user a changé de device et ses anciens
          tokens sont morts.
        """
        try:
            tokens = await auth_service.list_active_device_tokens(user_id, db)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "notifications.dispatcher.list_tokens_failed",
                user_id=str(user_id),
                error=str(exc),
            )
            return _PushOutcome(
                success=False,
                message_id=None,
                attempts=0,
                removed_tokens=0,
                all_tokens_unregistered=False,
                had_no_tokens=True,
            )

        if not tokens:
            return _PushOutcome(
                success=False,
                message_id=None,
                attempts=0,
                removed_tokens=0,
                all_tokens_unregistered=False,
                had_no_tokens=True,
            )

        # Stringifie les valeurs non-str (FCM HTTP v1 exige des strings
        # côté `data`). Fait ici pour que tout caller soit libre d'envoyer
        # des UUID/int/bool.
        string_data: dict[str, str] = {
            k: (str(v) if v is not None else "") for k, v in data_payload.items()
        }

        provider: FCMProvider = get_fcm_provider()

        async def _send(tok: str):
            return await provider.send_push(tok, title=title, body=body, data=string_data)

        results = await asyncio.gather(*[_send(t) for t in tokens], return_exceptions=True)

        success = False
        message_id: str | None = None
        tokens_to_remove: list[str] = []
        attempts = 0
        unregistered_count = 0

        for tok, outcome in zip(tokens, results):
            attempts += 1
            if isinstance(outcome, FCMUnregisteredError):
                tokens_to_remove.append(tok)
                unregistered_count += 1
                # K1 — métrique FCM failure (UNREGISTERED = device dead)
                try:
                    record_fcm_failure("UNREGISTERED")
                except Exception:  # noqa: BLE001
                    pass
                continue
            if isinstance(outcome, FCMUnavailableError):
                log.warning(
                    "notifications.dispatcher.push_unavailable",
                    user_id=str(user_id),
                    error=str(outcome),
                )
                try:
                    record_fcm_failure("UNAVAILABLE")
                except Exception:  # noqa: BLE001
                    pass
                continue
            if isinstance(outcome, Exception):
                log.warning(
                    "notifications.dispatcher.push_error",
                    user_id=str(user_id),
                    error=str(outcome),
                    error_type=type(outcome).__name__,
                )
                try:
                    record_fcm_failure(type(outcome).__name__)
                except Exception:  # noqa: BLE001
                    pass
                continue
            # Succès
            if isinstance(outcome, FCMResult) and outcome.success:
                success = True
                if message_id is None:
                    message_id = outcome.message_id

        removed = 0
        if tokens_to_remove:
            for tok in tokens_to_remove:
                try:
                    await auth_service.remove_invalid_token(tok, db)
                    removed += 1
                except Exception as exc:  # noqa: BLE001
                    log.warning(
                        "notifications.dispatcher.token_cleanup_failed",
                        user_id=str(user_id),
                        error=str(exc),
                    )

        return _PushOutcome(
            success=success,
            message_id=message_id,
            attempts=attempts,
            removed_tokens=removed,
            all_tokens_unregistered=(
                unregistered_count > 0 and unregistered_count == len(tokens) and not success
            ),
            had_no_tokens=False,
        )

    # ── Email ─────────────────────────────────────────────────────
    @staticmethod
    async def _try_email(
        *,
        user: User,
        category: str,
        title: str,
        body: str,
        data_payload: dict[str, Any],
        source_task_id: uuid.UUID | None,
    ) -> _EmailOutcome:
        """Rend le template associé à `category` + envoie via Brevo/Mock.

        Mapping category → template :
        - `tasks`    → `task_completed` (par défaut) ou `task_reminder`
                       selon `data_payload.get('notification_kind')`.
        - `payments` → `payment_confirmed`.
        - `security` → `account_security_alert`.
        - `digest`   → ? — hors scope F3, skippé silencieusement.
        - `product`  → ? — hors scope F3, skippé silencieusement.

        Pour `security`, l'`unsubscribe_url` est `None` (non-désinscriptible).
        """
        template_name = NotificationDispatcher._resolve_template(category, data_payload)
        if template_name is None:
            log.info(
                "notifications.dispatcher.email_category_no_template",
                category=category,
            )
            return _EmailOutcome(
                success=False,
                message_id=None,
                attempts=0,
                skipped_reason="no_template_for_category",
            )

        # Construction du contexte template.
        unsubscribe_url = NotificationDispatcher._build_unsubscribe_url(user.id, category)
        user_name = user.display_name or user.username or user.email.split("@")[0]
        context: dict[str, Any] = {
            "user_name": user_name,
            "title": title,
            "body": body,
            "data": data_payload,
            "task_deep_link": data_payload.get("deep_link"),
            "unsubscribe_url": unsubscribe_url,
            # Champs spécifiques selon template — le caller enrichit
            # `data_payload` si besoin (payment_confirmed.amount, etc.)
            "task_title": data_payload.get("task_title", title),
            "result_preview": body,
            "scheduled_at_human_readable": data_payload.get("scheduled_at_human_readable"),
            "plan_name": data_payload.get("plan_name"),
            "amount_formatted": data_payload.get("amount_formatted"),
            "invoice_url": data_payload.get("invoice_url"),
            "event_type": data_payload.get("event_type"),
            "event_ip": data_payload.get("event_ip"),
            "event_user_agent_truncated": data_payload.get("event_user_agent_truncated"),
            "event_time_utc": data_payload.get("event_time_utc"),
            "password_reset_url": data_payload.get("password_reset_url"),
        }

        try:
            renderer = get_template_renderer()
            html_body, text_body = renderer.render(template_name, **context)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "notifications.dispatcher.email_render_failed",
                category=category,
                template=template_name,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return _EmailOutcome(
                success=False,
                message_id=None,
                attempts=1,
                skipped_reason="render_failed",
            )

        message = EmailMessage(
            to_email=user.email,
            to_name=user_name,
            subject=title,
            html_body=html_body,
            text_body=text_body,
            tags=[f"notif:{category}", f"template:{template_name}"],
        )

        try:
            service = get_email_service()
            await service.send(message)
        except EmailSendException as exc:
            log.warning(
                "notifications.dispatcher.email_send_failed",
                category=category,
                error=str(exc),
            )
            return _EmailOutcome(
                success=False,
                message_id=None,
                attempts=1,
                skipped_reason="send_failed",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "notifications.dispatcher.email_unexpected_error",
                category=category,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return _EmailOutcome(
                success=False,
                message_id=None,
                attempts=1,
                skipped_reason="unexpected",
            )

        # Brevo renvoie un message_id dans le payload, mais notre ABC
        # EmailService.send() ne le propage pas aujourd'hui (refactor
        # hors scope F3). Place-holder `None` — à enrichir en Phase 13.
        return _EmailOutcome(
            success=True,
            message_id=None,
            attempts=1,
        )

    # ── Helpers ──────────────────────────────────────────────────
    @staticmethod
    def _resolve_template(category: str, data_payload: dict[str, Any]) -> str | None:
        """Map category → nom de template (ou None si pas de template)."""
        if category == "tasks":
            kind = data_payload.get("notification_kind")
            if kind == "reminder":
                return "task_reminder"
            return "task_completed"
        if category == "payments":
            return "payment_confirmed"
        if category == "security":
            return "account_security_alert"
        # `digest` + `product` : pas de template F3 (scope).
        return None

    @staticmethod
    def _build_unsubscribe_url(user_id: uuid.UUID, category: str) -> str | None:
        """Construit l'URL du lien unsubscribe dans l'email footer.

        Retourne `None` pour la catégorie `security` (non-désinscriptible
        par obligation légale) — le footer `{% if unsubscribe_url %}...{%
        endif %}` n'affiche pas la ligne.
        """
        if category == "security":
            return None
        token = create_unsubscribe_token(user_id, category)
        base = (settings.frontend_unsubscribe_url or "").rstrip("?&")
        sep = "&" if "?" in base else "?"
        return f"{base}{sep}token={token}"


# ═══════════════════════════════════════════════════════════════════
# Helper unsubscribe public (consommé par le router)
# ═══════════════════════════════════════════════════════════════════


async def apply_unsubscribe(user_id: uuid.UUID, category: str, db: AsyncSession) -> None:
    """Applique la désinscription one-click sur (user, category).

    Le router décode le token + appelle cette fonction. La catégorie
    `security` est rejetée par le router AVANT d'appeler ce helper,
    donc ici on n'a qu'à poser `channel='none'` via UPSERT.
    """
    await NotificationPreferencesService.set_category_none(user_id, category, db)
