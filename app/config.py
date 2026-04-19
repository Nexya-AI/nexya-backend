"""
NEXYA Backend — Configuration centralisée.

Toutes les variables d'environnement sont lues, typées et validées ici.
Si une variable obligatoire manque, l'API refuse de démarrer avec un message explicite.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Settings chargés depuis .env — validés au démarrage par Pydantic."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",          # ignore les variables inconnues dans .env
        case_sensitive=False,
    )

    # ── App ────────────────────────────────────────────────────
    env: str = "development"
    app_secret: str = "change-me"
    debug: bool = True

    # ── Database ───────────────────────────────────────────────
    database_url: str = "postgresql+asyncpg://nexya:nexya_dev@localhost:5432/nexya"

    # ── Redis ──────────────────────────────────────────────────
    redis_url: str = "redis://localhost:6379/0"

    # ── JWT RS256 ──────────────────────────────────────────────
    # Peut être soit le contenu PEM brut, soit un chemin vers un fichier .pem.
    # Le validator ci-dessous détecte automatiquement le format.
    jwt_private_key: str = ""
    jwt_public_key: str = ""
    jwt_access_ttl_minutes: int = 15
    jwt_refresh_ttl_days: int = 30

    @field_validator("jwt_private_key", "jwt_public_key", mode="after")
    @classmethod
    def load_key_from_file_if_path(cls, v: str) -> str:
        """Si la valeur est un chemin vers un .pem existant, charge son contenu.

        Permet deux usages :
        - Dev : JWT_PRIVATE_KEY=private.pem (chemin relatif au backend)
        - Prod : JWT_PRIVATE_KEY=<contenu PEM brut> (variable d'env multi-ligne)
        """
        if not v:
            return v
        # Si ça ressemble à une clé PEM (commence par -----BEGIN), on renvoie tel quel
        if v.startswith("-----BEGIN"):
            return v
        # Sinon on tente de lire le fichier
        path = Path(v)
        if path.is_file():
            return path.read_text(encoding="utf-8")
        return v

    # ── IA — Gemini (Vertex AI) ────────────────────────────────
    gemini_api_key: str = ""
    gcp_project_id: str = "nexya-ai"
    gcp_location: str = "us-central1"

    # ── IA — OpenAI ────────────────────────────────────────────
    openai_api_key: str = ""

    # ── IA — Qwen ──────────────────────────────────────────────
    qwen_api_key: str = ""

    # ── Storage (MinIO / S3) ───────────────────────────────────
    s3_endpoint: str = "http://localhost:9000"
    s3_access_key: str = ""
    s3_secret_key: str = ""
    s3_bucket_name: str = "nexya-media"

    # ── Paiements ──────────────────────────────────────────────
    cinetpay_api_key: str = ""
    cinetpay_site_id: str = ""
    notchpay_public_key: str = ""
    notchpay_secret_key: str = ""
    stripe_secret_key: str = ""
    stripe_webhook_secret: str = ""

    # ── Notifications ──────────────────────────────────────────
    fcm_server_key: str = ""

    # ── CORS ───────────────────────────────────────────────────
    allowed_origins: str = "*"

    # ── Database pool ──────────────────────────────────────────
    db_pool_size: int = 20
    db_max_overflow: int = 10
    db_echo: bool = False

    # ── Redis pool ─────────────────────────────────────────────
    redis_max_connections: int = 50

    # ── Timeouts (secondes) — Africa-first ─────────────────────
    llm_timeout: int = 30
    stream_timeout: int = 120
    upload_timeout: int = 60

    # ── Pagination ─────────────────────────────────────────────
    pagination_max_limit: int = Field(default=50, ge=1)
    pagination_default_limit: int = Field(default=20, ge=1)

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def is_development(self) -> bool:
        return self.env == "development"

    @property
    def cors_origins(self) -> list[str]:
        """Parse la liste d'origines CORS depuis la string comma-separated."""
        if self.allowed_origins == "*":
            return ["*"]
        return [origin.strip() for origin in self.allowed_origins.split(",") if origin.strip()]

    # ── Garde-fous de production ──────────────────────────────
    # Si `ENV=production`, on refuse de démarrer l'API avec des valeurs de dev.
    # Mieux vaut un crash explicite au boot qu'une fuite silencieuse en prod.
    @model_validator(mode="after")
    def _enforce_production_safety(self) -> Settings:
        if not self.is_production:
            return self

        problems: list[str] = []

        # CORS — "*" + allow_credentials=True est un trou béant (CSRF + token theft)
        if self.allowed_origins.strip() == "*":
            problems.append("ALLOWED_ORIGINS=* est interdit en production")

        # Secret d'app — un défaut identifiable = secret cassé
        insecure_secrets = {"", "change-me", "dev-local-secret-change-me-in-production-please"}
        if self.app_secret in insecure_secrets or self.app_secret.startswith("dev-"):
            problems.append("APP_SECRET doit être une valeur aléatoire forte en production")

        # Clés JWT — impossibles à signer sans elles
        if not self.jwt_private_key or not self.jwt_public_key:
            problems.append("JWT_PRIVATE_KEY et JWT_PUBLIC_KEY sont obligatoires en production")

        # Debug — expose les stacks et les détails internes
        if self.debug:
            problems.append("DEBUG=true est interdit en production")

        # Echo SQL — imprime les requêtes (et parfois les paramètres) sur stdout
        if self.db_echo:
            problems.append("DB_ECHO=true est interdit en production")

        if problems:
            joined = "\n  - ".join(problems)
            raise ValueError(
                "Configuration production invalide :\n  - " + joined
            )
        return self


# Singleton — importé partout via `from app.config import settings`
settings = Settings()
