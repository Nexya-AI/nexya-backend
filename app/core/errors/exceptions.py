"""
Catalogue d'exceptions NEXYA — chaque erreur a un code, un message et un status HTTP.

Le Flutter lit le champ `code` pour afficher le bon message à l'utilisateur.
Les codes sont documentés dans CLAUDE.md section 10.
"""

from __future__ import annotations

from datetime import datetime


class NexYaException(Exception):
    """Exception de base NEXYA.

    Toutes les erreurs métier héritent de cette classe.
    Le handler global dans handlers.py la transforme en NexyaResponse.
    """

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 400,
        data: dict | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = data
        super().__init__(message)


# ══════════════════════════════════════════════════════════════
# AUTH — 401 / 409
# ══════════════════════════════════════════════════════════════

class AuthTokenExpiredException(NexYaException):
    def __init__(self) -> None:
        super().__init__(
            code="AUTH_TOKEN_EXPIRED",
            message="Session expirée. Veuillez vous reconnecter.",
            status_code=401,
        )


class AuthTokenInvalidException(NexYaException):
    def __init__(self) -> None:
        super().__init__(
            code="AUTH_TOKEN_INVALID",
            message="Token invalide.",
            status_code=401,
        )


class AuthRefreshExpiredException(NexYaException):
    def __init__(self) -> None:
        super().__init__(
            code="AUTH_REFRESH_EXPIRED",
            message="Session expirée. Veuillez vous reconnecter.",
            status_code=401,
        )


class AuthCredentialsInvalidException(NexYaException):
    def __init__(self) -> None:
        super().__init__(
            code="AUTH_CREDENTIALS_INVALID",
            message="Email ou mot de passe incorrect.",
            status_code=401,
        )


class AuthEmailAlreadyExistsException(NexYaException):
    def __init__(self) -> None:
        super().__init__(
            code="AUTH_EMAIL_ALREADY_EXISTS",
            message="Cet email est déjà utilisé.",
            status_code=409,
        )


class AuthUsernameAlreadyExistsException(NexYaException):
    def __init__(self) -> None:
        super().__init__(
            code="AUTH_USERNAME_ALREADY_EXISTS",
            message="Ce nom d'utilisateur est déjà pris.",
            status_code=409,
        )


# ══════════════════════════════════════════════════════════════
# RATE LIMITING — 429
# ══════════════════════════════════════════════════════════════

class RateLimitExceededException(NexYaException):
    def __init__(self, reset_at: datetime | None = None) -> None:
        data = {"reset_at": reset_at.isoformat()} if reset_at else None
        super().__init__(
            code="RATE_LIMIT_EXCEEDED",
            message="Quota journalier atteint.",
            status_code=429,
            data=data,
        )


class RateLimitIPException(NexYaException):
    def __init__(self, retry_after: int = 60) -> None:
        super().__init__(
            code="RATE_LIMIT_IP",
            message="Trop de tentatives. Veuillez patienter.",
            status_code=429,
            data={"retry_after": retry_after},
        )


class RateLimitAbuseException(NexYaException):
    """Quota user-scoped dépassé sur les signalements d'abus.

    Code distinct de `RATE_LIMIT_IP` : un signalement est toujours authentifié
    et lié à un user. Le frontend doit distinguer « l'IP est pénalisée »
    (brute-force auth) de « l'utilisateur signale trop » (anti-spam du
    mécanisme de modération lui-même).
    """

    def __init__(self, retry_after: int = 3600) -> None:
        super().__init__(
            code="RATE_LIMIT_ABUSE",
            message="Trop de signalements récents. Réessayez plus tard.",
            status_code=429,
            data={"retry_after": retry_after},
        )


# ══════════════════════════════════════════════════════════════
# LLM / IA — 402 / 503
# ══════════════════════════════════════════════════════════════

class LlmUnavailableException(NexYaException):
    def __init__(self) -> None:
        super().__init__(
            code="LLM_UNAVAILABLE",
            message="Le service IA est temporairement indisponible. Réessayez dans quelques instants.",
            status_code=503,
        )


