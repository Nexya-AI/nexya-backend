"""
Schémas Pydantic — Rich content payload (C4.4 + C4.5 + C4.6).

Stocké dans `messages.metadata_json.rich_content` (JSONB déjà livré
planner-from-chat LOT B 2026-05-23). Discriminé par `kind`.

V1 (C4.4) — 2 kinds : email_draft + whatsapp_draft.
V2 (C4.5) — 4 nouveaux kinds : sms_draft + linkedin_post_draft +
            tweet_draft + document_draft.
V3 (C4.6) — 2 nouveaux kinds : code_file_draft + code_project_draft.

Caps stricts par kind :
  EMAIL (C4.4) :
  - `subject` ≤ 300 chars (limite raisonnable Gmail/Outlook UI)
  - `body` ≤ 10 000 chars
  - `to` ≤ 320 chars (RFC 5321 longueur max email)

  WHATSAPP (C4.4) :
  - `phone` ≤ 20 chars (E.164 max 15 + format)
  - `body` ≤ 10 000 chars

  SMS (C4.5) :
  - `phone` ≤ 20 chars (E.164 max 15 + format)
  - `body` ≤ 1 600 chars (cap dur — 10 segments SMS de 160 chars,
    au-delà les opérateurs basculent en MMS ou tronquent silencieusement).

  LINKEDIN POST (C4.5) :
  - `body` ≤ 3 000 chars (limite officielle LinkedIn Posts 2026,
    cf. https://www.linkedin.com/help/linkedin/answer/a566188).

  TWEET (C4.5) :
  - `body` ≤ 280 chars (limite officielle Twitter/X 2026,
    cap dur côté backend pour économiser le round-trip si le LLM
    sur-génère, le client refuse aussi côté UI compose).

  DOCUMENT (C4.5) :
  - `title` ≤ 300 chars (titre du document, optionnel)
  - `body` ≤ 50 000 chars (~10 pages PDF A4 dense, cap dur anti
    explosion taille fichier généré côté Flutter `printing` lib)
  - `recipient` ≤ 200 chars (destinataire formel optionnel,
    p.ex. « Madame Le Maire de Yaoundé », pour entête lettre)

  CODE FILE (C4.6) :
  - `filename` ≤ 200 chars (path-safe strict — anti path traversal)
  - `content` ≤ 100 000 chars (~ 3000 lignes de code, cap dur anti
    jank rendu `NxCodeBlock` flutter_highlight + anti explosion RAM)
  - `language` ≤ 32 chars (slug `python`/`dart`/`typescript`/...)
  - `description` ≤ 500 chars (optionnel — courte description du
    fichier pour affichage UI tooltip)

  CODE PROJECT (C4.6) :
  - `project_name` ≤ 100 chars
  - `description` ≤ 1000 chars (optionnel — README court UI)
  - `files` 2-50 fichiers (cap min 2 sinon Code File est suffisant,
    cap max 50 Africa-first anti-jank tree view + anti explosion
    .zip taille MinIO)
  - `project_type` ≤ 32 chars (optionnel — inféré backend
    `python`/`nodejs`/`fastapi`/`flutter`/`rust` etc.)
  - Total cumulé `sum(len(f.content))` ≤ 5 MB texte brut → ~1-2 MB
    zippé Africa-first cap dur, friction partage WhatsApp/Email
    acceptable.

Le schéma `RichContentPayload` est exposé au client via
`MessageResponse.metadata_json["rich_content"]` (typage `dict` côté
Pydantic pour ne pas exploser le contrat). Le client Flutter parse en
fail-safe via `DraftPayload.tryFromMetadata`.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

RichContentKind = Literal[
    "email_draft",
    "whatsapp_draft",
    "sms_draft",
    "linkedin_post_draft",
    "tweet_draft",
    "document_draft",
    "code_file_draft",
    "code_project_draft",
]


# ══════════════════════════════════════════════════════════════════════
# C4.6 — Helpers sécurité partagés Code File + Code Project
# ══════════════════════════════════════════════════════════════════════

# Sécurité critique anti path traversal sur `zipfile.writestr` côté
# CodeProjectService.build_zip (cf. service.py). Un attaquant qui poste
# `filename="../../../etc/passwd"` construirait un .zip qui, à
# l'extraction côté user (unzip auto Finder/Explorer), écrirait le
# fichier HORS du dossier cible (overwrite système).
#
# Le validator REJETTE 422 toutes ces formes :
#   - `../`, `..\\`, `..%2F` (path traversal classique)
#   - `~` au début (home expansion shell)
#   - `/foo` (path absolu Unix) ou `\\foo` (path absolu Windows)
#   - `C:\\` ou tout `<lettre>:\\` (path absolu Windows)
#   - chars de contrôle ASCII < 0x20 (anti smuggling binaire)
#
# AUTORISE explicitement : `/` ET `\\` au MILIEU du path car les
# projets code multi-fichiers ont légitimement des sous-dossiers
# (`tests/test_main.py`, `routes/users.py`, etc.). Le `/` simple
# est normalisé en séparateur Unix côté `zipfile.writestr`.

_FILENAME_FORBIDDEN_PATTERNS = (
    re.compile(r"\.\."),  # path traversal (anywhere in path)
    re.compile(r"^~"),  # home expansion
    re.compile(r"^[/\\]"),  # path absolu Unix ou Windows
    re.compile(r"^[A-Za-z]:[/\\]"),  # path absolu Windows (C:\...)
    re.compile(r"[\x00-\x1f]"),  # chars de contrôle ASCII
)


def _validate_filename_path_safe(filename: str) -> str:
    """Valide qu'un filename est path-safe pour `zipfile.writestr`.

    Lève ValueError si filename contient :
      - `..` (path traversal anywhere)
      - `~` au début (home expansion)
      - path absolu (`/foo`, `\\foo`, `C:\\foo`)
      - chars de contrôle ASCII < 0x20

    Autorise `/` et `\\` au milieu du path (sous-dossiers projets).
    Normalise le path en séparateurs Unix au passage (ZIP standard).

    Strip espaces début/fin (mais rejette si vide post-strip).
    """
    cleaned = filename.strip()
    if not cleaned:
        raise ValueError("filename ne peut pas être vide ou whitespace-only.")
    # Normalise séparateurs Windows → Unix (zipfile attend `/`)
    cleaned = cleaned.replace("\\", "/")
    for pattern in _FILENAME_FORBIDDEN_PATTERNS:
        if pattern.search(cleaned):
            raise ValueError(
                f"filename contient un motif interdit (path traversal "
                f"ou path absolu) : {filename!r}"
            )
    return cleaned


class EmailDraftData(BaseModel):
    """Payload d'un brouillon d'email.

    `subject` optionnel — certains emails informels n'en ont pas. Le
    client affichera un placeholder dans l'UI si absent.

    `to` optionnel — le LLM ne sait généralement pas à qui l'utilisateur
    veut envoyer le mail. Le client laisse le champ vide pour que l'user
    le complète après tap « ✉ Envoyer ».
    """

    subject: str | None = Field(default=None, max_length=300)
    body: str = Field(min_length=1, max_length=10_000)
    to: str | None = Field(default=None, max_length=320)

    @field_validator("subject", "to")
    @classmethod
    def _strip_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class WhatsAppDraftData(BaseModel):
    """Payload d'un brouillon WhatsApp.

    `phone` optionnel — comme `to` côté email, le LLM ne sait pas
    à qui envoyer. L'user complète après tap « 💬 Ouvrir WhatsApp ».
    Format attendu : E.164 sans préfixe `+` (WhatsApp accepte les deux,
    le client Flutter normalise).
    """

    phone: str | None = Field(default=None, max_length=20)
    body: str = Field(min_length=1, max_length=10_000)

    @field_validator("phone")
    @classmethod
    def _strip_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class SmsDraftData(BaseModel):
    """Payload d'un brouillon SMS (C4.5).

    `phone` optionnel — pattern aligné WhatsApp. L'user complète après
    tap « 📱 Envoyer SMS ». Format attendu : E.164.

    `body` capé 1600 chars (= 10 segments SMS de 160 chars). Au-delà
    les opérateurs Afrique francophone (Orange/MTN/Wave) basculent en
    MMS coûteux ou tronquent silencieusement à 160 chars — on bloque
    côté backend pour épargner ce piège à l'user.
    """

    phone: str | None = Field(default=None, max_length=20)
    body: str = Field(min_length=1, max_length=1_600)

    @field_validator("phone")
    @classmethod
    def _strip_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class LinkedInPostDraftData(BaseModel):
    """Payload d'un brouillon LinkedIn post (C4.5).

    Pas de destinataire (un post LinkedIn est publié sur le mur de
    l'utilisateur, pas envoyé à un contact). L'user édite le body
    dans la NxDraftCard puis tape « 💼 Ouvrir LinkedIn » qui ouvre
    le composer LinkedIn natif avec le texte pré-rempli (deep link
    `linkedin://shareArticle?text=` ou fallback https://linkedin.com/feed/?shareActive=true).

    `body` capé 3000 chars (limite officielle LinkedIn 2026, cf.
    `https://www.linkedin.com/help/linkedin/answer/a566188`).
    """

    body: str = Field(min_length=1, max_length=3_000)


class TweetDraftData(BaseModel):
    """Payload d'un brouillon Tweet/X post (C4.5).

    Pas de destinataire (un tweet est publié sur le profil).
    L'user tape « 🐦 Ouvrir X » qui ouvre le composer X natif avec
    le texte pré-rempli (deep link `twitter://post?message=` ou fallback
    https://twitter.com/intent/tweet?text=).

    `body` capé 280 chars (limite officielle Twitter/X 2026, cap dur
    côté backend pour économiser le round-trip si le LLM sur-génère).
    Le client refuse aussi côté UI compose.

    Note : la limite étendue 25 000 chars pour les abonnés X Premium
    n'est PAS supportée V1 (cas marginal, le compose natif X coupera
    proprement à 280 si l'user n'est pas Premium).
    """

    body: str = Field(min_length=1, max_length=280)


class DocumentDraftData(BaseModel):
    """Payload d'un brouillon de document long (C4.5).

    Cas d'usage : « rédige-moi une lettre formelle au maire de Yaoundé
    pour demander un acte de naissance », « rédige un rapport de
    réunion », « rédige un compte-rendu de stage », « génère un cours
    sur les boucles for en Python ».

    L'user tape « 📄 Générer PDF » qui appelle `printing` lib côté
    Flutter et produit un PDF natif partageable via share_plus.

    `title` optionnel — sert d'entête PDF + filename (sanitizé côté
    Flutter). Si absent, fallback `Document NEXYA - YYYY-MM-DD.pdf`.

    `recipient` optionnel — pour les lettres formelles, sert d'entête
    formelle « À l'attention de <recipient> ». Pour les cours/rapports
    sans destinataire, laisser None.

    `body` capé 50 000 chars (~10 pages PDF A4 dense). Cap dur anti
    explosion taille fichier généré (un PDF de 50k chars fait ~500 KB
    raisonnable Africa-first 2G/3G, au-delà = friction partage).
    """

    title: str | None = Field(default=None, max_length=300)
    body: str = Field(min_length=1, max_length=50_000)
    recipient: str | None = Field(default=None, max_length=200)

    @field_validator("title", "recipient")
    @classmethod
    def _strip_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class CodeFileDraftData(BaseModel):
    """Payload d'un brouillon UN SEUL fichier de code (C4.6).

    Cas d'usage : « écris-moi un script Python qui calcule fibonacci »,
    « génère une classe Dart Widget pour mon écran login », « code-moi
    une fonction TypeScript de validation email ». L'IA répond avec
    UN SEUL bloc ```language\n...\n``` markdown.

    L'user tape « 📋 Copier » (clipboard), « 💾 Sauvegarder dans Library »
    (POST /library type=code), « 🔗 Partager » (share_plus avec
    `getTemporaryDirectory()` + XFile).

    `filename` (1-200 chars) : nom du fichier déduit côté backend par
    le détecteur (4 stratégies fallback : ligne précédente / markdown
    bold / commentaire / fallback `main.{ext}`). Sécurité critique
    path-safe (anti path traversal sur futur usage zipfile partagé).

    `content` (1-100 000 chars) : code source brut sans les markers
    markdown ```. Cap dur Africa-first anti-jank rendu `NxCodeBlock`
    (le cap UI Flutter est 50 000 chars → fallback monospace plat
    au-delà).

    `language` (1-32 chars) : slug highlight.js (`python`/`dart`/
    `typescript`/`javascript`/`java`/`go`/`rust`/etc.). Si langue
    inconnue → côté Flutter fallback `plaintext` monospace plat.

    `description` (max 500 chars, optionnel) : description courte
    affichée en tooltip UI (ex: « Implémentation récursive »).
    """

    filename: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)
    language: str = Field(min_length=1, max_length=32)
    description: str | None = Field(default=None, max_length=500)

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, v: str) -> str:
        return _validate_filename_path_safe(v)

    @field_validator("language")
    @classmethod
    def _normalize_language(cls, v: str) -> str:
        # Slug minuscule, alphanumérique + `_` + `-` + `+` (ex: `c++`).
        cleaned = v.strip().lower()
        if not cleaned:
            raise ValueError("language ne peut pas être vide.")
        if not re.fullmatch(r"[a-z0-9+_\-]+", cleaned):
            raise ValueError(
                f"language doit être alphanumérique + `_-+` "
                f"(reçu : {v!r})"
            )
        return cleaned

    @field_validator("description")
    @classmethod
    def _strip_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None


class CodeProjectFileItem(BaseModel):
    """Item fichier dans un Code Project (C4.6).

    PAS un sous-type de `RichContentPayload` — c'est juste un data
    holder pour `CodeProjectDraftData.files`. Validators alignés
    `CodeFileDraftData` (filename path-safe, content cap, language slug).

    Pas de `description` au niveau item (la description vit côté
    `CodeProjectDraftData` au niveau projet entier).
    """

    filename: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=100_000)
    language: str = Field(min_length=1, max_length=32)

    @field_validator("filename")
    @classmethod
    def _validate_filename(cls, v: str) -> str:
        return _validate_filename_path_safe(v)

    @field_validator("language")
    @classmethod
    def _normalize_language(cls, v: str) -> str:
        cleaned = v.strip().lower()
        if not cleaned:
            raise ValueError("language ne peut pas être vide.")
        if not re.fullmatch(r"[a-z0-9+_\-]+", cleaned):
            raise ValueError(
                f"language doit être alphanumérique + `_-+` "
                f"(reçu : {v!r})"
            )
        return cleaned


class CodeProjectDraftData(BaseModel):
    """Payload d'un projet code multi-fichiers (C4.6).

    Cas d'usage : « écris-moi une API FastAPI complète pour gérer
    des tâches : main.py avec FastAPI, routes.py avec /tasks GET POST,
    models.py avec Pydantic Task, requirements.txt ». L'IA répond
    avec 3+ blocs ```language\n...\n``` nommés explicitement.

    L'user tape « ⬇ Télécharger .zip » qui appelle
    `POST /code-projects/build-zip` (route C4.6) qui construit
    l'archive en mémoire via `zipfile.ZipFile(BytesIO())` + presigned
    URL MinIO TTL 24h → Dio download Flutter → `share_plus`.

    `project_name` (1-100 chars) : inféré depuis `user_message` par
    le détecteur (regex `(api|application|projet) (\\w+)`) sinon
    fallback `"Code Project"`.

    `description` (max 1000 chars, optionnel) : courte description
    affichée dans la carte UI + ajoutée au README.md généré dans le
    .zip.

    `files` (2-50 fichiers) : la liste des fichiers du projet.
    - Cap min 2 : sinon `code_file_draft` capture le bloc isolé
      (UX cohérente — un projet a au moins 2 fichiers).
    - Cap max 50 : Africa-first anti-jank tree view Flutter +
      anti explosion taille .zip.

    `project_type` (max 32 chars, optionnel) : inféré côté backend
    par `_infer_project_type(files)` (heuristique : `package.json`
    → `nodejs`, `pyproject.toml`/`requirements.txt` → `python`,
    `pubspec.yaml` → `flutter`, `Cargo.toml` → `rust`, etc.).
    Sinon `None`.

    Validator transverse `_validate_total_size` : sum(len(f.content))
    ≤ 5 MB texte brut (cap dur Africa-first, ~1-2 MB zippé).
    """

    project_name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    files: list[CodeProjectFileItem] = Field(min_length=2, max_length=50)
    project_type: str | None = Field(default=None, max_length=32)

    @field_validator("description", "project_type")
    @classmethod
    def _strip_empty(cls, v: str | None) -> str | None:
        if v is None:
            return None
        stripped = v.strip()
        return stripped or None

    @field_validator("project_name")
    @classmethod
    def _strip_project_name(cls, v: str) -> str:
        stripped = v.strip()
        if not stripped:
            raise ValueError("project_name ne peut pas être vide.")
        return stripped

    @model_validator(mode="after")
    def _validate_total_size_and_duplicates(self) -> CodeProjectDraftData:
        # 1) Cap dur total : 5 MB texte brut (~1-2 MB zippé Africa-first).
        total_size = sum(len(f.content) for f in self.files)
        if total_size > 5_000_000:
            raise ValueError(
                f"Le projet dépasse le cap dur de 5 MB de texte brut "
                f"(actuel : {total_size:,} chars)."
            )

        # 2) Pas de filenames dupliqués (un projet code ne peut PAS avoir
        #    2 fichiers `main.py` — le 2ᵉ écraserait le 1ᵉʳ dans le .zip
        #    silencieusement, perte de données invisible côté user).
        seen: set[str] = set()
        for f in self.files:
            # Comparaison normalisée Unix (déjà fait dans _validate_filename).
            if f.filename in seen:
                raise ValueError(
                    f"Filename dupliqué dans files : {f.filename!r}. "
                    f"Chaque fichier doit avoir un nom unique."
                )
            seen.add(f.filename)

        return self


class RichContentPayload(BaseModel):
    """Discriminé par `kind`.

    Stocké tel quel dans `messages.metadata_json["rich_content"]`.
    Le `data` est un `dict` côté Pydantic — le caller détecteur produit
    un payload conforme à `XxxDraftData` selon le `kind`, validé au
    moment de la construction via les factory `RichContentPayload.xxx()`.
    """

    kind: RichContentKind
    data: dict

    @classmethod
    def email(
        cls,
        *,
        subject: str | None,
        body: str,
        to: str | None = None,
    ) -> "RichContentPayload":
        """Construit un payload email avec validation Pydantic stricte.

        Lève `ValidationError` si `body` vide ou trop long, `subject`/`to`
        trop longs, etc. Le caller détecteur garantit que ces invariants
        sont respectés AVANT d'appeler ce constructeur.
        """
        data = EmailDraftData(subject=subject, body=body, to=to)
        return cls(kind="email_draft", data=data.model_dump())

    @classmethod
    def whatsapp(
        cls,
        *,
        phone: str | None,
        body: str,
    ) -> "RichContentPayload":
        """Construit un payload WhatsApp avec validation Pydantic stricte."""
        data = WhatsAppDraftData(phone=phone, body=body)
        return cls(kind="whatsapp_draft", data=data.model_dump())

    @classmethod
    def sms(
        cls,
        *,
        phone: str | None,
        body: str,
    ) -> "RichContentPayload":
        """Construit un payload SMS avec validation Pydantic stricte (C4.5).

        Lève `ValidationError` si `body` vide ou > 1600 chars.
        """
        data = SmsDraftData(phone=phone, body=body)
        return cls(kind="sms_draft", data=data.model_dump())

    @classmethod
    def linkedin_post(
        cls,
        *,
        body: str,
    ) -> "RichContentPayload":
        """Construit un payload LinkedIn post avec validation stricte (C4.5).

        Lève `ValidationError` si `body` vide ou > 3000 chars.
        """
        data = LinkedInPostDraftData(body=body)
        return cls(kind="linkedin_post_draft", data=data.model_dump())

    @classmethod
    def tweet(
        cls,
        *,
        body: str,
    ) -> "RichContentPayload":
        """Construit un payload Tweet/X avec validation stricte (C4.5).

        Lève `ValidationError` si `body` vide ou > 280 chars.
        """
        data = TweetDraftData(body=body)
        return cls(kind="tweet_draft", data=data.model_dump())

    @classmethod
    def document(
        cls,
        *,
        title: str | None,
        body: str,
        recipient: str | None = None,
    ) -> "RichContentPayload":
        """Construit un payload document long avec validation stricte (C4.5).

        Lève `ValidationError` si `body` vide ou > 50 000 chars, `title`/
        `recipient` trop longs.
        """
        data = DocumentDraftData(title=title, body=body, recipient=recipient)
        return cls(kind="document_draft", data=data.model_dump())

    @classmethod
    def code_file(
        cls,
        *,
        filename: str,
        content: str,
        language: str,
        description: str | None = None,
    ) -> "RichContentPayload":
        """Construit un payload UN SEUL fichier de code avec validation
        stricte (C4.6).

        Lève `ValidationError` si :
        - `filename` vide, > 200 chars, ou path-unsafe (path traversal
          `../`, path absolu `/foo`, `C:\\`, chars de contrôle).
        - `content` vide ou > 100 000 chars.
        - `language` vide, > 32 chars, ou non-alphanumérique.
        - `description` > 500 chars.
        """
        data = CodeFileDraftData(
            filename=filename,
            content=content,
            language=language,
            description=description,
        )
        return cls(kind="code_file_draft", data=data.model_dump())

    @classmethod
    def code_project(
        cls,
        *,
        project_name: str,
        files: list[dict] | list[CodeProjectFileItem],
        description: str | None = None,
        project_type: str | None = None,
    ) -> "RichContentPayload":
        """Construit un payload projet code multi-fichiers avec validation
        stricte (C4.6).

        `files` accepte une liste de dicts (auto-parse Pydantic) OU une
        liste de `CodeProjectFileItem` instances (déjà validées).

        Lève `ValidationError` si :
        - `project_name` vide ou > 100 chars.
        - `files` < 2 ou > 50 items.
        - Un filename dans `files` est path-unsafe ou dupliqué.
        - `sum(len(f.content) for f in files)` > 5 MB texte brut.
        - `description` > 1000 chars OU `project_type` > 32 chars.
        """
        # Auto-parse les dicts en CodeProjectFileItem si fourni en raw.
        parsed_files: list[CodeProjectFileItem] = [
            f if isinstance(f, CodeProjectFileItem) else CodeProjectFileItem(**f)
            for f in files
        ]
        data = CodeProjectDraftData(
            project_name=project_name,
            description=description,
            files=parsed_files,
            project_type=project_type,
        )
        return cls(kind="code_project_draft", data=data.model_dump())
