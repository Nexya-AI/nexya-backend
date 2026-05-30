"""Schémas Pydantic pour document_generator (C4.7a).

Aligné pattern strict NEXYA :
    - Literal pour les enums sûrs (anti path traversal sur `template`)
    - `model_config = ConfigDict(from_attributes=True)` pour réponses
    - `Field(..., min_length, max_length, ge, le)` pour validation
    - Docstrings FR pour OpenAPI (lu par Swagger UI prod)
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# ── Enums Literal (anti-injection + Pydantic strict) ─────────────────

DocumentTemplate = Literal["school", "minimal"]
"""Templates disponibles V1 C4.7a.

- `school` : entête « Devoir / Travail dirigé » + métadonnées Niveau/
  Matière/Date, style sobre noir/blanc, taille A4 portrait, marges 2cm.
- `minimal` : page blanche minimaliste, titre H1 + body markdown, sans
  entête institutionnel. Idéal pour notes, mémos, exports généraux.

V2 (C4.7b/c/d) ajoutera : sciences, legal, medicine, cooking, business.
"""

DocumentFormat = Literal["pdf"]
"""Format de sortie V1 = PDF seul.

V2 C4.7b ajoutera `docx` (python-docx). V1 strict : seul PDF accepté,
le champ Pydantic Literal rejette `docx`/`both` avec 422 propre.
"""


# ── Sub-schemas ──────────────────────────────────────────────────────


class DocumentGenerateOptions(BaseModel):
    """Options de génération (toutes optionnelles).

    Champs `school` :
        subject : matière (ex: « Mathématiques », « SVT »)
        level : niveau scolaire (ex: « Terminale S », « Master 1 »)
        date_iso : date affichée dans l'entête (défaut = aujourd'hui)

    Champs communs :
        include_toc : table des matières auto (V2, ignoré V1)
        page_numbers : numérotation bas de page (défaut True)
    """

    subject: str | None = Field(
        default=None,
        max_length=100,
        description="Matière scolaire affichée dans l'entête (template `school`).",
    )
    level: str | None = Field(
        default=None,
        max_length=100,
        description="Niveau scolaire affiché dans l'entête (template `school`).",
    )
    date_iso: str | None = Field(
        default=None,
        max_length=32,
        description="Date ISO 8601 (YYYY-MM-DD) affichée. Défaut = aujourd'hui UTC.",
    )
    include_toc: bool = Field(
        default=False,
        description="Table des matières auto. V2 — ignoré V1.",
    )
    page_numbers: bool = Field(
        default=True,
        description="Numérotation bas de page « N / Total ». Défaut True.",
    )


# ── Request ──────────────────────────────────────────────────────────


class DocumentGenerateRequest(BaseModel):
    """Body de `POST /generate/document`.

    Le backend récupère le contenu markdown source depuis la table
    `messages` via `(conversation_id, message_id)` (ownership check
    automatique via le `JOIN messages-conversations` standard NEXYA
    — pattern aligné C2 feedback + C1 reports).

    Pourquoi pas envoyer le markdown directement dans le body ?
        - Évite la duplication réseau (le LLM a déjà produit le texte,
          il est en DB).
        - Anti tampering : l'user ne peut pas modifier le contenu IA
          avant de le rendre (sécurité brand NEXYA).
        - Cap source côté backend = config setting, pas négociable.
    """

    conversation_id: UUID = Field(
        ...,
        description="UUID de la conversation source (ownership check IDOR-safe).",
    )
    message_id: UUID = Field(
        ...,
        description="UUID du message assistant dont le content est la source markdown.",
    )
    format: DocumentFormat = Field(
        default="pdf",
        description="Format de sortie. V1 = PDF seul. DOCX en C4.7b.",
    )
    template: DocumentTemplate = Field(
        default="minimal",
        description="Template Jinja2 à appliquer. V1 : `school` ou `minimal`.",
    )
    options: DocumentGenerateOptions = Field(
        default_factory=DocumentGenerateOptions,
        description="Options de personnalisation par template.",
    )
    title: str | None = Field(
        default=None,
        max_length=200,
        description=(
            "Titre principal du document (sert aussi de titre Library + "
            "filename PDF). Défaut = dérivé du contenu via title_generator."
        ),
    )


# ── Response ─────────────────────────────────────────────────────────


class DocumentGenerateResponse(BaseModel):
    """Réponse de `POST /generate/document`.

    Le client (Flutter) consomme :
        - `download_url` : presigned URL MinIO TTL 30 min (le client
          peut télécharger directement sans repasser par l'API)
        - `library_id` : pour deep link futur « voir dans bibliothèque »
        - `truncated` : si True, afficher badge « tronqué à N pages »
        - `pages` : nombre réel de pages rendues (≤ max_pages cap)
    """

    model_config = ConfigDict(from_attributes=True)

    library_id: UUID = Field(
        ...,
        description="UUID de l'item Library créé (type=document, file_type=pdf).",
    )
    download_url: str = Field(
        ...,
        description="Presigned URL MinIO TTL 30 min pour download direct.",
    )
    filename: str = Field(
        ...,
        max_length=255,
        description="Nom de fichier suggéré pour le download (ex: `document_2026-05-30.pdf`).",
    )
    size_bytes: int = Field(
        ...,
        ge=0,
        description="Taille du PDF en bytes APRÈS compression pikepdf.",
    )
    pages: int = Field(
        ...,
        ge=0,
        description="Nombre de pages rendues (cap à max_pages, voir truncated).",
    )
    truncated: bool = Field(
        ...,
        description="True si le PDF a été tronqué au cap pages.",
    )
    expires_at: datetime = Field(
        ...,
        description="ISO datetime UTC d'expiration de `download_url` (TTL 30 min).",
    )
    generated_at: datetime = Field(
        ...,
        description="ISO datetime UTC de fin de rendu PDF.",
    )
