"""Script ad-hoc pour créer un user de test en prod via docker exec.

Usage (sur le VPS, après scp) :
    docker exec -i nexya-backend python /tmp/create_test_user.py

Crée un user `test-chat-bug@nexyalabs.com` (idempotent — si existe, juste
récupère l'ID) et affiche un JWT valide 15 min pour tester /chat/stream.

Utilisé une seule fois pour valider le fix v1.0.5 du bug v1.0.4 chat hang.
À supprimer après validation.
"""

from __future__ import annotations

import asyncio
import sys
import uuid

sys.path.insert(0, "/app")

import bcrypt  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app.core.auth.jwt import create_access_token  # noqa: E402
from app.core.database.postgres import AsyncSessionLocal  # noqa: E402
from app.features.auth.models import User  # noqa: E402

TEST_EMAIL = "test-chat-bug@nexyalabs.com"
TEST_PASSWORD = "TestChatBug2026!Senior"
TEST_USERNAME = "testchatbug"
TEST_DISPLAY_NAME = "Test ChatBug"


def _hash_password(password: str) -> str:
    """Réplique exacte de app.features.auth.service._hash_password (bcrypt 72-byte truncation)."""
    password_bytes = password.encode("utf-8")[:72]
    return bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode("utf-8")


async def main() -> None:
    async with AsyncSessionLocal() as db:
        result = await db.execute(select(User).where(User.email == TEST_EMAIL))
        user = result.scalar_one_or_none()

        if user:
            print(f"USER_EXISTS: id={user.id} email={user.email}", flush=True)
        else:
            user = User(
                id=uuid.uuid4(),
                email=TEST_EMAIL,
                username=TEST_USERNAME,
                display_name=TEST_DISPLAY_NAME,
                password_hash=_hash_password(TEST_PASSWORD),
                plan="free",
                is_active=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
            print(f"USER_CREATED: id={user.id} email={user.email}", flush=True)

        token = create_access_token(user_id=user.id, plan=user.plan)
        print(f"JWT: {token}", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
