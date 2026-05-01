"""
AuthService — logique métier d'authentification.

Toute la logique est ici. Le router ne fait qu'appeler le service.
Le service orchestre : DB (SQLAlchemy) + Redis (blacklist) + bcrypt (hash).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import bcrypt
import structlog
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth.jwt import blacklist_token, create_access_token
from app.core.auth.password_reset import (
    RESET_TOKEN_TTL_MINUTES,
    create_password_reset_token,
    decode_password_reset_token,
    verify_password_hash_fingerprint,
)
from app.core.auth.refresh import (
    create_refresh_token,
    revoke_all_user_tokens,
    verify_and_rotate_refresh_token,
)
from app.core.email import EmailMessage, get_email_service, get_template_renderer
from app.core.email.base import EmailSendException
from app.core.errors.exceptions import (
    AuthCredentialsInvalidException,
    AuthEmailAlreadyExistsException,
    AuthRefreshExpiredException,
    AuthUsernameAlreadyExistsException,
    CaptchaInvalidException,
    DeviceQuotaExceededException,
    ResourceNotFoundException,
)
from app.core.security.captcha import (
    CaptchaVerifyException,
    get_captcha_verifier,
)
from app.core.security.rate_limiter import (
    _ForgotPasswordEmailThrottled,
    rate_limit_forgot_password_email,
)
from app.core.security.sanitizer import clean_email, clean_text
from app.features.auth.auth_events import log_auth_event
from app.features.auth.device_quotas import (
    check_and_consume_device_quota,
    normalize_device_id,
)
from app.features.auth.models import DeviceToken, User
from app.features.auth.schemas import (
    ChangePasswordRequest,
    DeviceTokenRequest,
    ForgotPasswordRequest,
    LoginRequest,
    RegisterRequest,
    ResetPasswordRequest,
    TokenResponse,
    UpdateProfileRequest,
    UserProfile,
)

log = structlog.get_logger()


def _hash_password(password: str) -> str:
    """Hash bcrypt — lent par design (résistant au brute-force).

    Note importante : bcrypt tronque silencieusement à 72 bytes.
    On tronque EXPLICITEMENT ici pour éviter qu'un utilisateur croie
    qu'un mot de passe de 100 caractères est plus sûr qu'un de 72.
    La validation Pydantic limite déjà à 128 caractères côté entrée.
    """
    password_bytes = password.encode("utf-8")[:72]
    hashed = bcrypt.hashpw(password_bytes, bcrypt.gensalt())
    return hashed.decode("utf-8")


def _verify_password(password: str, password_hash: str) -> bool:
    """Vérifie un mot de passe contre son hash bcrypt."""
    password_bytes = password.encode("utf-8")[:72]
    return bcrypt.checkpw(password_bytes, password_hash.encode("utf-8"))


def _build_token_response(access_token: str, refresh_token: str) -> TokenResponse:
    """Construit la réponse token standardisée."""
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        token_type="bearer",
        expires_in=settings.jwt_access_ttl_minutes * 60,
    )


# ══════════════════════════════════════════════════════════════
# REGISTER — pipeline durci A3
# ══════════════════════════════════════════════════════════════

# On ordonne les checks du moins coûteux au plus coûteux, et du plus
# « cheap to fake » au plus « hard to fake » — c'est une décision
# délibérée d'attaquabilité :
#
#   1. Captcha (preuve d'humanité côté client — coûte 0 DB query)
#   2. Device quota (1 UPSERT DB indépendant, commit immédiat pour
#      que l'incrément survive à un rollback ultérieur)
#   3. Unicité email/username (1 SELECT DB)
#   4. Insert user (1 INSERT + flush)
#   5. Génération tokens (Redis + DB)
#
# Les 3 premières étapes peuvent lever sans avoir encore créé d'user.
# Un `AuthEvent` est loggué pour chaque rejet afin de tracer les
# patterns d'attaque (captcha failed, quota exceeded, email duplicate).


async def register(
    body: RegisterRequest,
    db: AsyncSession,
    *,
    client_ip: str | None = None,
    user_agent: str | None = None,
    device_id_raw: str | None = None,
) -> TokenResponse:
    """Inscrit un nouvel utilisateur — pipeline A3 (captcha + device quota + audit).

    Args:
        body: payload validé par Pydantic (RegisterRequest).
        db: session SQLAlchemy async (commit transverse orchestré ici).
        client_ip: IP extraite par le router depuis X-Forwarded-For /
            `request.client.host`. Stockée dans `auth_events.ip` et
            passée à hCaptcha en double vérification.
        user_agent: header User-Agent brut — tronqué à 256 chars lors
            du log_auth_event. Aide à identifier les bots (UA fixe
            connu) vs clients légitimes Flutter.
        device_id_raw: header X-Device-Id — UUID stable généré par le
            Flutter. Normalisé via `normalize_device_id()` avant usage
            (gère les cas header absent / malformé).

    Raises:
        CaptchaInvalidException: token captcha manquant / rejeté (prod).
        DeviceQuotaExceededException: >N inscriptions/jour/device.
        AuthEmailAlreadyExistsException: email déjà utilisé.
        AuthUsernameAlreadyExistsException: username déjà pris.
    """
    device_id = normalize_device_id(device_id_raw)
    email_normalized = clean_email(body.email)

    # ── Étape 1 — Captcha ──────────────────────────────────────
    # On délègue la politique "quand exiger un captcha" à la factory :
    # si HCAPTCHA_ENABLED=false OU clé vide → Mock accepte tout.
    # En prod, le Mock est remplacé par HCaptchaVerifier qui refuse
    # les tokens absents / invalides. Un fail-open est conservé sur
    # erreur transport (hCaptcha down) pour ne pas bloquer l'inscription
    # en cas de panne du provider.
    await _verify_captcha_or_fail(
        token=body.captcha_token or "",
        client_ip=client_ip,
        user_agent=user_agent,
        device_id=device_id,
        db=db,
    )

    # ── Étape 2 — Device quota ─────────────────────────────────
    # Incrément atomique + commit, indépendamment de la suite du register.
    # Si cette étape lève, on a quand même incrémenté le compteur du
    # device → l'attaquant voit son quota décroître même en cas d'échec,
    # ce qui rend la ferme d'inscriptions peu rentable.
    try:
        await check_and_consume_device_quota(device_id, db, ip=client_ip)
    except DeviceQuotaExceededException:
        await log_auth_event(
            db,
            event_type="device_quota_exceeded",
            ip=client_ip,
            user_agent=user_agent,
            device_id=device_id,
            metadata={"email_hash": _hash_email_log(email_normalized)},
        )
        raise

    # ── Étape 3 — Unicité email / username ─────────────────────
    conditions = [User.email == email_normalized]
    if body.username:
        conditions.append(User.username == body.username.lower())

    result = await db.execute(select(User).where(or_(*conditions)))
    existing = result.scalar_one_or_none()

    if existing is not None:
        reason = "email_taken" if existing.email == email_normalized else "username_taken"
        await log_auth_event(
            db,
            event_type="register_failed",
            ip=client_ip,
            user_agent=user_agent,
            device_id=device_id,
            metadata={
                "reason": reason,
                "email_hash": _hash_email_log(email_normalized),
            },
        )
        if reason == "email_taken":
            raise AuthEmailAlreadyExistsException()
        raise AuthUsernameAlreadyExistsException()

    # ── Étape 4 — Création user ────────────────────────────────
    # Le display_name est déjà nettoyé (NFC + strip) par le
    # field_validator de RegisterRequest. On garde un fallback vers
    # username/local-part si aucun display_name n'a été fourni.
    display_name_fallback = clean_text(
        body.username or email_normalized.split("@")[0],
        max_length=100,
        collapse_whitespace=True,
    )
    user = User(
        email=email_normalized,
        username=body.username,
        password_hash=_hash_password(body.password),
        display_name=body.display_name or display_name_fallback,
    )
    db.add(user)
    await db.flush()  # flush pour obtenir user.id avant le commit

    # ── Étape 5 — Tokens + audit ──────────────────────────────
    access_token = create_access_token(user.id, user.plan)
    refresh_token = await create_refresh_token(user.id, db)

    await log_auth_event(
        db,
        event_type="register_success",
        user_id=user.id,
        ip=client_ip,
        user_agent=user_agent,
        device_id=device_id,
    )

    log.info("auth.register.success", user_id=str(user.id), device_id=device_id)
    return _build_token_response(access_token, refresh_token)


async def _verify_captcha_or_fail(
    *,
    token: str,
    client_ip: str | None,
    user_agent: str | None,
    device_id: str,
    db: AsyncSession,
) -> None:
    """Exécute la vérif captcha. Lève `CaptchaInvalidException` si
    rejet, fail-open sur erreur transport (hCaptcha down).

    Politique fail-open délibérée : mieux vaut laisser passer quelques
    inscriptions suspectes qu'ouvrir un vecteur de DoS où un attaquant
    saturerait hCaptcha pour bloquer toutes les inscriptions légitimes.
    Le device_quota reste actif comme garde-fou secondaire.
    """
    verifier = get_captcha_verifier()

    try:
        result = await verifier.verify(token, remote_ip=client_ip)
    except CaptchaVerifyException as exc:
        # Transport error — on laisse passer mais on trace.
        log.warning(
            "auth.register.captcha_transport_error",
            error=str(exc),
            device_id=device_id,
        )
        return

    if not result.success:
        await log_auth_event(
            db,
            event_type="captcha_failed",
            ip=client_ip,
            user_agent=user_agent,
            device_id=device_id,
            metadata={"error_codes": list(result.error_codes)},
        )
        raise CaptchaInvalidException()


# ══════════════════════════════════════════════════════════════
# LOGIN
# ══════════════════════════════════════════════════════════════


async def login(
    body: LoginRequest,
    db: AsyncSession,
    *,
    client_ip: str | None = None,
    user_agent: str | None = None,
    device_id_raw: str | None = None,
) -> TokenResponse:
    """Connecte un utilisateur existant.

    1. Cherche l'utilisateur par email
    2. Vérifie le mot de passe avec bcrypt
    3. Émet `login_success` ou `login_failed` dans `auth_events`
    4. Génère le couple access + refresh token en cas de succès

    `client_ip`, `user_agent` et `device_id_raw` sont tracés dans
    `auth_events` pour détecter brute-force (N échecs consécutifs
    depuis la même IP) et compromission (même user vu sur un device
    inhabituel).
    """
    email_normalized = clean_email(body.email)
    device_id = normalize_device_id(device_id_raw)

    result = await db.execute(
        select(User).where(
            User.email == email_normalized,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )
    user = result.scalar_one_or_none()

    if user is None or not _verify_password(body.password, user.password_hash):
        await log_auth_event(
            db,
            event_type="login_failed",
            user_id=user.id if user is not None else None,
            ip=client_ip,
            user_agent=user_agent,
            device_id=device_id,
            metadata={"email_hash": _hash_email_log(email_normalized)},
        )
        # Message volontairement vague — ne pas révéler si l'email existe
        raise AuthCredentialsInvalidException()

    access_token = create_access_token(user.id, user.plan)
    refresh_token = await create_refresh_token(user.id, db)

    await log_auth_event(
        db,
        event_type="login_success",
        user_id=user.id,
        ip=client_ip,
        user_agent=user_agent,
        device_id=device_id,
    )

    log.info("auth.login.success", user_id=str(user.id), device_id=device_id)
    return _build_token_response(access_token, refresh_token)


# ══════════════════════════════════════════════════════════════
# REFRESH
# ══════════════════════════════════════════════════════════════


async def refresh(raw_refresh_token: str, db: AsyncSession) -> TokenResponse:
    """Renouvelle le couple access + refresh token via rotation.

    L'ancien refresh token est révoqué, un nouveau est créé.
    """
    old_token, new_raw_token = await verify_and_rotate_refresh_token(raw_refresh_token, db)

    if old_token is None:
        raise AuthRefreshExpiredException()

    # Charger l'utilisateur pour le nouveau access token
    result = await db.execute(
        select(User).where(
            User.id == old_token.user_id,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        raise AuthRefreshExpiredException()

    access_token = create_access_token(user.id, user.plan)

    log.info("auth.refresh.success", user_id=str(user.id))
    return _build_token_response(access_token, new_raw_token)


# ══════════════════════════════════════════════════════════════
# LOGOUT
# ══════════════════════════════════════════════════════════════


async def logout(
    user: User,
    access_jti: str,
    access_exp: datetime,
    db: AsyncSession,
    *,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Déconnecte l'utilisateur.

    1. Blacklist l'access token en cours (Redis)
    2. Révoque tous les refresh tokens de l'utilisateur (DB)
    3. Émet `logout` dans `auth_events`
    """
    await blacklist_token(access_jti, access_exp)
    await revoke_all_user_tokens(user.id, db)
    await log_auth_event(
        db,
        event_type="logout",
        user_id=user.id,
        ip=client_ip,
        user_agent=user_agent,
    )
    log.info("auth.logout.success", user_id=str(user.id))


