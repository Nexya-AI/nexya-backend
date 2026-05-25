"""Schémas Pydantic pour `POST /metadata/url-preview` (Session C4.2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field, HttpUrl


class UrlPreviewRequest(BaseModel):
    """Requête de preview d'une URL.

    `url` est strictement validé `HttpUrl` Pydantic (http/https uniquement)
    + cap 2048 chars (RFC 7230 recommandation pratique). L'anti-SSRF est
    appliqué côté service (résolution DNS + check IP non-privée).
    """

    url: HttpUrl = Field(
        ...,
        description=(
            "URL absolue http(s) à prévisualiser. Validé Pydantic + "
            "anti-SSRF côté service (rejette IPs privées 10.x/192.168.x/"
            "127.x/169.254.x/fe80::/etc.)."
        ),
    )


class UrlPreviewResponse(BaseModel):
    """Réponse de preview d'une URL.

    Tous les champs sauf `url` et `fetched_at` sont optionnels car la page
    cible peut ne pas exposer d'OG tags. Le frontend Flutter affiche les
    champs disponibles + fallback `url` brute si tout est null.
    """

    url: str = Field(..., description="URL cible (échoée pour cohérence).")
    title: str | None = Field(
        None,
        max_length=200,
        description="og:title ou <title>, tronqué à 200 chars, HTML stripped.",
    )
    description: str | None = Field(
        None,
        max_length=300,
        description="og:description, tronqué à 300 chars, HTML stripped.",
    )
    og_image_url: str | None = Field(
        None,
        description=(
            "og:image absolue (résolution relative→absolue côté service). "
            "Le frontend lazy-load via cached_network_image."
        ),
    )
    favicon_url: str | None = Field(
        None,
        description=("Favicon absolue (fallback /favicon.ico si pas de <link rel='icon'>)."),
    )
    fetched_at: datetime = Field(
        ...,
        description="Timestamp UTC du fetch backend (utile cache debugging).",
    )
    from_cache: bool = Field(
        False,
        description=(
            "True si la réponse vient du cache Redis (TTL 7j sur sha256(url)). "
            "Le frontend peut afficher un indicateur subtil 'depuis cache' si "
            "souhaité (V2)."
        ),
    )
