"""Service CodeProject — construction .zip en mémoire + upload MinIO (C4.6).

Pipeline strict 7 étapes :
  1. Construit `BytesIO` + `zipfile.ZipFile(mode="w", ZIP_DEFLATED, level=6)`
  2. `zf.writestr(file.filename, file.content)` pour chaque fichier
  3. Ajoute `README.md` auto-généré avec project_name + structure + footer NEXYA
  4. SHA-256 du bytes (pour storage_key + dédup futur si ajouté)
  5. `storage_key = f"{user_id}/code-projects/{sha[:2]}/{sha}.zip"`
     (sharding 2-char aligné Library C3 / Files E3)
  6. Upload MinIO via `ObjectStore.upload_bytes`
  7. Presigned URL TTL configurable + retour `BuildZipResponse`

Cap dur taille .zip = `settings.code_projects_max_zip_size_mb` (50 MB
défaut). Si dépassé → `ObjectStoreUnavailableException` 503 ? Non :
on lève une exception métier dédiée pour 422 (client a soumis un
payload trop gros — Pydantic capait déjà à 5 MB texte brut, donc le
.zip ne devrait jamais dépasser ~2 MB).

Aucune persistance DB V1 — le .zip vit MinIO TTL 24h. V2 si signal
user : table `code_project_zips` avec quota + auto-save Library
type='code' file_type='zip'.
"""

from __future__ import annotations

import hashlib
import io
import uuid
import zipfile
from datetime import UTC, datetime, timedelta

import structlog

from app.config import settings
from app.core.storage.object_store import ObjectStore
from app.features.code_projects.schemas import BuildZipResponse
from app.features.rich_content.schemas import CodeProjectDraftData

log = structlog.get_logger()


def _sanitize_filename(name: str) -> str:
    """Sanitize project_name → filename FS-safe pour le download client.

    - Remplace chars non-FS-safe (`<>:"/\\|?*` + chars de contrôle) par `_`
    - Cap 100 chars (anti-ENAMETOOLONG sur iOS/Android FS)
    - Strip whitespace
    - Fallback "code-project" si vide post-sanitization
    """
    import re

    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip()
    if not cleaned:
        cleaned = "code-project"
    return cleaned[:100]


def _build_readme(payload: CodeProjectDraftData) -> str:
    """Génère un README.md automatique pour le .zip.

    Inclus :
    - Titre = project_name
    - Description si présente
    - Liste des fichiers avec leurs langages
    - Footer signature NEXYA
    """
    lines = [
        f"# {payload.project_name}",
        "",
    ]

    if payload.description:
        lines.extend([payload.description, ""])

    if payload.project_type:
        lines.extend([f"**Type de projet** : `{payload.project_type}`", ""])

    lines.extend(["## Structure", ""])
    for f in payload.files:
        lines.append(f"- `{f.filename}` ({f.language})")

    lines.extend(
        [
            "",
            "---",
            "",
            "_Projet généré par NEXYA AI — https://nexyalabs.com_",
            "",
        ]
    )
    return "\n".join(lines)


class CodeProjectService:
    """Service construction .zip projet code multi-fichiers (C4.6)."""

    @staticmethod
    async def build_zip(
        *,
        payload: CodeProjectDraftData,
        user_id: uuid.UUID,
        object_store: ObjectStore,
    ) -> BuildZipResponse:
        """Construit le .zip en mémoire, upload MinIO, retourne presigned URL.

        Args:
            payload: validation Pydantic stricte déjà appliquée (2-50
                fichiers, filename path-safe, cap 5 MB total).
            user_id: pour le sharding du storage_key + traçabilité forensic.
            object_store: instance ObjectStore (mock-first ou S3/MinIO réel).

        Returns:
            `BuildZipResponse` avec download_url + filename + size_bytes
            + expires_at.

        Raises:
            ValueError si le .zip dépasse cap dur `code_projects_max_zip_size_mb`.
            ObjectStoreUnavailableException si MinIO down (propage telle quelle).
        """
        # 1. Construit le .zip en mémoire.
        buffer = io.BytesIO()
        with zipfile.ZipFile(
            buffer,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=6,
        ) as zf:
            # 2. Écrit chaque fichier.
            for f in payload.files:
                # Pydantic _validate_filename_path_safe a déjà normalisé
                # les `\\` Windows en `/` Unix. zipfile attend `/` standard.
                zf.writestr(f.filename, f.content)

            # 3. README.md auto-généré.
            readme = _build_readme(payload)
            zf.writestr("README.md", readme)

        zip_bytes = buffer.getvalue()
        size_bytes = len(zip_bytes)

        # 4. Cap dur taille .zip.
        max_bytes = settings.code_projects_max_zip_size_mb * 1024 * 1024
        if size_bytes > max_bytes:
            raise ValueError(
                f"Le .zip généré dépasse le cap dur de "
                f"{settings.code_projects_max_zip_size_mb} MB "
                f"(actuel : {size_bytes / 1024 / 1024:.2f} MB)."
            )

        # 5. SHA-256 pour storage_key (dédup-friendly + sharding 2-char).
        sha = hashlib.sha256(zip_bytes).hexdigest()
        storage_key = f"{user_id}/code-projects/{sha[:2]}/{sha}.zip"

        # 6. Upload MinIO via ObjectStore (mock-first ou réel).
        await object_store.upload_bytes(
            storage_key,
            zip_bytes,
            mime_type="application/zip",
            metadata={"user_id": str(user_id), "project_name": payload.project_name},
        )

        # 7. Presigned URL TTL configurable.
        ttl = settings.code_projects_zip_presigned_ttl_seconds
        download_url = await object_store.generate_presigned_url(
            storage_key, ttl_seconds=ttl, method="GET"
        )
        expires_at = datetime.now(UTC) + timedelta(seconds=ttl)

        # 8. Filename FS-safe côté client.
        filename = _sanitize_filename(payload.project_name) + ".zip"

        log.info(
            "code_projects.build_zip.success",
            user_id=str(user_id),
            project_name=payload.project_name,
            files_count=len(payload.files),
            size_bytes=size_bytes,
            sha256=sha[:12],  # tronqué pour log
        )

        return BuildZipResponse(
            download_url=download_url,
            filename=filename,
            size_bytes=size_bytes,
            expires_at=expires_at,
        )
