"""
Schemas Pydantic Auth — Request/Response pour chaque endpoint.

Conventions NEXYA :
- Suffixe Request pour les corps de requête
- Suffixe Response pour les données retournées dans NexyaResponse[T]
- Validation stricte via Pydantic v2 (min_length, regex, etc.)
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

# ══════════════════════════════════════════════════════════════
# AUTH — Register / Login / Refresh / Logout
# ══════════════════════════════════════════════════════════════


class RegisterRequest(BaseModel):
    """Inscription d'un nouvel utilisateur.

    Le champ `captcha_token` est optionnel côté contrat Pydantic mais
    **obligatoire** côté service dès que `settings.hcaptcha_enabled=True`
    ET `settings.hcaptcha_secret_key` est non vide. On ne l'impose pas
    en Pydantic pour :
    - Laisser les tests d'intégration envoyer des requêtes sans captcha
      quand le Mock verifier est en place (token absent = accepté).
    - Laisser le Flutter web et les app mobiles envoyer `null` sans
      faire échouer la validation globale avant même d'entrer en service.
    Le service lève `CaptchaInvalidException` si le token est manquant
    en prod — erreur unique, pas deux codes distincts pour une seule
    cause (manquant / invalide = même traitement côté user).
    """

    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    username: str | None = Field(default=None, min_length=3, max_length=50)
    display_name: str | None = Field(default=None, max_length=100)
    captcha_token: str | None = Field(default=None, max_length=4096)

    @field_validator("password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        """Vérifie la robustesse du mot de passe.

        Minimum : 8 caractères, 1 majuscule, 1 minuscule, 1 chiffre.
        Pas de regex ultra-complexe — l'objectif est d'éviter les
        mots de passe triviaux, pas de frustrer l'utilisateur.
        """
        if not re.search(r"[A-Z]", v):
            raise ValueError("Le mot de passe doit contenir au moins une majuscule.")
        if not re.search(r"[a-z]", v):
            raise ValueError("Le mot de passe doit contenir au moins une minuscule.")
        if not re.search(r"\d", v):
            raise ValueError("Le mot de passe doit contenir au moins un chiffre.")
        return v

    @field_validator("username")
    @classmethod
    def username_format(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not re.match(r"^[a-zA-Z0-9_]+$", v):
            raise ValueError(
                "Le nom d'utilisateur ne peut contenir que des lettres, chiffres et underscores."
            )
        return v.lower()

    @field_validator("display_name")
    @classmethod
    def display_name_clean(cls, v: str | None) -> str | None:
        """Nettoyage défensif du display_name (null bytes, bidi, NFC).

        On ne stripe pas le display_name à None ici — c'est la
        responsabilité du service, qui peut fallback sur username ou
        la partie locale de l'email. On normalise juste les bytes
        dangereux qui pourraient casser la DB ou tromper un parser
        d'export (CSV, logs).
        """
        if v is None:
            return None
        # Import local pour éviter un cycle (schemas ne doit pas
        # dépendre de core/security en module-level).
        from app.core.security.sanitizer import clean_text

        cleaned = clean_text(v, max_length=100, collapse_whitespace=True)
        return cleaned if cleaned else None


class LoginRequest(BaseModel):
    """Connexion d'un utilisateur existant."""

    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    """Renouvellement du couple access + refresh token."""

    refresh_token: str


