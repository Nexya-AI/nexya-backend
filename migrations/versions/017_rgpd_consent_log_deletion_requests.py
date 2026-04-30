"""RGPD compliance — `consent_log` + `deletion_requests` + `ai_calls`
enrichi (Article 13 AI Act).

Revision ID: 017_rgpd
Revises: 016_expert_corpus_chunks
Create Date: 2026-04-26

Session J1 — Conformité RGPD (UE 2016/679 — Articles 7, 15, 17, 20)
+ AI Act UE (Règlement 2024/1689 applicable août 2026 — Article 13).

## Tables

### `consent_log` — historique horodaté des consentements

Trace **chaque évolution** de consentement (granted ou revoked) avec
l'horodatage exact + la version du document consenti + le hash SHA-256
du document. C'est la **preuve juridique** que NEXYA a obtenu le
consentement explicite à un instant T pour une version précise du
document. Sans elle, en cas d'audit CNIL ou litige, NEXYA ne peut PAS
démontrer la conformité Article 7 RGPD.

Design décisions :
- 7 catégories de consentement (`tos`, `privacy_policy`,
  `ai_processing`, `ai_training_data`, `marketing_email`, `analytics`,
  `cookies`) — granularité fine pour permettre opt-out partiel.
- `document_version` + `document_hash` figés au moment du
  consentement — anti-modification rétroactive (NEXYA ne peut pas
  changer la ToS et prétendre que le user avait consenti à la nouvelle).
- `ip_address` + `user_agent` capturés pour preuve forensic. Anonymisés
  /24 (IPv4) ou /48 (IPv6) au-delà de 2 ans (cron Phase 12).
- FK `user_id` ON DELETE CASCADE — un user purgé efface son
  consentement (la trace globale reste dans `auth_events.consent_*`).

### `deletion_requests` — queue de purge différée

Implémente le **workflow 2-step** Article 17 RGPD :
1. User demande la suppression → `status='pending'` +
   `scheduled_purge_at = NOW() + grace_period`.
2. Cron quotidien 03:17 UTC purge les requests dont `scheduled_purge_at`
   est passé. Cascade SQL hard delete + suppression des blobs MinIO.

Le délai de grâce (30 jours par défaut) protège contre :
- Erreur user (clic sur « supprimer compte » par accident).
- Compte compromis utilisé pour effacer les données légitimes du user.
- Contestation a posteriori (le user peut rétracter via
  `POST /rgpd/user/account/delete-request/cancel`).

Design décisions :
- Index unique partiel `WHERE status IN ('pending','processing')` —
  un user ne peut avoir qu'UNE demande active à la fois (idempotence
  côté service).
- `purge_summary_json` stocke l'email du user AVANT anonymisation
  pour pouvoir envoyer le mail de confirmation post-purge (le hard
  delete cascade efface l'email original).

### Enrichissement `ai_calls` (AI Act Article 13)

3 nouvelles colonnes pour conformité « registre des traitements » :
- `legal_basis` (4 valeurs) — base légale du traitement IA.
- `data_categories` (6 valeurs) — catégorie de données traitées.
- `retention_until` — date limite de conservation (90j par défaut).

Backfill : tous les rows existants → `legal_basis='contract'` +
`data_categories='user_input'` + `retention_until=created_at + 90j`.

Cela permet d'exporter le registre AI Act via
`GET /rgpd/admin/ai-act-registry` (CSV/JSON) sans table dédiée,
en réutilisant l'existant.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "017_rgpd"
down_revision = "016_expert_corpus_chunks"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ═══════════════════════════════════════════════════════════════
    # TABLE : consent_log
    # ═══════════════════════════════════════════════════════════════
    op.create_table(
        "consent_log",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("consent_type", sa.String(32), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "granted_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("document_version", sa.String(32), nullable=False),
        sa.Column("document_hash", sa.String(64), nullable=False),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(256), nullable=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("metadata_json", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "consent_type IN ('tos','privacy_policy','ai_processing',"
            "'ai_training_data','marketing_email','analytics','cookies')",
            name="ck_consent_log_type",
        ),
        sa.CheckConstraint(
            "status IN ('granted','revoked')",
            name="ck_consent_log_status",
        ),
        sa.CheckConstraint(
            "source IN ('register','settings_screen','api','cookies_banner','admin_grant')",
            name="ck_consent_log_source",
        ),
        sa.CheckConstraint(
            "char_length(document_hash) = 64",
            name="ck_consent_log_hash_length",
        ),
        sa.CheckConstraint(
            "(status = 'granted' AND revoked_at IS NULL) OR "
            "(status = 'revoked' AND revoked_at IS NOT NULL)",
            name="ck_consent_log_status_revoked_consistency",
        ),
    )
    # Hot path : « ce user a-t-il consenti à X et est-ce toujours actif ? »
    op.create_index(
        "ix_consent_log_user_active",
        "consent_log",
        ["user_id", "consent_type"],
        postgresql_where=sa.text("status = 'granted' AND revoked_at IS NULL"),
    )
    # Reporting CNIL : « combien de consentements granted entre 2 dates ? »
    op.create_index(
        "ix_consent_log_type_time",
        "consent_log",
        ["consent_type", "granted_at"],
    )
    # Lookup historique d'un user (export RGPD).
    op.create_index(
        "ix_consent_log_user_time",
        "consent_log",
        ["user_id", "granted_at"],
    )

    # ═══════════════════════════════════════════════════════════════
    # TABLE : deletion_requests
    # ═══════════════════════════════════════════════════════════════
    op.create_table(
        "deletion_requests",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
        ),
        sa.Column(
            "user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "scheduled_purge_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("purged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("purge_summary_json", JSONB(), nullable=True),
        sa.Column("ip_address", sa.String(64), nullable=True),
        sa.Column("user_agent", sa.String(256), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('pending','cancelled','processing','completed','failed')",
            name="ck_deletion_requests_status",
        ),
        sa.CheckConstraint(
            "scheduled_purge_at >= requested_at",
            name="ck_deletion_requests_schedule_order",
        ),
    )
    # Cron : tous les `pending` dont la date est passée.
    op.create_index(
        "ix_deletion_requests_due",
        "deletion_requests",
        ["scheduled_purge_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    # Idempotence : un user ne peut avoir qu'UNE demande en cours
    # (pending OU processing). Index unique partiel.
    op.create_index(
        "uq_deletion_requests_user_active",
        "deletion_requests",
        ["user_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending','processing')"),
    )

    # ═══════════════════════════════════════════════════════════════
    # ENRICHISSEMENT : ai_calls (AI Act Article 13)
    # ═══════════════════════════════════════════════════════════════
    # Ajout 3 colonnes nullable pour permettre le déploiement progressif.
    # Backfill ensuite avec defaults documentés.
    op.add_column(
        "ai_calls",
        sa.Column("legal_basis", sa.String(32), nullable=True),
    )
    op.add_column(
        "ai_calls",
        sa.Column("data_categories", sa.String(64), nullable=True),
    )
    op.add_column(
        "ai_calls",
        sa.Column("retention_until", sa.DateTime(timezone=True), nullable=True),
    )

    # Backfill : tous les appels existants → contract + user_input + 90j.
    op.execute(
        """
        UPDATE ai_calls
        SET legal_basis = 'contract',
            data_categories = 'user_input',
            retention_until = created_at + INTERVAL '90 days'
        WHERE legal_basis IS NULL
        """
    )

    # Pose les CHECK constraints APRÈS backfill (pour ne pas planter
    # sur les rows existants).
    op.create_check_constraint(
        "ck_ai_calls_legal_basis",
        "ai_calls",
        "legal_basis IS NULL OR legal_basis IN "
        "('contract','legitimate_interest','consent','legal_obligation')",
    )
    op.create_check_constraint(
        "ck_ai_calls_data_categories",
        "ai_calls",
        "data_categories IS NULL OR data_categories IN "
        "('user_input','prompt_history','file_content','voice_audio',"
        "'image_content','profile_data')",
    )

    # Index pour reporting AI Act registry par fenêtre temporelle +
    # base légale (« combien d'appels consent vs contract sur la
    # dernière année ? »).
    op.create_index(
        "ix_ai_calls_legal_basis_time",
        "ai_calls",
        ["legal_basis", "created_at"],
    )

    # ═══════════════════════════════════════════════════════════════
    # ENRICHISSEMENT : auth_events — 5 nouveaux event_types RGPD
    # ═══════════════════════════════════════════════════════════════
    # On élargit le CHECK pour ajouter :
    # - consent_granted / consent_revoked  (Article 7 RGPD)
    # - account_delete_requested / account_delete_cancelled
    #   (workflow 2-step Article 17)
    # - data_exported  (Article 15 RGPD — preuve d'envoi d'export)
    op.drop_constraint("ck_auth_events_event_type", "auth_events", type_="check")
    op.create_check_constraint(
        "ck_auth_events_event_type",
        "auth_events",
        "event_type IN ("
        "'register_success','register_failed',"
        "'login_success','login_failed',"
        "'logout',"
        "'password_change','password_reset_request','password_reset_success',"
        "'account_delete',"
        "'captcha_failed','device_quota_exceeded',"
        "'consent_granted','consent_revoked',"
        "'account_delete_requested','account_delete_cancelled',"
        "'data_exported'"
        ")",
    )


def downgrade() -> None:
    # Restaure le CHECK auth_events historique (sans les 5 RGPD).
    op.drop_constraint("ck_auth_events_event_type", "auth_events", type_="check")
    op.create_check_constraint(
        "ck_auth_events_event_type",
        "auth_events",
        "event_type IN ("
        "'register_success','register_failed',"
        "'login_success','login_failed',"
        "'logout',"
        "'password_change','password_reset_request','password_reset_success',"
        "'account_delete',"
        "'captcha_failed','device_quota_exceeded'"
        ")",
    )

    # Inversion stricte ordre.
    op.drop_index("ix_ai_calls_legal_basis_time", table_name="ai_calls")
    op.drop_constraint("ck_ai_calls_data_categories", "ai_calls", type_="check")
    op.drop_constraint("ck_ai_calls_legal_basis", "ai_calls", type_="check")
    op.drop_column("ai_calls", "retention_until")
    op.drop_column("ai_calls", "data_categories")
    op.drop_column("ai_calls", "legal_basis")

    op.drop_index("uq_deletion_requests_user_active", table_name="deletion_requests")
    op.drop_index("ix_deletion_requests_due", table_name="deletion_requests")
    op.drop_table("deletion_requests")

    op.drop_index("ix_consent_log_user_time", table_name="consent_log")
    op.drop_index("ix_consent_log_type_time", table_name="consent_log")
    op.drop_index("ix_consent_log_user_active", table_name="consent_log")
    op.drop_table("consent_log")