class LlmQuotaExceededException(NexYaException):
    def __init__(self) -> None:
        super().__init__(
            code="LLM_QUOTA_EXCEEDED",
            message="Quota de tokens atteint.",
            status_code=402,
        )


# ══════════════════════════════════════════════════════════════
# PLAN / PERMISSION — 403
# ══════════════════════════════════════════════════════════════

class PlanRequiredException(NexYaException):
    def __init__(self, feature: str = "") -> None:
        msg = "Cette fonctionnalité nécessite un abonnement Pro."
        if feature:
            msg = f"{feature} nécessite un abonnement Pro."
        super().__init__(
            code="PLAN_REQUIRED",
            message=msg,
            status_code=403,
        )


class PermissionDeniedException(NexYaException):
    def __init__(self) -> None:
        super().__init__(
            code="PERMISSION_DENIED",
            message="Vous n'avez pas accès à cette ressource.",
            status_code=403,
        )


# ══════════════════════════════════════════════════════════════
# RESSOURCES — 404
# ══════════════════════════════════════════════════════════════

class ResourceNotFoundException(NexYaException):
    def __init__(self, resource: str = "Ressource") -> None:
        super().__init__(
            code="RESOURCE_NOT_FOUND",
            message=f"{resource} introuvable.",
            status_code=404,
        )


class DuplicateReportException(NexYaException):
    """Un user a déjà signalé ce message (UNIQUE `(user_id, message_id)` violé).

    Retour HTTP 409 : le Flutter affiche un toast « déjà signalé » et garde
    l'UI cohérente (pas d'erreur rouge alarmante pour un double-tap utilisateur).
    """

    def __init__(self) -> None:
        super().__init__(
            code="DUPLICATE_REPORT",
            message="Vous avez déjà signalé ce message.",
            status_code=409,
        )


# ══════════════════════════════════════════════════════════════
# VALIDATION MÉTIER — 422
# ══════════════════════════════════════════════════════════════

class ValidationException(NexYaException):
    """Erreur de validation métier levée par un service.

    Distincte de l'erreur Pydantic globale (qui utilise aussi le code
    VALIDATION_ERROR mais est levée AVANT le service, au niveau du parseur
    de requête). Utiliser cette exception pour des invariants métier
    rejetés en cours de traitement — ex. curseur de pagination malformé,
    combinaison de champs incohérente.
    """

    def __init__(self, message: str = "Données invalides.") -> None:
        super().__init__(
            code="VALIDATION_ERROR",
            message=message,
            status_code=422,
        )


# ══════════════════════════════════════════════════════════════
# FICHIERS — 413 / 415
# ══════════════════════════════════════════════════════════════

class FileTooLargeException(NexYaException):
    def __init__(self, max_mb: int = 20) -> None:
        super().__init__(
            code="FILE_TOO_LARGE",
            message=f"Le fichier dépasse la taille maximale de {max_mb} Mo.",
            status_code=413,
        )


class FileTypeNotAllowedException(NexYaException):
    def __init__(self, mime_type: str = "") -> None:
        super().__init__(
            code="FILE_TYPE_NOT_ALLOWED",
            message=f"Type de fichier non autorisé : {mime_type}." if mime_type else "Type de fichier non autorisé.",
            status_code=415,
        )


# ══════════════════════════════════════════════════════════════
# PAIEMENTS — 400 / 402
# ══════════════════════════════════════════════════════════════

class PaymentFailedException(NexYaException):
    def __init__(self, detail: str = "") -> None:
        msg = "Le paiement a échoué."
        if detail:
            msg = f"Le paiement a échoué : {detail}"
        super().__init__(
            code="PAYMENT_FAILED",
            message=msg,
            status_code=402,
        )


class PaymentWebhookInvalidException(NexYaException):
    def __init__(self) -> None:
        super().__init__(
            code="PAYMENT_WEBHOOK_INVALID",
            message="Signature webhook invalide.",
            status_code=400,
        )
