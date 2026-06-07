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

DocumentTemplate = Literal["school", "minimal", "sciences", "legal", "medicine"]
"""Templates disponibles (C4.7a + C4.7c).

Templates V1 (C4.7a) :
- `school` : entête « Devoir / Travail dirigé » + métadonnées Niveau/
  Matière/Date, style sobre noir/blanc, A4 portrait, marges 2cm.
- `minimal` : page blanche minimaliste, titre H1 + body markdown, sans
  entête institutionnel. Idéal pour notes, mémos, exports généraux.

Templates V2 (C4.7c, livré 2026-05-31) — réutilisent les options
existantes (subject/level/date_iso) avec resignification sémantique :

- `sciences` : article scientifique. Style sobre académique.
  · `subject` = Discipline (ex: « Physique », « Biologie moléculaire »)
  · `level` = Établissement (ex: « L3 Université Yaoundé I »)
  · Footer disclaimer figé « Document de travail — vérifier les sources ».

- `legal` : document juridique. Style serif (Georgia) formel.
  · `subject` = Domaine juridique (ex: « Droit OHADA », « Droit civil »)
  · `level` = Juridiction (ex: « Cour d'appel Yaoundé », « TGI »)
  · Footer disclaimer figé « Document d'information — consulter un avocat ».

- `medicine` : document médical. **SAFETY-CRITICAL**. Style sobre.
  · `subject` = Spécialité (ex: « Cardiologie », « Pédiatrie »)
  · `level` = Établissement (ex: « Hôpital Général Yaoundé »)
  · **Disclaimer urgence EN TÊTE body** (bloc rouge gras) avec numéros
    Cameroun 117/118/119 + 112 international.
  · Footer disclaimer figé « NEXYA AI ne pose pas de diagnostic ».

V2+ (C4.7d) ajoutera : watermark NEXYA branding + C2PA AI Act.
"""

DocumentFormat = Literal["pdf", "docx"]
"""Format de sortie disponible (C4.7a + C4.7b).

- `pdf` : pipeline WeasyPrint + pikepdf (C4.7a, livré 2026-05-30)
- `docx` : pipeline python-docx natif Word (C4.7b, livré 2026-05-31)

Les deux templates `school` / `minimal` sont disponibles dans les deux
formats (4 combinaisons : pdf+school, pdf+minimal, docx+school, docx+minimal).

V2 (C4.7c+) ajoutera 3 templates supplémentaires (sciences/legal/medicine).
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
    remove_watermark: bool = Field(
        default=False,
        description=(
            "Retirer le watermark NEXYA (logo bleu bottom-right PDF + footer "
            "DOCX). **Pro only** — Free qui tente `True` → 403 PLAN_REQUIRED. "
            "Pattern aligné `/image/generate` E4. C4.7d."
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

    # ── C4.7d : Watermark + C2PA enrichissement ────────────────
    watermark_applied: bool = Field(
        default=False,
        description=(
            "True si le watermark NEXYA visuel a été appliqué (logo PDF "
            "bottom-right ou footer DOCX). False si remove_watermark=True, "
            "kill-switch off, ou fail-safe sur erreur Pillow/WeasyPrint."
        ),
    )
    watermark_version: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Version du watermark appliqué (ex: `v1-doc-pdf-docx-2026-05`). "
            "Null si applied=False. Permet de tracer l'historique sans "
            "casser les anciens documents si on change de logo plus tard."
        ),
    )
    c2pa_applied: bool = Field(
        default=False,
        description=(
            "True si le manifest C2PA signé cryptographiquement a été "
            "embarqué dans les métadonnées du document (PDF uniquement V1, "
            "DOCX différé V2 — c2pa-rs ne supporte pas OOXML natif). "
            "Conformité AI Act UE août 2026."
        ),
    )
    c2pa_manifest_id: str | None = Field(
        default=None,
        max_length=128,
        description=(
            "Identifiant du manifest C2PA embarqué. Null si applied=False. "
            "Utile pour audit + vérification cross-tool via Content Credentials "
            "Adobe https://contentcredentials.org/verify."
        ),
    )
    c2pa_skip_reason: str | None = Field(
        default=None,
        max_length=64,
        description=(
            "Raison du skip C2PA si applied=False (informatif audit). "
            "Valeurs : `unsupported_format_docx`, `disabled_by_killswitch`, "
            "`sign_error`, `c2pa_lib_unavailable`. Null si applied=True."
        ),
    )


# ── C4.12 : génération asynchrone (docs lourds) ──────────────────────

DocumentJobStatus = Literal["queued", "processing", "done", "failed"]
"""Cycle de vie d'un job de génération asynchrone (C4.12).

