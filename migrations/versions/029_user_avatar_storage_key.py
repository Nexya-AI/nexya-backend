"""Add `users.avatar_storage_key` for the dedicated avatar upload pipeline.

Revision ID: 029_user_avatar_storage_key
Revises: 028_document_jobs
Create Date: 2026-06-21 (Avatar lot — POST /user/avatar full-stack).

Pourquoi une colonne dédiée `avatar_storage_key` plutôt que réutiliser
`avatar_url` ?

  - `users.avatar_url` (déjà présente) stockerait soit une URL publique
    permanente, soit une presigned MinIO. Une presigned **expire** (TTL
    1 h par défaut) → si on la persiste telle quelle, elle devient
    morte au prochain `GET /user/profile`. Stocker une URL publique
    fixe imposerait un bucket public (fuite + énumération).

  - **Décision senior** : on stocke la **clé de stockage opaque**
    (`users/{user_id}/avatar.{ext}`) et on **régénère une presigned
    fraîche à chaque lecture** du profil (`build_profile_response`).
    L'`avatar_url` retourné dans `UserProfile` est donc toujours signé
    et valide, jamais périmé. La colonne legacy `avatar_url` n'est plus
    écrite par le flux avatar (elle reste pour rétrocompat, toujours
    `NULL` désormais — overridée à la lecture).

  - **Clé FIXE par user** (`avatar.{ext}`, overwrite) : zéro orphelin,
    pas de cron de nettoyage. Un ré-upload écrase le blob précédent ;
    la presigned régénérée à chaque lecture busted naturellement le
    cache client (signature + expiry changent à chaque `GET`).

VARCHAR(512) : aligné sur `uploaded_files.storage_key` (clés MinIO
shardées). NULL = pas d'avatar (l'user voit l'asset/initiales fallback).

Rollback strict inverse : DROP COLUMN.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "029_user_avatar_storage_key"
down_revision = "028_document_jobs"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Ajoute `users.avatar_storage_key VARCHAR(512) NULL`."""
    op.add_column(
        "users",
        sa.Column("avatar_storage_key", sa.String(length=512), nullable=True),
    )


def downgrade() -> None:
    """Rollback strict inverse : DROP COLUMN."""
    op.drop_column("users", "avatar_storage_key")
