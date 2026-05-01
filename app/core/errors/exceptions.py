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


class ResetTokenInvalidException(NexYaException):
    """Token de reset mot de passe invalide — signature ko, purpose ko,
    ou fingerprint du hash ne correspond plus (mot de passe déjà changé).
    Message générique pour ne pas laisser fuir la cause précise.
    """

    def __init__(self) -> None:
        super().__init__(
            code="RESET_TOKEN_INVALID",
            message="Ce lien de réinitialisation est invalide.",
            status_code=400,
        )


class ResetTokenExpiredException(NexYaException):
    """Token de reset expiré (> 15 min depuis l'emission)."""

    def __init__(self) -> None:
        super().__init__(
            code="RESET_TOKEN_EXPIRED",
            message="Ce lien de réinitialisation a expiré. Demandez-en un nouveau.",
            status_code=400,
        )


class UnsubscribeTokenInvalidException(NexYaException):
    """Token d'unsubscribe email invalide — signature KO, purpose KO,
    payload malformé, ou catégorie hors whitelist.

    Message neutre pour ne pas distinguer un token forgé d'un token
    simplement périmé — évite les attaques d'énumération.
    """

    def __init__(self) -> None:
        super().__init__(
            code="UNSUBSCRIBE_TOKEN_INVALID",
            message="Ce lien de désinscription est invalide.",
            status_code=400,
        )


class UnsubscribeTokenExpiredException(NexYaException):
    """Token d'unsubscribe expiré (> `notification_unsubscribe_token_ttl_days`,
    défaut 365 jours).

    Cas rare en pratique (TTL très long) mais couvert pour les liens
    d'emails archivés depuis plusieurs années.
    """

    def __init__(self) -> None:
        super().__init__(
            code="UNSUBSCRIBE_TOKEN_EXPIRED",
            message=(
                "Ce lien de désinscription a expiré. "
                "Ouvrez l'application NEXYA pour gérer vos préférences."
            ),
            status_code=400,
        )


class UnsubscribeSecurityRefusedException(NexYaException):
    """Tentative de désinscrire la catégorie `security`.

    La catégorie `security` (login inhabituel, changement de mot de
    passe, suppression d'appareil) N'EST PAS désinscriptible par
    obligation légale. Retour 400 avec message explicite.
    """

    def __init__(self) -> None:
        super().__init__(
            code="UNSUBSCRIBE_SECURITY_REFUSED",
            message=(
                "La catégorie « Sécurité » ne peut pas être désinscrite. "
                "Ces alertes protègent votre compte."
            ),
            status_code=400,
        )


class CaptchaInvalidException(NexYaException):
    """Vérification captcha échouée à l'inscription.

    HTTP 400 (et non 429) — la requête est bien formée, mais le défi
    anti-bot n'a pas été validé côté hCaptcha. Le Flutter recharge le
    widget pour proposer un nouveau challenge à l'utilisateur.
    """

    def __init__(self) -> None:
        super().__init__(
            code="CAPTCHA_INVALID",
            message="Vérification anti-robot échouée. Réessayez.",
            status_code=400,
        )


class DeviceQuotaExceededException(NexYaException):
    """Un appareil a atteint sa limite journalière d'inscriptions.

    HTTP 429 : même si l'UX Flutter doit idéalement ne jamais afficher
    cette erreur (le quota est pensé pour dépasser largement le besoin
    légitime), on la code pour qu'un bot qui contourne le captcha tombe
    sur un mur net — et que le Flutter puisse afficher « Trop
    d'inscriptions depuis cet appareil. Réessayez demain. ».
    """

    def __init__(self, retry_after: int = 86400) -> None:
        super().__init__(
            code="DEVICE_QUOTA_EXCEEDED",
            message="Trop d'inscriptions depuis cet appareil. Réessayez plus tard.",
            status_code=429,
            data={"retry_after": retry_after},
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
            message=f"Type de fichier non autorisé : {mime_type}."
            if mime_type
            else "Type de fichier non autorisé.",
            status_code=415,
        )


class FileContentMismatchException(NexYaException):
    """Le MIME annoncé par le client ne correspond pas au MIME détecté par
    magic-bytes — anti-smuggling.

    HTTP 415 : le serveur refuse le type détecté. Le message reste neutre
    (« le contenu ne correspond pas au type déclaré ») pour ne pas aider
    un attaquant à comprendre quelle détection a échoué.
    """

    def __init__(self, *, announced: str = "", detected: str = "") -> None:
        super().__init__(
            code="FILE_CONTENT_MISMATCH",
            message="Le contenu du fichier ne correspond pas au type déclaré.",
            status_code=415,
            data={"announced": announced, "detected": detected} if announced or detected else None,
        )


