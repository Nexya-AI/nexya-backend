"""DataExportService — RGPD Articles 15 (accès) + 20 (portabilité).

Session J1 — 2026-04-26.

Construit une archive ZIP en mémoire avec **toutes** les données
appartenant à un utilisateur, dans un format structuré + lisible par
machine (JSON), accompagnée d'un README.txt FR (Article 12 RGPD :
information dans un format clair).

Architecture :
- ZIP en mémoire via `zipfile.ZipFile(BytesIO())` — pas de tempfile
  disque (suffit jusqu'à ~500 MB ; au-delà → futur job arq async).
- Blobs MinIO (uploaded_files, library_items) → presigned URLs 7 jours
  inclus dans le ZIP, PAS le binaire lui-même (sinon explosion de taille).
- IPs anonymisées /24 (IPv4) ou /48 (IPv6) dans `auth_events`.
- `ai_calls` exporté SANS prompt content (privacy-by-default + AI Act).
- 0 password_hash, 0 storage_key brut, 0 cross-user leak (filter strict
  par `user_id`).
- Cap soft `rgpd_export_max_size_bytes` — si dépassé, manifest porte
  un flag `truncated=True` + `truncated_reason`.
"""

from __future__ import annotations

import io
import ipaddress
import json
import uuid
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.storage.object_store import ObjectStore, get_object_store
from app.features.auth.models import AuthEvent, DeviceToken, User
from app.features.chat.models import AbuseReport, Conversation, Message
from app.features.files.chunk_models import DocumentChunk
from app.features.files.models import UploadedFile
from app.features.library.models import LibraryItem
from app.features.memory.models import Memory
from app.features.notifications.models import (
    Notification,
    NotificationPreference,
)
from app.features.planner.models import (
    ScheduledTask,
    ScheduledTaskResult,
)
from app.features.projects.models import Project, ProjectFile
from app.features.rgpd.consent_service import ConsentService
from app.features.rgpd.models import DeletionRequest
from app.features.vision.models import VisionAnalysis
from app.features.voice.models import VoiceTranscription

log = structlog.get_logger(__name__)


# Champs à NE JAMAIS exporter (sécurité + privacy).
_USER_FIELDS_REDACTED = {"password_hash"}
# Pour device_tokens, on masque le token sauf les 8 derniers chars.
_DEVICE_TOKEN_TAIL_CHARS = 8
# Champs ai_calls à NE PAS exporter (prompts utilisateurs sensibles).
# `extra` peut contenir le prompt → on l'exclut.
_AI_CALL_REDACTED_FIELDS = {"extra"}


@dataclass
class ExportResult:
    """Résultat d'un build_export — ZIP bytes + manifest summary."""

    zip_bytes: bytes
    record_counts: dict[str, int]
    truncated: bool
    truncated_reason: str | None


def _anonymize_ip(ip: str | None) -> str | None:
    """Anonymise une IP : /24 pour IPv4, /48 pour IPv6.

    - `1.2.3.4` → `1.2.3.0/24`
    - `2001:db8::1` → `2001:db8::/48`
    - None / vide / invalide → None
    """
    if not ip:
        return None
    try:
        addr = ipaddress.ip_address(ip)
    except (ValueError, TypeError):
        return None
    if isinstance(addr, ipaddress.IPv4Address):
        net = ipaddress.ip_network(f"{ip}/24", strict=False)
    else:
        net = ipaddress.ip_network(f"{ip}/48", strict=False)
    return str(net)


def _serialize(value: Any) -> Any:
    """Convertit un objet ORM ou primitif en JSON-serializable."""
    if value is None:
        return None
    if isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    # Decimal, date, etc.
    return str(value)


def _row_to_dict(row: Any, *, redact: set[str] | None = None) -> dict:
    """Sérialise une row ORM en dict. Exclut les colonnes `redact`."""
    redact = redact or set()
    result = {}
    for column in row.__table__.columns:
        if column.name in redact:
            continue
        result[column.name] = _serialize(getattr(row, column.name))
    return result


def _mask_device_token(token: str) -> str:
    if len(token) <= _DEVICE_TOKEN_TAIL_CHARS:
        return "***"
    return "***" + token[-_DEVICE_TOKEN_TAIL_CHARS:]


