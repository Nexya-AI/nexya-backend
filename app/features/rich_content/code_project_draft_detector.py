"""
Détecteur de brouillon projet code multi-fichiers (C4.6).

Cas d'usage majeurs :
  - « écris-moi une API FastAPI complète pour gérer des tâches :
     main.py + routes.py + models.py + requirements.txt »
  - « génère un projet Flutter avec pubspec.yaml + main.dart +
     widgets pour login »
  - « code-moi un programme Node.js qui parse un CSV : index.js +
     parser.js + package.json »

L'IA répond avec 2+ blocs ```language\n...\n``` markdown, chacun
nommé explicitement (filename sur la ligne précédente OU en
markdown bold OU en commentaire inline OU déductible).

Stratégie de détection : **body-driven STRICT + intent BONUS**.
- 2+ blocs nommés explicitement → flag (cas dominant).
- 2+ blocs sans nom (fallback `main.{ext}`) → flag UNIQUEMENT si
  intent fort présent (« écris une API complète », « build me a
  full project »). Sinon → Code File capture le 1er.

Conditions cumulatives pour activer `code_project_draft` :
  1. assistant_text contient ≥ 2 blocs ```language\n...\n```.
  2. Chaque bloc fait ≥ 10 chars de content (plus permissif que
     Code File car les `requirements.txt`/`pubspec.yaml` peuvent
     être courts).
  3. Au moins 50 % des fichiers ont un filename explicite (strat
     a/b/c de `_extract_filename`) OU intent fort présent.
  4. Cap max 50 fichiers (au-delà → tronque à 50 + log warning).
  5. Filenames uniques (dédup post-extraction, cas LLM répété).

Helper `_infer_project_type(files)` heuristique :
  - `package.json` présent → `nodejs`
  - `pyproject.toml` OU `requirements.txt` → `python`
  - `pubspec.yaml` → `flutter`
  - `Cargo.toml` → `rust`
  - `go.mod` → `go`
  - `pom.xml` OU `build.gradle` → `java`
  - `Gemfile` → `ruby`
  - Sinon → None

Aucun appel LLM, aucun I/O, aucun side-effect — module pur synchrone.
"""

from __future__ import annotations

import re
from typing import Pattern

from app.features.rich_content.code_file_draft_detector import (
    _CODE_BLOCK_RE,
    _extract_filename,
)
from app.features.rich_content.schemas import RichContentPayload

# ── Constantes ────────────────────────────────────────────────────────

# Cap min content/fichier (plus permissif que Code File 30 chars car
# les manifests `requirements.txt`/`pubspec.yaml` peuvent faire ~10 chars).
_CODE_PROJECT_FILE_CONTENT_MIN_CHARS = 10

# Cap max fichiers — au-delà, on tronque à 50 + log warning.
_CODE_PROJECT_FILES_MAX = 50

# Cap min fichiers — sinon Code File capture le bloc isolé.
_CODE_PROJECT_FILES_MIN = 2

# Ratio min de fichiers avec filename EXPLICITE (strat a/b/c) pour
# accepter un projet sans intent fort. Sinon le projet est rejeté
# pour éviter les faux positifs (2 blocs Python orphelins ≠ projet).
_EXPLICIT_FILENAME_RATIO_MIN = 0.5


# ── Intent classifier (BONUS — pour cas sans filenames explicites) ────

_INTENT_PATTERNS_FR: tuple[Pattern[str], ...] = (
    re.compile(
        r"\b(?:écris|ecris|écrire|ecrire|génère|genere|crée|cree|"
        r"code-moi|code\s+moi|fais-moi|fais\s+moi|prépare|prepare|"
        r"développe|developpe|monte|construis)\b[^.\n]{0,80}?"
        r"\b(?:api|application|app\s+(?:complète|complete|full)|"
        r"projet(?:\s+complet)?|application\s+(?:complète|complete|full)|"
        r"site\s+(?:web|complet)|backend|frontend|full[\s-]?stack|"
        r"microservice|programme(?:\s+complet)?|script\s+complet)\b",
        re.IGNORECASE,
    ),
    # « API complète FastAPI » / « projet Flutter complet »
    re.compile(
        r"\b(?:api|projet|application|app)\b[^.\n]{0,40}?"
        r"\b(?:complète|complete|complet|full|full[\s-]?stack)\b",
        re.IGNORECASE,
    ),
)