class MemoryQuotaExceededException(NexYaException):
    """Plafond de mémoires (faits durables IA) atteint pour le plan courant.

    Status 402 — alignement avec `ProjectQuotaExceededException` (C2),
    `LibraryQuotaExceededException` (C3), `ProjectFilesQuotaExceededException`.
    `data` expose la jauge pour l'UI Flutter :
    « 100 mémoires sur 100 — Pro pour en ajouter davantage. »
    """

    def __init__(self, *, current: int, maximum: int, plan: str) -> None:
        super().__init__(
            code="MEMORY_QUOTA_EXCEEDED",
            message=(
                f"Vous avez atteint la limite de {maximum} mémoires du plan {plan}. "
                "Passez à Pro pour en sauvegarder davantage."
            ),
            status_code=402,
            data={"current": current, "max": maximum, "plan": plan},
        )


class EmbeddingsUnavailableException(NexYaException):
    """Le fournisseur d'embeddings (OpenAI API ou modèle local) est
    injoignable — impossible de générer un vecteur pour la mémoire.

    Status 503 — même pattern que `ObjectStoreUnavailableException` (C3).
    L'utilisateur voit « Service temporairement indisponible, réessayez ».
    `data` porte le nom du provider + la raison pour l'audit backend.
    """

    def __init__(self, *, provider: str = "", reason: str = "") -> None:
        super().__init__(
            code="EMBEDDINGS_UNAVAILABLE",
            message="Service d'indexation mémoire temporairement indisponible.",
            status_code=503,
            data={"provider": provider, "reason": reason} if (provider or reason) else None,
        )