class DataExportService:
    """Construit un ZIP RGPD complet pour un user."""

    def __init__(self, object_store: ObjectStore | None = None):
        self._object_store = object_store

    async def _store(self) -> ObjectStore:
        if self._object_store is None:
            self._object_store = get_object_store()
        return self._object_store

    async def build_export(self, user: User, db: AsyncSession) -> ExportResult:
        """Construit le ZIP en mémoire pour cet utilisateur.

        Pipeline :
        1. Charge toutes les rows user-scope via `select(...) WHERE user_id`.
        2. Sérialise en JSON.
        3. Génère presigned URLs 7j pour les blobs MinIO référencés.
        4. Construit manifest.json + README.txt + auth_events anonymisé.
        5. Cap soft : si > `rgpd_export_max_size_bytes` → flag truncated
           dans manifest mais on continue (l'user a quand même un export).
        """
        record_counts: dict[str, int] = {}
        buffer = io.BytesIO()

        with zipfile.ZipFile(
            buffer, mode="w", compression=zipfile.ZIP_DEFLATED, compresslevel=6
        ) as zf:
            # 1. users.json (profil — sans password_hash)
            user_dict = _row_to_dict(user, redact=_USER_FIELDS_REDACTED)
            zf.writestr("users.json", json.dumps(user_dict, ensure_ascii=False, indent=2))
            record_counts["users"] = 1

            # 2. consents.json (historique complet — preuve juridique)
            consents = await ConsentService.list_history_for_user(user.id, db)
            consent_dicts = [_row_to_dict(c) for c in consents]
            zf.writestr(
                "consents.json",
                json.dumps(consent_dicts, ensure_ascii=False, indent=2),
            )
            record_counts["consents"] = len(consent_dicts)

            # 3. deletion_requests.json (historique demandes)
            del_reqs = await self._fetch_all(
                db,
                select(DeletionRequest).where(DeletionRequest.user_id == user.id),
            )
            zf.writestr(
                "deletion_requests.json",
                json.dumps(
                    [_row_to_dict(d) for d in del_reqs],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            record_counts["deletion_requests"] = len(del_reqs)

            # 4. auth_events.json (avec IP anonymisée /24)
            events = await self._fetch_all(
                db,
                select(AuthEvent).where(AuthEvent.user_id == user.id),
            )
            event_dicts = []
            for e in events:
                d = _row_to_dict(e)
                d["ip"] = _anonymize_ip(d.get("ip"))
                event_dicts.append(d)
            zf.writestr(
                "auth_events.json",
                json.dumps(event_dicts, ensure_ascii=False, indent=2),
            )
            record_counts["auth_events"] = len(event_dicts)

            # 5. device_tokens.json (token masqué sauf 8 derniers)
            tokens = await self._fetch_all(
                db,
                select(DeviceToken).where(DeviceToken.user_id == user.id),
            )
            tok_dicts = []
            for t in tokens:
                d = _row_to_dict(t)
                d["token"] = _mask_device_token(d.get("token", ""))
                tok_dicts.append(d)
            zf.writestr(
                "device_tokens.json",
                json.dumps(tok_dicts, ensure_ascii=False, indent=2),
            )
            record_counts["device_tokens"] = len(tok_dicts)

            # 6. chat — conversations + messages + abuse_reports
            convs = await self._fetch_all(
                db,
                select(Conversation).where(Conversation.user_id == user.id),
            )
            conv_ids = [c.id for c in convs]
            zf.writestr(
                "chat/conversations.json",
                json.dumps(
                    [_row_to_dict(c) for c in convs],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            record_counts["conversations"] = len(convs)

            messages = []
            if conv_ids:
                messages = await self._fetch_all(
                    db,
                    select(Message).where(Message.conversation_id.in_(conv_ids)),
                )
            zf.writestr(
                "chat/messages.json",
                json.dumps(
                    [_row_to_dict(m) for m in messages],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            record_counts["messages"] = len(messages)

            reports = await self._fetch_all(
                db,
                select(AbuseReport).where(AbuseReport.user_id == user.id),
            )
            zf.writestr(
                "chat/abuse_reports.json",
                json.dumps(
                    [_row_to_dict(r) for r in reports],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            record_counts["abuse_reports"] = len(reports)

            # 7. projects + files
            projects = await self._fetch_all(
                db,
                select(Project).where(Project.user_id == user.id),
            )
            project_ids = [p.id for p in projects]
            zf.writestr(
                "projects/projects.json",
                json.dumps(
                    [_row_to_dict(p) for p in projects],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            record_counts["projects"] = len(projects)

            pfiles = []
            if project_ids:
                pfiles = await self._fetch_all(
                    db,
                    select(ProjectFile).where(ProjectFile.project_id.in_(project_ids)),
                )
            zf.writestr(
                "projects/files.json",
                json.dumps(
                    [_row_to_dict(pf) for pf in pfiles],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            record_counts["project_files"] = len(pfiles)

            # 8. library — items + presigned URLs
            lib_items = await self._fetch_all(
                db,
                select(LibraryItem).where(LibraryItem.user_id == user.id),
            )
            zf.writestr(
                "library/items.json",
                json.dumps(
                    [_row_to_dict(li) for li in lib_items],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            record_counts["library_items"] = len(lib_items)
            blob_urls = await self._presign_many([(li.id, li.storage_key) for li in lib_items])
            zf.writestr(
                "library/blob_urls.json",
                json.dumps(blob_urls, ensure_ascii=False, indent=2),
            )

            # 9. memory
            mems = await self._fetch_all(
                db,
                select(Memory).where(Memory.user_id == user.id),
            )
            mem_dicts = []
            for m in mems:
                d = _row_to_dict(m)
                # `embedding` est un vecteur 1536 → trop volumineux pour
                # l'export user. On exclut.
                d.pop("embedding", None)
                mem_dicts.append(d)
            zf.writestr(
                "memory/memories.json",
                json.dumps(mem_dicts, ensure_ascii=False, indent=2),
            )
            record_counts["memories"] = len(mems)

            # 10. notifications + preferences
            notifs = await self._fetch_all(
                db,
                select(Notification).where(Notification.user_id == user.id),
            )
            zf.writestr(
                "notifications/notifications.json",
                json.dumps(
                    [_row_to_dict(n) for n in notifs],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            record_counts["notifications"] = len(notifs)

            prefs = await self._fetch_all(
                db,
                select(NotificationPreference).where(NotificationPreference.user_id == user.id),
            )
            zf.writestr(
                "notifications/preferences.json",
                json.dumps(
                    [_row_to_dict(p) for p in prefs],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            record_counts["notification_preferences"] = len(prefs)

            # 11. planner — tasks + results
            tasks = await self._fetch_all(
                db,
                select(ScheduledTask).where(ScheduledTask.user_id == user.id),
            )
            task_ids = [t.id for t in tasks]
            zf.writestr(
                "planner/tasks.json",
                json.dumps(
                    [_row_to_dict(t) for t in tasks],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            record_counts["scheduled_tasks"] = len(tasks)

            results_rows = []
            if task_ids:
                results_rows = await self._fetch_all(
                    db,
                    select(ScheduledTaskResult).where(ScheduledTaskResult.task_id.in_(task_ids)),
                )
            zf.writestr(
                "planner/results.json",
                json.dumps(
                    [_row_to_dict(r) for r in results_rows],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            record_counts["scheduled_task_results"] = len(results_rows)

            # 12. files — uploaded + chunks + presigned blobs
            uploaded = await self._fetch_all(
                db,
                select(UploadedFile).where(UploadedFile.user_id == user.id),
            )
            upload_ids = [u.id for u in uploaded]
            zf.writestr(
                "files/uploaded.json",
                json.dumps(
                    [_row_to_dict(u) for u in uploaded],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            record_counts["uploaded_files"] = len(uploaded)

            chunks = []
            if upload_ids:
                chunks = await self._fetch_all(
                    db,
                    select(DocumentChunk).where(DocumentChunk.file_id.in_(upload_ids)),
                )
            chunk_dicts = []
            for c in chunks:
                d = _row_to_dict(c)
                # embedding 1536 dim — trop volumineux.
                d.pop("embedding", None)
                chunk_dicts.append(d)
            zf.writestr(
                "files/chunks.json",
                json.dumps(chunk_dicts, ensure_ascii=False, indent=2),
            )
            record_counts["document_chunks"] = len(chunks)

            blob_urls_files = await self._presign_many([(u.id, u.storage_key) for u in uploaded])
            zf.writestr(
                "files/blob_urls.json",
                json.dumps(blob_urls_files, ensure_ascii=False, indent=2),
            )

            # 13. voice — transcriptions
            voices = await self._fetch_all(
                db,
                select(VoiceTranscription).where(VoiceTranscription.user_id == user.id),
            )
            zf.writestr(
                "voice/transcriptions.json",
                json.dumps(
                    [_row_to_dict(v) for v in voices],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            record_counts["voice_transcriptions"] = len(voices)

            # 14. vision — analyses
            visions = await self._fetch_all(
                db,
                select(VisionAnalysis).where(VisionAnalysis.user_id == user.id),
            )
            zf.writestr(
                "vision/analyses.json",
                json.dumps(
                    [_row_to_dict(v) for v in visions],
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            record_counts["vision_analyses"] = len(visions)

            # 15. ai_calls (AI Act registry — sans `extra` qui peut contenir prompt)
            from app.ai.models import AiCall

            calls = await self._fetch_all(
                db,
                select(AiCall).where(AiCall.user_id == user.id),
            )
            call_dicts = [_row_to_dict(c, redact=_AI_CALL_REDACTED_FIELDS) for c in calls]
            zf.writestr(
                "ai_calls/ai_calls.json",
                json.dumps(call_dicts, ensure_ascii=False, indent=2),
            )
            record_counts["ai_calls"] = len(call_dicts)

            # 16. README + manifest (en dernier pour avoir record_counts finaux)
            zf.writestr("README.txt", _README_FR)

            # Cap soft : on calcule la taille approximative du buffer
            # courant (avant fermeture du ZipFile). zipfile streame, donc
            # le buffer est fidèle.
            current_size = buffer.getbuffer().nbytes
            truncated = current_size > settings.rgpd_export_max_size_bytes
            truncated_reason = (
                f"Export size {current_size} > cap {settings.rgpd_export_max_size_bytes}"
                if truncated
                else None
            )

            manifest = {
                "user_id": str(user.id),
                "exported_at": datetime.now(UTC).isoformat(),
                "schema_version": "1.0",
                "record_counts": record_counts,
                "truncated": truncated,
                "truncated_reason": truncated_reason,
                "blob_url_ttl_seconds": settings.rgpd_blob_presigned_ttl_seconds,
            }
            zf.writestr(
                "manifest.json",
                json.dumps(manifest, ensure_ascii=False, indent=2),
            )

        zip_bytes = buffer.getvalue()
        log.info(
            "rgpd.export.built",
            user_id=str(user.id),
            size_bytes=len(zip_bytes),
            record_counts=record_counts,
            truncated=truncated,
        )
        return ExportResult(
            zip_bytes=zip_bytes,
            record_counts=record_counts,
            truncated=truncated,
            truncated_reason=truncated_reason,
        )

    async def _fetch_all(self, db: AsyncSession, stmt: Any) -> list[Any]:
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def _presign_many(self, items: list[tuple[uuid.UUID, str]]) -> list[dict]:
        """Génère presigned URLs pour une liste de (id, storage_key)."""
        if not items:
            return []
        store = await self._store()
        urls = []
        for item_id, key in items:
            try:
                url = await store.generate_presigned_url(
                    key,
                    ttl_seconds=settings.rgpd_blob_presigned_ttl_seconds,
                )
            except Exception as exc:  # noqa: BLE001 — fail-safe
                log.warning(
                    "rgpd.export.presign_failed",
                    storage_key=key,
                    error=str(exc),
                )
                url = None
            urls.append({"id": str(item_id), "url": url})
        return urls


_README_FR = """\
NEXYA — Export de vos données personnelles
==========================================

Ce fichier ZIP contient l'ensemble des données que NEXYA détient sur vous,
en vertu des Articles 15 (droit d'accès) et 20 (portabilité) du
Règlement Général sur la Protection des Données (RGPD UE 2016/679).

Contenu de l'archive
--------------------
- manifest.json          : résumé technique (nb de lignes par table,
                           date d'export, version du schéma).
- README.txt             : ce fichier.
- users.json             : votre profil (sans hash de mot de passe).
- consents.json          : historique de vos consentements.
- deletion_requests.json : historique des demandes de suppression.
- auth_events.json       : journal d'authentification (IP anonymisée /24).
- device_tokens.json     : appareils enregistrés (tokens masqués).
- chat/                  : conversations + messages + signalements.
- projects/              : vos projets et fichiers attachés.
- library/               : votre bibliothèque média + URLs de
                           téléchargement (valides 7 jours).
- memory/                : faits durables que NEXYA a mémorisés sur
                           vous (sans les vecteurs sémantiques).
- notifications/         : timeline + préférences.
- planner/               : tâches planifiées + résultats.
- files/                 : fichiers téléversés + URLs (valides 7 jours).
- voice/                 : transcriptions audio.
- vision/                : analyses d'images.
- ai_calls/              : registre des appels d'intelligence
                           artificielle (sans le contenu de vos
                           prompts, conformément aux Articles 13 et 50
                           du Règlement IA UE 2024/1689).

Format des fichiers
-------------------
Tous les fichiers sont au format JSON UTF-8, structurés et lisibles par
machine, conformément à l'Article 20 RGPD (« format structuré, couramment
utilisé et lisible par machine »).

Téléchargement des médias
-------------------------
Les fichiers binaires (images, audio, documents) ne sont PAS inclus dans
ce ZIP pour éviter une taille excessive. Vous trouverez dans
`library/blob_urls.json` et `files/blob_urls.json` des URLs signées
valides 7 jours qui vous permettent de les télécharger un par un. Au-delà
de ce délai, vous devrez refaire un export.

Suppression complète
--------------------
Pour demander la suppression définitive de votre compte (Article 17
RGPD), utilisez l'endpoint `POST /rgpd/user/account/delete-request` ou
le bouton « Supprimer mon compte » dans l'application. La suppression
est différée de 30 jours pour vous protéger contre les erreurs de
manipulation. Vous pouvez annuler la demande à tout moment durant ce
délai.

Questions ?
-----------
Contactez le délégué à la protection des données (DPO) de NEXYA en
écrivant à : dpo@nexya.ai

Date de génération de cet export : voir `manifest.json` champ `exported_at`.
"""