class TokenResponse(BaseModel):
    """Réponse après login/register/refresh — contient la paire de tokens."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int  # secondes avant expiration de l'access token


# ══════════════════════════════════════════════════════════════
# USER — Profile
# ══════════════════════════════════════════════════════════════


class UserProfile(BaseModel):
    """Profil utilisateur retourné par GET /user/profile."""

    id: uuid.UUID
    email: str
    username: str | None
    display_name: str | None
    avatar_url: str | None
    bio: str | None
    locale: str
    timezone: str
    plan: str
    plan_expires_at: datetime | None
    voice_id: uuid.UUID | None
    data_collection_enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class UpdateProfileRequest(BaseModel):
    """Mise à jour du profil (PUT /user/profile).

    Tous les champs sont optionnels — seuls les champs envoyés sont mis à jour.
    """

    display_name: str | None = Field(default=None, max_length=100)
    username: str | None = Field(default=None, min_length=3, max_length=50)
    avatar_url: str | None = Field(default=None, max_length=500)
    bio: str | None = Field(default=None, max_length=1000)
    locale: str | None = Field(default=None, max_length=10)
    timezone: str | None = Field(default=None, max_length=50)
    voice_id: uuid.UUID | None = None
    data_collection_enabled: bool | None = None

    @field_validator("username")
    @classmethod
    def username_format(cls, v: str | None) -> str | None:
        if v is None:
            return None
        if not re.match(r"^[a-zA-Z0-9_]+$", v):
            raise ValueError(
                "Le nom d'utilisateur ne peut contenir que des lettres, chiffres et underscores."
            )
        return v.lower()

    @field_validator("display_name")
    @classmethod
    def display_name_clean(cls, v: str | None) -> str | None:
        if v is None:
            return None
        from app.core.security.sanitizer import clean_text

        cleaned = clean_text(v, max_length=100, collapse_whitespace=True)
        return cleaned if cleaned else None

    @field_validator("bio")
    @classmethod
    def bio_clean(cls, v: str | None) -> str | None:
        """Nettoyage de la bio — preserve les retours à la ligne (bio
        multiligne autorisée), strip uniquement control / bidi / NFC."""
        if v is None:
            return None
        from app.core.security.sanitizer import clean_text

        cleaned = clean_text(v, max_length=1000, collapse_whitespace=False)
        return cleaned if cleaned else None


class ChangePasswordRequest(BaseModel):
    """Changement de mot de passe (PUT /user/password)."""

    current_password: str
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not re.search(r"[A-Z]", v):
            raise ValueError("Le mot de passe doit contenir au moins une majuscule.")
        if not re.search(r"[a-z]", v):
            raise ValueError("Le mot de passe doit contenir au moins une minuscule.")
        if not re.search(r"\d", v):
            raise ValueError("Le mot de passe doit contenir au moins un chiffre.")
        return v


class ForgotPasswordRequest(BaseModel):
    """Demande de reset mot de passe (POST /auth/forgot-password).

    Anti-enumeration : la réponse est toujours 200 avec un message
    générique, qu'un compte existe ou non pour cet email.
    """

    email: EmailStr


class ForgotPasswordResponse(BaseModel):
    """Réponse générique — ne laisse jamais fuiter l'existence du compte."""

    message: str = "Si un compte correspond à cet email, un lien de réinitialisation a été envoyé."


class ResetPasswordRequest(BaseModel):
    """Reset effectif du mot de passe (POST /auth/reset-password).

    Le `token` est celui du lien reçu par email (JWT RS256 TTL 15 min).
    Le `new_password` suit la même politique que RegisterRequest.
    """

    token: str = Field(min_length=10, max_length=2048)
    new_password: str = Field(min_length=8, max_length=128)

    @field_validator("new_password")
    @classmethod
    def password_strength(cls, v: str) -> str:
        if not re.search(r"[A-Z]", v):
            raise ValueError("Le mot de passe doit contenir au moins une majuscule.")
        if not re.search(r"[a-z]", v):
            raise ValueError("Le mot de passe doit contenir au moins une minuscule.")
        if not re.search(r"\d", v):
            raise ValueError("Le mot de passe doit contenir au moins un chiffre.")
        return v


class ResetPasswordResponse(BaseModel):
    """Confirmation post-reset — le client peut rediriger vers /login."""

    message: str = "Mot de passe mis à jour. Vous pouvez vous reconnecter."


# ══════════════════════════════════════════════════════════════
# DEVICE TOKEN — FCM (notifications push)
# ══════════════════════════════════════════════════════════════


class DeviceTokenRequest(BaseModel):
    """Enregistrement d'un token FCM pour les notifications push."""

    token: str = Field(min_length=10, max_length=500)
    platform: str = Field(pattern=r"^(android|ios)$")