class VirusDetectedException(NexYaException):
    """Un scanner antivirus a détecté une signature malveillante pendant
    l'upload d'un fichier.

    HTTP 415 : on refuse le type de contenu détecté (malveillant). Message
    utilisateur neutre (« Ce fichier n'a pas pu être accepté ») pour ne pas
    stigmatiser en cas de faux positif (certains scanners alertent sur des
    PDFs éducatifs légitimes qui citent un malware, par exemple).
    Le champ `data` porte la signature + le nom du scanner pour audit côté
    log/admin, mais n'expose pas le détail au user.
    """

    def __init__(self, *, signature: str = "", scanner: str = "") -> None:
        super().__init__(
            code="VIRUS_DETECTED",
            message="Ce fichier n'a pas pu être accepté.",
            status_code=415,
            data={"signature": signature, "scanner": scanner} if (signature or scanner) else None,
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


# ══════════════════════════════════════════════════════════════
# PROJECTS — 402 quota / 409 conflit
# ══════════════════════════════════════════════════════════════


class ProjectQuotaExceededException(NexYaException):
    """Plafond de projets actifs atteint pour le plan courant (Free/Pro).

    Status 402 pour inciter à l'upgrade côté Flutter (pattern aligné avec
    `LlmQuotaExceededException` et `PlanRequiredException` 403 pour les
    paywalls permanents). `data` expose la jauge pour que l'UI affiche
    « 3 projets sur 3 — passez à Pro pour en créer davantage ».
    """

    def __init__(self, *, current: int, maximum: int, plan: str) -> None:
        super().__init__(
            code="PROJECT_QUOTA_EXCEEDED",
            message=(
                f"Vous avez atteint la limite de {maximum} projets du plan {plan}. "
                "Passez à Pro pour en créer davantage."
            ),
            status_code=402,
            data={"current": current, "max": maximum, "plan": plan},
        )


class ProjectFilesQuotaExceededException(NexYaException):
    """Plafond de fichiers/projet atteint pour le plan courant."""

    def __init__(self, *, current: int, maximum: int, plan: str) -> None:
        super().__init__(
            code="PROJECT_FILES_QUOTA_EXCEEDED",
            message=(
                f"Ce projet contient déjà {maximum} fichiers — plafond du plan "
                f"{plan}. Supprimez-en un ou passez à Pro pour en ajouter davantage."
            ),
            status_code=402,
            data={"current": current, "max": maximum, "plan": plan},
        )


class ProjectNameConflictException(NexYaException):
    """Un projet actif portant le même nom (case-insensitive) existe déjà
    pour cet utilisateur — la contrainte UNIQUE partielle
    `(user_id, LOWER(name)) WHERE deleted_at IS NULL` a sauté.

    HTTP 409 : même pattern que `AuthEmailAlreadyExists` / `DuplicateReport`.
    Le Flutter affiche un toast « Vous avez déjà un projet de ce nom » et
    remet le champ en focus.
    """

    def __init__(self) -> None:
        super().__init__(
            code="PROJECT_NAME_CONFLICT",
            message="Vous avez déjà un projet de ce nom.",
            status_code=409,
        )


# ══════════════════════════════════════════════════════════════
# LIBRARY — 402 quota
# ══════════════════════════════════════════════════════════════


class LibraryQuotaExceededException(NexYaException):
    """Plafond d'items actifs dans la bibliothèque atteint pour le plan.

    Status 402 — alignement avec `ProjectQuotaExceededException` et les
    autres quota-gates côté paywall. `data` expose la jauge pour l'UI :
    « 50 médias sur 50 — passez à Pro pour en sauvegarder davantage ».
    """

    def __init__(self, *, current: int, maximum: int, plan: str) -> None:
        super().__init__(
            code="LIBRARY_QUOTA_EXCEEDED",
            message=(
                f"Vous avez atteint la limite de {maximum} médias du plan "
                f"{plan}. Passez à Pro pour en sauvegarder davantage."
            ),
            status_code=402,
            data={"current": current, "max": maximum, "plan": plan},
        )


class TasksQuotaExceededException(NexYaException):
    """Plafond de tâches planifiées actives atteint (F1 Planner).

    Status 402. Pattern aligné Project/Library/Memory/Documents/Vision.
    """

    def __init__(self, *, current: int, maximum: int, plan: str) -> None:
        super().__init__(
            code="TASKS_QUOTA_EXCEEDED",
            message=(f"Vous avez atteint la limite de {maximum} tâches planifiées du plan {plan}."),
            status_code=402,
            data={"current": current, "max": maximum, "plan": plan},
        )


class TaskScheduleInvalidException(NexYaException):
    """Schedule demandé invalide (date passée, interval < min, etc.) — F1."""

    def __init__(self, detail: str = "") -> None:
        message = "Schedule invalide."
        if detail:
            message = f"{message} {detail}"
        super().__init__(
            code="TASK_SCHEDULE_INVALID",
            message=message,
            status_code=422,
        )


class VisionQuotaExceededException(NexYaException):
    """Plafond quotidien d'analyses image vision atteint.

    Status 402. Free=3 img/jour (TODO Ivan), Pro=50 img/jour (TODO Ivan).
    `data` expose la jauge pour l'UI Flutter.

    Session E2.
    """

    def __init__(self, *, current: int, maximum: int, plan: str) -> None:
        super().__init__(
            code="VISION_QUOTA_EXCEEDED",
            message=(
                f"Vous avez atteint la limite de {maximum} analyses "
                f"d'image du plan {plan} pour aujourd'hui."
            ),
            status_code=402,
            data={"current": current, "max": maximum, "plan": plan},
        )


class VisionContentFilteredException(NexYaException):
    """Image ou prompt bloqué par la safety policy du provider.

    Status 400. Message user neutre — on ne dit pas ce qui a été bloqué
    (éviter de fournir des indices à un attaquant essayant de contourner).

    Session E2.
    """

    def __init__(self, *, provider: str = "unknown") -> None:
        super().__init__(
            code="VISION_CONTENT_FILTERED",
            message=(
                "Ce contenu n'a pas pu être analysé. Reformulez votre "
                "demande ou essayez avec une autre image."
            ),
            status_code=400,
            data={"provider": provider},
        )


class VisionUnavailableException(NexYaException):
    """Provider vision (Gemini / GPT-4o / Claude) injoignable.

    Status 503. Message user neutre — on ne révèle pas quel provider
    est utilisé (portabilité = détail interne).

    Session E2.
    """

    def __init__(self, *, provider: str = "unknown", reason: str = "") -> None:
        message = "Service d'analyse image temporairement indisponible."
        if reason:
            message = f"{message} ({reason})"
        super().__init__(
            code="VISION_UNAVAILABLE",
            message=message,
            status_code=503,
            data={"provider": provider, "reason": reason},
        )


class ImageTooLargeException(NexYaException):
    """Image dépasse la taille max acceptée pour analyse vision.

    Status 413. Séparée de `FileTooLargeException` pour distinguer
    côté Flutter (« votre image est trop grande pour l'analyse » vs
    « votre fichier est trop gros pour l'upload »).

    Session E2.
    """

    def __init__(self, *, size_bytes: int, max_bytes: int) -> None:
        super().__init__(
            code="IMAGE_TOO_LARGE",
            message=(
                f"Image trop grande ({size_bytes // (1024 * 1024)} MB). "
                f"Maximum : {max_bytes // (1024 * 1024)} MB."
            ),
            status_code=413,
            data={"size_bytes": size_bytes, "max_bytes": max_bytes},
        )


class VoiceQuotaExceededException(NexYaException):
    """Plafond quotidien de minutes de transcription voice atteint.

    Status 402 — alignement pattern Project/Library/Memory/Documents.
    Plan Pro uniquement (Free fait du STT natif Flutter → ne passe
    jamais par ce code path). `data` expose la jauge pour l'UI Flutter :
    « 120 min sur 120 aujourd'hui — revenez demain ou contactez support ».

    Session E1.
    """

    def __init__(self, *, current: int, maximum: int, plan: str) -> None:
        super().__init__(
            code="VOICE_QUOTA_EXCEEDED",
            message=(
                f"Vous avez atteint la limite de {maximum} minutes voice "
                f"du plan {plan} pour aujourd'hui. Réessayez demain."
            ),
            status_code=402,
            data={"current": current, "max": maximum, "plan": plan},
        )


class TTSQuotaExceededException(NexYaException):
    """Plafond quotidien de caractères TTS atteint.

    Status 402. Plan Pro uniquement. Unité = caractères (pas minutes),
    parce que la facturation TTS OpenAI est per-character.

    Session E1.
    """

    def __init__(self, *, current: int, maximum: int, plan: str) -> None:
        super().__init__(
            code="TTS_QUOTA_EXCEEDED",
            message=(
                f"Vous avez atteint la limite de {maximum} caractères de "
                f"synthèse vocale du plan {plan} pour aujourd'hui."
            ),
            status_code=402,
            data={"current": current, "max": maximum, "plan": plan},
        )


class AudioTooLongException(NexYaException):
    """Audio dépasse la durée max acceptée (10 min par défaut).

    Status 413 — aligné `FileTooLargeException`. Le chunking + reassembly
    d'audio > 10 min est reporté en Phase 12 — pour l'instant on refuse
    proprement avec message actionnable côté UI.

    Session E1.
    """

    def __init__(self, *, duration_seconds: float, max_seconds: int) -> None:
        super().__init__(
            code="AUDIO_TOO_LONG",
            message=(
                f"Audio trop long ({duration_seconds:.0f} s). "
                f"Maximum : {max_seconds} s. Découpez votre enregistrement."
            ),
            status_code=413,
            data={
                "duration_seconds": duration_seconds,
                "max_seconds": max_seconds,
            },
        )


class VoiceUnavailableException(NexYaException):
    """Provider voice (Whisper / faster-whisper / Deepgram) injoignable.

    Status 503 — aligné `EmbeddingsUnavailableException`. Le message
    user reste neutre pour ne pas révéler le provider utilisé.

    Session E1.
    """

    def __init__(self, *, provider: str = "unknown", reason: str = "") -> None:
        message = "Service de transcription vocale temporairement indisponible."
        if reason:
            message = f"{message} ({reason})"
        super().__init__(
            code="VOICE_UNAVAILABLE",
            message=message,
            status_code=503,
            data={"provider": provider, "reason": reason},
        )


class DocumentsQuotaExceededException(NexYaException):
    """Plafond de documents RAG actifs atteint pour le plan courant.

    Status 402 — alignement avec les autres quota-gates (Project, Library,
    Memory, ProjectFiles). `data` expose la jauge pour l'UI :
    « 3 documents sur 3 — passez à Pro pour en ajouter davantage ».

    Session D4 — le quota est vérifié côté `FileUploadService.upload`
    AVANT d'uploader les bytes, pour ne pas payer l'IO MinIO sur un
    fichier qu'on va refuser.
    """

    def __init__(self, *, current: int, maximum: int, plan: str) -> None:
        super().__init__(
            code="DOCUMENTS_QUOTA_EXCEEDED",
            message=(
                f"Vous avez atteint la limite de {maximum} documents du plan "
                f"{plan}. Passez à Pro pour en ajouter davantage."
            ),
            status_code=402,
            data={"current": current, "max": maximum, "plan": plan},
        )