_INTENT_PATTERNS_EN: tuple[Pattern[str], ...] = (
    re.compile(
        r"\b(?:write|build|create|generate|develop|code|make)\b[^.\n]{0,80}?"
        r"\b(?:(?:full|complete|whole|entire)\s+(?:project|app|application|api|backend|frontend|stack)|"
        r"full[\s-]?stack(?:\s+(?:app|application|project))?|"
        r"microservice|web\s+(?:app|application|site))\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(?:project|app|application|api)\b[^.\n]{0,40}?"
        r"\b(?:complete|full|whole|entire|full[\s-]?stack)\b",
        re.IGNORECASE,
    ),
    # « complete API for ... » / « full project for ... » — qualificatif
    # devant le type (au lieu d'après).
    re.compile(
        r"\b(?:complete|full|whole|entire)\s+(?:project|app|application|api|backend|frontend|stack|"
        r"microservice|web\s+(?:app|application|site))\b",
        re.IGNORECASE,
    ),
)


def detect_code_project_intent(user_message: str) -> bool:
    """True si l'user demande un projet code complet (multi-fichiers).

    Conservateur strict — exige un verbe d'action + qualificatif
    « complet/full/full-stack » OU mention explicite d'un type de
    projet (API, application web, microservice).
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return False
    for pattern in _INTENT_PATTERNS_FR + _INTENT_PATTERNS_EN:
        if pattern.search(user_message):
            return True
    return False


# ── Project type inference ────────────────────────────────────────────


def _infer_project_type(filenames: list[str]) -> str | None:
    """Devine le type de projet depuis la liste des filenames.

    Retourne un slug court (`python`, `nodejs`, `flutter`, `rust`, `go`,
    `java`, `ruby`) ou None si rien de reconnu.

    L'ordre des checks importe : si plusieurs marqueurs (ex: projet
    full-stack avec `package.json` ET `pyproject.toml`), on prend le
    PREMIER trouvé (priorité backend Python si présent).
    """
    if not filenames:
        return None

    # Normalise tous les filenames en basenames lowercase pour comparaison.
    # (cas `src/server/package.json` → "package.json")
    basenames = {f.rsplit("/", 1)[-1].lower() for f in filenames}

    # Ordre de priorité : Python avant Node si les 2 présents
    # (cas full-stack Django/Flask + React → on tag "python" backend).
    if "pyproject.toml" in basenames or "requirements.txt" in basenames:
        return "python"
    if "pubspec.yaml" in basenames or "pubspec.yml" in basenames:
        return "flutter"
    if "cargo.toml" in basenames:
        return "rust"
    if "go.mod" in basenames:
        return "go"
    if "package.json" in basenames:
        return "nodejs"
    if "pom.xml" in basenames or "build.gradle" in basenames or "build.gradle.kts" in basenames:
        return "java"
    if "gemfile" in basenames:
        return "ruby"
    if "composer.json" in basenames:
        return "php"
    return None


# ── Project name inference ────────────────────────────────────────────

# Patterns pour deviner le nom du projet depuis le user_message.
# Ex: « écris-moi une API FastAPI complète pour gérer des tâches »
# → on capture « FastAPI » ou « API tâches » comme nom.
_PROJECT_NAME_HINTS_FR: tuple[Pattern[str], ...] = (
    # « une/un X » immediatement après un verbe d'action
    re.compile(
        r"\b(?:écris|ecris|génère|genere|crée|cree|code-moi|fais-moi|"
        r"développe|developpe|monte|construis|prépare|prepare)\b[^.\n]*?"
        r"\b(?:un|une)\s+([\w\s\-]{1,40}?)(?:\s+(?:complète|complete|complet|full|qui|pour|avec|en))",
        re.IGNORECASE,
    ),
)

_PROJECT_NAME_HINTS_EN: tuple[Pattern[str], ...] = (
    re.compile(
        r"\b(?:write|build|create|generate|develop|code|make)\b[^.\n]*?"
        r"\b(?:a|an)\s+([\w\s\-]{1,40}?)(?:\s+(?:full|complete|that|to|with|in))",
        re.IGNORECASE,
    ),
)


def _infer_project_name(user_message: str, project_type: str | None) -> str:
    """Devine un nom de projet plausible depuis le user_message.

    Stratégies :
      1. Match regex « écris-moi une <X> ... » / « build me an <X> ... »
         puis nettoie + cap 100 chars.
      2. Fallback : si project_type connu, utilise « <Type> Project »
         (ex: « Python Project », « Flutter Project »).
      3. Fallback ultime : « Code Project ».
    """
    if isinstance(user_message, str) and user_message.strip():
        for pattern in _PROJECT_NAME_HINTS_FR + _PROJECT_NAME_HINTS_EN:
            match = pattern.search(user_message)
            if match:
                raw = match.group(1).strip()
                # Title case pour la beauté, cap 100 chars.
                name = raw.title()[:100]
                if name:
                    return name

    # Fallback type
    if project_type:
        return f"{project_type.title()} Project"

    return "Code Project"


# ── Détecteur ──────────────────────────────────────────────────────────


def _extract_project_files(assistant_text: str) -> list[dict]:
    """Extrait tous les fichiers d'un projet code depuis le markdown.

    Returns:
        Liste de dicts `{filename, content, language, has_explicit_name: bool}`
        post-extraction (avant validation Pydantic).

    Le flag `has_explicit_name` (interne) indique si le filename vient
    des stratégies a/b/c (explicite) ou (d) (fallback main.{ext}).
    Utilisé pour le ratio dans `detect_rich_content_code_project`.
    """
    files: list[dict] = []

    # On itère via finditer pour avoir les positions des matches
    # (nécessaire pour extraire le preceding_text de chaque bloc).
    last_block_end = 0
    for match in _CODE_BLOCK_RE.finditer(assistant_text):
        raw_language = match.group(1)
        raw_content = match.group(2)

        # Cap min content (plus permissif que Code File : 10 chars
        # car `requirements.txt`/manifest peuvent être courts).
        if len(raw_content) < _CODE_PROJECT_FILE_CONTENT_MIN_CHARS:
            continue

        # Extract preceding_text = depuis la fin du bloc précédent (ou
        # début du texte) jusqu'au début de ce bloc.
        block_start = match.start()
        preceding_text = assistant_text[last_block_end:block_start]
        # Cap 500 chars max pour _extract_filename (mémoire).
        preceding_text = preceding_text[-500:] if len(preceding_text) > 500 else preceding_text

        language = raw_language.strip().lower() or "plaintext"

        # Détecte si le filename vient des strats a/b/c (explicite) ou (d).
        # On regarde si une des strats a/b/c match avant d'appeler le helper.
        has_explicit_name = _has_explicit_filename(
            preceding_text=preceding_text,
            block_content=raw_content,
        )

        filename = _extract_filename(
            block_content=raw_content,
            block_language=language,
            preceding_text=preceding_text,
        )

        files.append({
            "filename": filename,
            "content": raw_content,
            "language": language,
            "_has_explicit_name": has_explicit_name,
        })

        last_block_end = match.end()

    return files


def _has_explicit_filename(*, preceding_text: str, block_content: str) -> bool:
    """Indique si _extract_filename va utiliser strat a/b/c (explicite)
    ou (d) (fallback main.{ext}).

    Logique : reproduit la même séquence de checks que `_extract_filename`
    SANS appeler le helper (pour éviter l'allocation du return). Si une
    des 3 stratégies explicites match → True.
    """
    # Strat (a) : ligne précédente immédiate.
    lines = [ln.strip() for ln in preceding_text.rstrip().split("\n") if ln.strip()]
    if lines:
        last_line = lines[-1]
        last_line_clean = re.sub(r"^[*_]+|[*_]+$", "", last_line).strip()
        last_line_clean = last_line_clean.rstrip(":").strip()
        if re.match(r"^[\w/.\-]+\.\w+$", last_line_clean) and len(last_line_clean) <= 200:
            return True

    # Strat (b) : markdown bold dans les 200 chars précédents.
    tail = preceding_text[-200:] if len(preceding_text) > 200 else preceding_text
    if re.search(r"(?:\*\*|__)([\w/.\-]+\.\w+)(?:\*\*|__)", tail):
        return True

    # Strat (c) : commentaire inline en tête du bloc.
    block_head = block_content[:500]
    inline_patterns = (
        r"^\s*#\s*([\w/.\-]+\.\w+)\s*$",
        r"^\s*//\s*([\w/.\-]+\.\w+)\s*$",
        r"^\s*<!--\s*([\w/.\-]+\.\w+)\s*-->\s*$",
        r"^\s*/\*\s*([\w/.\-]+\.\w+)\s*\*/\s*$",
    )
    for pat in inline_patterns:
        if re.search(pat, block_head, re.MULTILINE):
            return True

    return False


def detect_rich_content_code_project(
    user_message: str,
    assistant_text: str,
) -> dict | None:
    """Détecte un brouillon projet code multi-fichiers dans la réponse IA.

    Retourne un dict conforme à `RichContentPayload.code_project()`
    (sérialisé via `.model_dump()`) ou None si pas de match.

    Conditions strictes :
      1. `assistant_text` non-vide.
      2. ≥ 2 blocs de code dans le texte (sinon Code File tentera).
      3. ≥ 50 % des fichiers ont un filename EXPLICITE (strat a/b/c)
         OU `detect_code_project_intent(user_message)` retourne True.
      4. Cap 50 fichiers max (tronque + log warning).
      5. Filenames uniques (dédup post-extraction).

    Fail-safe absolu : toute exception Pydantic → return None (les
    blocs restent visibles dans le markdown brut côté Flutter).
    """
    if not isinstance(assistant_text, str) or not assistant_text.strip():
        return None

    # Extraction des fichiers (filtre cap min content déjà appliqué).
    files = _extract_project_files(assistant_text)

    # Cap min 2 fichiers — sinon Code File capture l'éventuel bloc isolé.
    if len(files) < _CODE_PROJECT_FILES_MIN:
        return None

    # Cap max 50 fichiers — tronque au-delà.
    if len(files) > _CODE_PROJECT_FILES_MAX:
        files = files[:_CODE_PROJECT_FILES_MAX]

    # Dédup filenames : si l'IA répète le même filename (cas rare,
    # bug LLM), on garde le DERNIER (l'IA aurait peut-être voulu
    # corriger le précédent).
    deduped: dict[str, dict] = {}
    for f in files:
        deduped[f["filename"]] = f
    files = list(deduped.values())

    # Cap min 2 fichiers re-checked après dédup (si l'IA a généré
    # 2 blocs avec le même filename, on retombe sur 1 fichier unique
    # → Code File capture).
    if len(files) < _CODE_PROJECT_FILES_MIN:
        return None

    # Check ratio filenames explicites OU intent fort.
    explicit_count = sum(1 for f in files if f["_has_explicit_name"])
    explicit_ratio = explicit_count / len(files)
    has_intent = detect_code_project_intent(user_message)

    if explicit_ratio < _EXPLICIT_FILENAME_RATIO_MIN and not has_intent:
        # Pas assez de filenames explicites + pas d'intent → faux positif
        # probable (2 blocs Python orphelins ≠ projet). Skip.
        return None

    # Infer project_type depuis les filenames.
    filenames = [f["filename"] for f in files]
    project_type = _infer_project_type(filenames)

    # Infer project_name depuis le user_message + fallback type.
    project_name = _infer_project_name(user_message, project_type)

    # Strip le flag interne `_has_explicit_name` avant Pydantic.
    files_clean = [
        {"filename": f["filename"], "content": f["content"], "language": f["language"]}
        for f in files
    ]

    # Construit le payload via factory Pydantic (validation stricte :
    # filename path-safe + caps + dédup).
    try:
        payload = RichContentPayload.code_project(
            project_name=project_name,
            files=files_clean,
            description=None,
            project_type=project_type,
        )
    except Exception:  # noqa: BLE001
        # ValidationError (filename path-unsafe, cap dépassé, etc.) →
        # carte n'apparaît pas, blocs restent visibles dans le markdown.
        return None

    return payload.model_dump()