- `queued`     : row créée par le router, job enqueué sur arq.
- `processing` : worker a pris le job, rendu en cours.
- `done`       : document généré + sauvé en Library, push FCM envoyé.
- `failed`     : render/storage KO, push FCM d'échec envoyé.
"""


class DocumentGenerateAcceptedResponse(BaseModel):
    """Réponse **202 Accepted** de `POST /generate/document` (chemin async C4.12).

    Renvoyée à la place de `DocumentGenerateResponse` quand le document est
    jugé lourd (`len(markdown_source) > documents_generator_async_threshold_chars`).
    Le rendu est déporté sur le worker arq ; le client est prévenu via push
    FCM « 📄 doc prêt » + deep link vers la conversation.

    Le client peut poller `GET /generate/document/jobs/{job_id}` en filet de
    secours si le push est manqué (réseau 2G/3G, app killed).
    """

    job_id: UUID = Field(
        ...,
        description="UUID du job de génération asynchrone (à poller).",
    )
    status: Literal["processing"] = Field(
        default="processing",
        description="Statut initial exposé au client (toujours 'processing').",
    )
    conversation_id: UUID = Field(
        ...,
        description="UUID de la conversation source (deep link de la notif).",
    )
    message_id: UUID = Field(
        ...,
        description="UUID du message source.",
    )
    format: DocumentFormat = Field(
        ...,
        description="Format demandé (pdf | docx).",
    )


class DocumentJobResponse(BaseModel):
    """Réponse de `GET /generate/document/jobs/{job_id}` (polling C4.12).

    Reflète l'état courant du job. Quand `status='done'`, les champs résultat
    (`library_id`, `download_url` presigné frais, `filename`, `pages`, ...)
    sont peuplés. Quand `status='failed'`, `error_code` est renseigné.
    """

    model_config = ConfigDict(from_attributes=True)

    job_id: UUID = Field(..., description="UUID du job.")
    status: DocumentJobStatus = Field(..., description="Statut courant du job.")
    format: DocumentFormat = Field(..., description="Format demandé.")
    template: DocumentTemplate = Field(..., description="Template demandé.")

    # ── Résultat (peuplé si status='done') ─────────────────────────
    library_id: UUID | None = Field(
        default=None,
        description="UUID de l'item Library créé (null tant que pas done).",
    )
    download_url: str | None = Field(
        default=None,
        description="Presigned URL MinIO FRAÎCHE TTL 30 min (régénérée à chaque poll). Null si pas done.",
    )
    filename: str | None = Field(
        default=None,
        max_length=255,
        description="Nom de fichier du document (null si pas done).",
    )
    pages: int | None = Field(
        default=None,
        ge=0,
        description="Nombre de pages rendues (null si pas done).",
    )
    size_bytes: int | None = Field(
        default=None,
        ge=0,
        description="Taille du document en bytes (null si pas done).",
    )
    truncated: bool | None = Field(
        default=None,
        description="True si tronqué au cap pages (null si pas done).",
    )

    # ── Erreur (peuplée si status='failed') ────────────────────────
    error_code: str | None = Field(
        default=None,
        max_length=64,
        description="Code d'erreur si status='failed' (ex: DOCUMENT_RENDER_FAILED).",
    )

    created_at: datetime = Field(..., description="ISO datetime UTC de création du job.")
    completed_at: datetime | None = Field(
        default=None,
        description="ISO datetime UTC de fin (done ou failed). Null si en cours.",
    )
