"""
Seed de développement — peuple la DB avec des comptes de test.

Lancement (depuis la racine `nexya_backend/`, venv activé) :
    python -m scripts.seed_dev

Crée (ou met à jour s'ils existent déjà) :
  - free@nexya.ai     / DemoFree2026!  → plan free
  - pro@nexya.ai      / DemoPro2026!   → plan pro (1 an)

Usage typique :
  - Démos jury / soutenance
  - Tests manuels Swagger
  - Reproduction de bugs sur un compte connu

Refuse de tourner en production — un seed accidentel en prod écraserait
les comptes réels et exposerait des mots de passe connus.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import UTC, datetime, timedelta

import bcrypt
import structlog
from sqlalchemy import select

from app.config import settings
from app.core.database.postgres import AsyncSessionLocal, dispose_engine
from app.core.observability import configure_logging
from app.features.auth.models import User

log = structlog.get_logger()


# ── Comptes de démo — mots de passe volontairement publiables ──────
# Ils respectent la politique du backend : ≥ 12 caractères, majuscule,
# minuscule, chiffre, caractère spécial (voir RegisterRequest).
DEMO_ACCOUNTS: list[dict[str, object]] = [
    {
        "email": "free@nexya.ai",
        "username": "demofree",
        "password": "DemoFree2026!",
        "display_name": "Démo Free",
        "plan": "free",
        "plan_expires_at": None,
        "bio": "Compte de démonstration — plan gratuit.",
    },
    {
        "email": "pro@nexya.ai",
        "username": "demopro",
        "password": "DemoPro2026!",
        "display_name": "Démo Pro",
        "plan": "pro",
        "plan_expires_at": datetime.now(UTC) + timedelta(days=365),
        "bio": "Compte de démonstration — plan Pro (quotas étendus).",
    },
]


def _hash(password: str) -> str:
    """bcrypt avec troncature explicite à 72 bytes (cohérent avec le service Auth)."""
    return bcrypt.hashpw(password.encode("utf-8")[:72], bcrypt.gensalt()).decode("utf-8")


async def _upsert_user(session, account: dict[str, object]) -> str:
    """Crée le compte s'il n'existe pas, met à jour le hash + plan sinon.

    Retourne `created` ou `updated` pour le rapport final.
    """
    stmt = select(User).where(User.email == account["email"])
    existing = (await session.execute(stmt)).scalar_one_or_none()

    if existing is None:
        session.add(
            User(
                email=account["email"],
                username=account["username"],
                password_hash=_hash(account["password"]),  # type: ignore[arg-type]
                display_name=account["display_name"],
                plan=account["plan"],
                plan_expires_at=account["plan_expires_at"],
                bio=account["bio"],
            )
        )
        return "created"

    existing.password_hash = _hash(account["password"])  # type: ignore[arg-type]
    existing.display_name = account["display_name"]  # type: ignore[assignment]
    existing.plan = account["plan"]  # type: ignore[assignment]
    existing.plan_expires_at = account["plan_expires_at"]  # type: ignore[assignment]
    existing.bio = account["bio"]  # type: ignore[assignment]
    existing.is_active = True
    existing.deleted_at = None
    return "updated"


async def main() -> None:
    configure_logging()

    if settings.is_production:
        log.critical("seed.refused.production", env=settings.env)
        print("[seed] REFUSÉ : ENV=production. Le seed est strictement réservé au dev.")
        sys.exit(2)

    log.info("seed.start", env=settings.env, accounts=len(DEMO_ACCOUNTS))

    async with AsyncSessionLocal() as session:
        report: dict[str, list[str]] = {"created": [], "updated": []}
        for account in DEMO_ACCOUNTS:
            action = await _upsert_user(session, account)
            report[action].append(account["email"])  # type: ignore[arg-type]
        await session.commit()

    log.info("seed.done", **{k: len(v) for k, v in report.items()})

    print("\n=== NEXYA seed ===")
    for email in report["created"]:
        print(f"  [+] cree       : {email}")
    for email in report["updated"]:
        print(f"  [~] mis a jour : {email}")
    print(f"\nMots de passe : voir DEMO_ACCOUNTS dans {__file__}")
    print("==================\n")

    await dispose_engine()


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