# ══════════════════════════════════════════════════════════════
# PROFILE
# ══════════════════════════════════════════════════════════════


def get_profile(user: User) -> UserProfile:
    """Retourne le profil de l'utilisateur courant."""
    return UserProfile.model_validate(user)


async def update_profile(
    user: User,
    body: UpdateProfileRequest,
    db: AsyncSession,
) -> UserProfile:
    """Met à jour le profil de l'utilisateur.

    Seuls les champs non-None sont mis à jour (partial update).
    """
    # Vérifier unicité du username si modifié
    if body.username and body.username != user.username:
        result = await db.execute(
            select(User).where(
                User.username == body.username,
                User.id != user.id,
            )
        )
        if result.scalar_one_or_none() is not None:
            raise AuthUsernameAlreadyExistsException()

    update_data = body.model_dump(exclude_none=True)
    for field, value in update_data.items():
        setattr(user, field, value)

    user.updated_at = datetime.now(UTC)
    await db.flush()

    log.info("auth.profile.updated", user_id=str(user.id), fields=list(update_data.keys()))
    return UserProfile.model_validate(user)


# ══════════════════════════════════════════════════════════════
# CHANGE PASSWORD
# ══════════════════════════════════════════════════════════════


async def change_password(
    user: User,
    body: ChangePasswordRequest,
    db: AsyncSession,
    *,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Change le mot de passe de l'utilisateur.

    1. Vérifie l'ancien mot de passe
    2. Hash le nouveau avec bcrypt
    3. Révoque tous les refresh tokens (force la reconnexion partout)
    4. Émet `password_change` dans `auth_events`
    """
    if not _verify_password(body.current_password, user.password_hash):
        raise AuthCredentialsInvalidException()

    user.password_hash = _hash_password(body.new_password)
    user.updated_at = datetime.now(UTC)
    await db.flush()

    # Révoquer tous les tokens — l'utilisateur devra se reconnecter
    await revoke_all_user_tokens(user.id, db)

    await log_auth_event(
        db,
        event_type="password_change",
        user_id=user.id,
        ip=client_ip,
        user_agent=user_agent,
    )
    log.info("auth.password.changed", user_id=str(user.id))


# ══════════════════════════════════════════════════════════════
# FORGOT / RESET PASSWORD
# ══════════════════════════════════════════════════════════════


async def forgot_password(
    body: ForgotPasswordRequest,
    db: AsyncSession,
    *,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Déclenche l'envoi d'un email de reset mot de passe.

    Anti-enumeration : cette fonction ne lève JAMAIS d'exception
    liée à l'existence du compte. Le router retourne un 200 générique
    que le compte existe ou non — un attaquant ne peut pas savoir
    si un email est enregistré.

    Side-effects :
    1. Cherche l'user par email
    2. Si trouvé + rate limit email pas dépassé : génère un token de reset
       et envoie l'email via le provider configuré
    3. Si trouvé + rate limit email dépassé : silencieusement no-op
    4. Si non trouvé : silencieusement no-op (même temps de réponse approximatif)
    """
    email_normalized = body.email.lower().strip()

    result = await db.execute(
        select(User).where(
            User.email == email_normalized,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        # Compte inexistant ou supprimé : ne rien faire
        log.info("auth.forgot_password.no_account", email_hash=_hash_email_log(email_normalized))
        return

    # Rate limit email-scoped : 3 demandes/heure/email (sans fuiter côté client)
    try:
        await rate_limit_forgot_password_email(email_normalized)
    except _ForgotPasswordEmailThrottled:
        log.info("auth.forgot_password.email_throttled", user_id=str(user.id))
        return

    # Génération du token signé RS256 (15 min TTL, lié au hash courant)
    token = create_password_reset_token(user.id, user.password_hash)
    reset_url = f"{settings.frontend_password_reset_url}?token={token}"

    renderer = get_template_renderer()
    html_body, text_body = renderer.render(
        "password_reset",
        user_name=user.display_name or user.username or "",
        reset_url=reset_url,
        expires_minutes=RESET_TOKEN_TTL_MINUTES,
        # F3 : le footer partiel inclus dans le template password_reset
        # exige `unsubscribe_url` dans le contexte (StrictUndefined sinon
        # crash). La catégorie `security` — dont le reset password fait
        # partie — est non-désinscriptible par obligation légale : on
        # passe `None` pour que le partiel n'affiche pas la ligne
        # unsubscribe (le `{% if unsubscribe_url %}...{% endif %}` masque
        # proprement).
        unsubscribe_url=None,
    )

    message = EmailMessage(
        to_email=user.email,
        to_name=user.display_name,
        subject="Réinitialisation de votre mot de passe NEXYA",
        html_body=html_body,
        text_body=text_body,
        tags=["password_reset"],
    )

    try:
        await get_email_service().send(message)
    except EmailSendException as exc:
        # On loggue et on reste silencieux côté client — un échec provider
        # ne doit pas révéler que le compte existe ni crasher le endpoint.
        log.error(
            "auth.forgot_password.email_failed",
            user_id=str(user.id),
            error=str(exc),
        )
        return

    await log_auth_event(
        db,
        event_type="password_reset_request",
        user_id=user.id,
        ip=client_ip,
        user_agent=user_agent,
    )
    log.info("auth.forgot_password.email_sent", user_id=str(user.id))


async def reset_password(
    body: ResetPasswordRequest,
    db: AsyncSession,
    *,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Reset effectif du mot de passe via token signé.

    1. Décode + valide le JWT (purpose, TTL)
    2. Charge l'utilisateur, vérifie le fingerprint du hash (anti-replay)
    3. Hash le nouveau mot de passe
    4. Révoque tous les refresh tokens (sécurité post-reset)

    Lève :
        ResetTokenExpiredException : TTL dépassé
        ResetTokenInvalidException : signature ko, purpose ko,
            ou mot de passe déjà changé (fingerprint mismatch), ou
            compte inexistant / désactivé.
    """
    from app.core.errors.exceptions import ResetTokenInvalidException

    payload = decode_password_reset_token(body.token)  # raises expired/invalid

    user_id_raw = payload["sub"]
    try:
        user_id = uuid.UUID(user_id_raw)
    except (TypeError, ValueError) as exc:
        raise ResetTokenInvalidException() from exc

    result = await db.execute(
        select(User).where(
            User.id == user_id,
            User.is_active.is_(True),
            User.deleted_at.is_(None),
        )
    )
    user = result.scalar_one_or_none()

    if user is None:
        # Compte supprimé entre l'émission du token et son usage
        raise ResetTokenInvalidException()

    # Vérifie que le hash courant correspond encore au fingerprint du token.
    # Bloque le replay : dès qu'un reset est fait, le fingerprint change et
    # tous les tokens précédemment émis deviennent invalides.
    verify_password_hash_fingerprint(payload, user.password_hash)

    user.password_hash = _hash_password(body.new_password)
    user.updated_at = datetime.now(UTC)
    await db.flush()

    # Révoque tous les refresh tokens — sécurité : on assume que la demande
    # de reset peut venir d'un compte compromis, toutes les sessions en cours
    # sont donc invalidées.
    await revoke_all_user_tokens(user.id, db)

    await log_auth_event(
        db,
        event_type="password_reset_success",
        user_id=user.id,
        ip=client_ip,
        user_agent=user_agent,
    )
    log.info("auth.reset_password.success", user_id=str(user.id))


def _hash_email_log(email: str) -> str:
    """Hash court pour les logs — ne pas révéler l'email tenté."""
    import hashlib

    return hashlib.sha256(email.encode("utf-8")).hexdigest()[:12]


# ══════════════════════════════════════════════════════════════
# DELETE ACCOUNT (RGPD — anonymisation)
# ══════════════════════════════════════════════════════════════


async def delete_account(
    user: User,
    access_jti: str,
    access_exp: datetime,
    db: AsyncSession,
    *,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """Supprime (anonymise) le compte utilisateur — RGPD.

    On ne supprime PAS les données — on anonymise :
    - email → deleted_<uuid>@nexya.ai
    - username → None
    - display_name → "Utilisateur supprimé"
    - password_hash → chaîne invalide (impossible de se reconnecter)
    - is_active → False
    - deleted_at → now

    Les conversations et données associées restent en DB
    mais ne sont plus liées à une identité réelle.
    """
    now = datetime.now(UTC)

    user.email = f"deleted_{uuid.uuid4().hex[:12]}@nexya.ai"
    user.username = None
    user.display_name = "Utilisateur supprime"
    user.avatar_url = None
    user.bio = None
    user.password_hash = "DELETED_ACCOUNT"
    user.is_active = False
    user.deleted_at = now
    user.updated_at = now

    await db.flush()

    # Blacklist l'access token + révoquer tous les refresh tokens
    await blacklist_token(access_jti, access_exp)
    await revoke_all_user_tokens(user.id, db)

    # Audit forensic — l'entrée `account_delete` persistera même après
    # l'anonymisation de l'user (`auth_events.user_id` FK ON DELETE SET NULL).
    # C'est la trace que la demande de suppression a bien été honorée.
    await log_auth_event(
        db,
        event_type="account_delete",
        user_id=user.id,
        ip=client_ip,
        user_agent=user_agent,
    )

    log.info("auth.account.deleted", user_id=str(user.id))


# ══════════════════════════════════════════════════════════════
# DEVICE TOKENS (FCM — notifications push)
# ══════════════════════════════════════════════════════════════


async def register_device_token(
    user: User,
    body: DeviceTokenRequest,
    db: AsyncSession,
) -> None:
    """Enregistre un token FCM pour les notifications push.

    Si le token existe déjà (même appareil), on met à jour le user_id
    (cas où l'utilisateur change de compte sur le même appareil).
    """
    result = await db.execute(select(DeviceToken).where(DeviceToken.token == body.token))
    existing = result.scalar_one_or_none()

    if existing:
        existing.user_id = user.id
        existing.platform = body.platform
        existing.is_active = True
        existing.updated_at = datetime.now(UTC)
    else:
        device = DeviceToken(
            user_id=user.id,
            token=body.token,
            platform=body.platform,
        )
        db.add(device)

    await db.flush()
    log.info("auth.device_token.registered", user_id=str(user.id), platform=body.platform)


async def unregister_device_token(
    user: User,
    token: str,
    db: AsyncSession,
) -> None:
    """Désactive un token FCM (déconnexion d'un appareil)."""
    result = await db.execute(
        select(DeviceToken).where(
            DeviceToken.token == token,
            DeviceToken.user_id == user.id,
        )
    )
    device = result.scalar_one_or_none()

    if device is None:
        raise ResourceNotFoundException("Token d'appareil")

    device.is_active = False
    device.updated_at = datetime.now(UTC)
    await db.flush()

    log.info("auth.device_token.unregistered", user_id=str(user.id))


# ══════════════════════════════════════════════════════════════
# DEVICE TOKENS — helpers worker (Session F2)
# ══════════════════════════════════════════════════════════════
# Ces helpers sont consommés par `workers/scheduler_tasks.execute_scheduled_task`
# (hook FCM post-exécution de tâche planifiée). Ils ouvrent volontairement
# une recherche/écriture par `user_id` pour un contrat simple côté worker.


async def list_active_device_tokens(user_id: uuid.UUID, db: AsyncSession) -> list[str]:
    """Liste les tokens FCM actifs d'un utilisateur.

    Utilisé par le worker Planner pour savoir à quel(s) device(s) envoyer
    le push après l'exécution d'une tâche. Un user sans device actif =
    liste vide (le worker skip silencieusement l'envoi).
    """
    result = await db.execute(
        select(DeviceToken.token).where(
            DeviceToken.user_id == user_id,
            DeviceToken.is_active.is_(True),
        )
    )
    return [row for row in result.scalars().all() if row]


async def remove_invalid_token(token: str, db: AsyncSession) -> None:
    """Désactive un token FCM retourné `UNREGISTERED` par Firebase.

    Housekeeping automatique : quand FCM répond que le token est expiré
    ou a été désinstallé côté client, on marque la ligne `is_active=False`
    pour ne plus spammer ce token aux prochains push. Idempotent et
    fail-safe : si la row a déjà disparu (2 workers en parallèle qui
    reçoivent UNREGISTERED sur le même token), on ne fait rien sans
    lever d'exception.
    """
    result = await db.execute(select(DeviceToken).where(DeviceToken.token == token))
    device = result.scalar_one_or_none()
    if device is None:
        return
    if device.is_active:
        device.is_active = False
        device.updated_at = datetime.now(UTC)
        await db.flush()
        await db.commit()
        log.info(
            "auth.device_token.invalidated_by_fcm",
            user_id=str(device.user_id),
            token_preview=token[:8] + "…",
        )
