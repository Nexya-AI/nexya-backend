"""Module `code_projects` (C4.6).

Endpoint `POST /code-projects/build-zip` qui construit un .zip en mémoire
depuis un payload `CodeProjectDraftData` (rich_content C4.6) + upload
MinIO + retourne presigned URL TTL 24h.

Pattern strict aligné `app/features/rgpd/data_export_service.py` :
- `zipfile.ZipFile(BytesIO(), mode="w", ZIP_DEFLATED, compresslevel=6)`
- `zf.writestr(filename, content)` pour chaque fichier
- Ajoute `README.md` auto-généré avec project_name + structure + footer NEXYA
- SHA-256 du bytes pour storage_key MinIO dédup-friendly
- Storage key sharding 2-char SHA aligné Library C3
- Presigned URL via `ObjectStore.generate_presigned_url` (HMAC local)
- Aucune persistance DB V1 (re-generate à la demande, pas de cache backend)
"""

from __future__ import annotations

from app.features.code_projects.schemas import BuildZipRequest, BuildZipResponse
from app.features.code_projects.service import CodeProjectService

__all__ = [
    "BuildZipRequest",
    "BuildZipResponse",
    "CodeProjectService",
]
