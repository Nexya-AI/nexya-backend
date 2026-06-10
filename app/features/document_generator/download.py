"""Téléchargement de documents générés — chemin API proxy (fix P0 2026-06-10).

PROBLÈME RÉSOLU
---------------
Avant ce fix, `download_url` était un **presigned URL MinIO** (`S3_ENDPOINT`).
En production, MinIO n'a AUCUN port public (réseau Docker interne `nexya-prod`)
et Caddy ne route que `api.nexyalabs.com → nexya-api:8000`. Le presigned URL
(host `http://minio:9000`) était donc **physiquement injoignable depuis le
téléphone** → `dio.get(downloadUrl)` échouait → « erreur de connexion » à chaque
clic sur « Générer ». Latent depuis C4.7.

SOLUTION
--------
On renvoie un **chemin relatif** vers un endpoint API authentifié
(`GET /generate/document/download/{library_id}`) qui stream le binaire depuis
MinIO interne (le backend, lui, est dans le réseau Docker). L'API est joignable
(TLS + Caddy), le dio Flutter est `apiClientProvider` (baseUrl=api.nexyalabs.com
+ JWT Bearer auto) → il résout le chemin relatif et y attache le token. Owner
check IDOR-safe côté backend. **Fix 100 % backend, aucun rebuild APK** (le
frontend faisait déjà `dio.get(downloadUrl)` — seul le contenu de l'URL change).
"""

from __future__ import annotations

import uuid

# Préfixe router `/generate` + sous-chemin. Doit rester aligné sur le
# `@router.get(...)` de `router.py`. Un seul endroit de vérité.
DOCUMENT_DOWNLOAD_ROUTE = "/generate/document/download"


def build_document_download_path(library_id: uuid.UUID) -> str:
    """Chemin RELATIF de téléchargement d'un document généré.

    Relatif (et non absolu) car le dio Flutter (`apiClientProvider`) a déjà
    `baseUrl = api.nexyalabs.com` configuré + l'intercepteur JWT. `dio.get(path)`
    résout le chemin contre la baseUrl et y attache le Bearer automatiquement.
    Aucune config backend de host public requise.
    """
    return f"{DOCUMENT_DOWNLOAD_ROUTE}/{library_id}"
