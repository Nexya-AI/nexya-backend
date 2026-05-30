"""
Détecteur de brouillon UN SEUL fichier de code (C4.6).

Cas d'usage majeurs :
  - « écris-moi un script Python qui calcule fibonacci jusqu'à n=10 »
  - « génère une fonction Dart pour valider un email »
  - « code-moi une classe TypeScript User avec getters »

L'IA répond avec UN SEUL bloc ```language\n...\n``` markdown.

Stratégie de détection : **body-driven** (pas intent-only).
Contrairement à `whatsapp_draft_detector` / `sms_draft_detector` qui
exigent un intent explicite (« rédige un WhatsApp »), le code file
peut être détecté SANS intent — le simple fait que l'IA génère un
bloc de code suffit pour activer la carte (UX cohérente avec ChatGPT/
Claude qui affichent automatiquement un bouton « Copier » sur tout
bloc de code).

Conditions cumulatives strictes pour activer `code_file_draft` :
  1. assistant_text contient EXACTEMENT 1 bloc ```language\n...\n```
     (0 → pas de code, 2+ → Code Project tentera ou skip).
  2. Le content du bloc fait ≥ 30 chars (un snippet de 10 chars ne
     mérite pas une carte avec 3 boutons).
  3. Le language est connu (whitelist 20+ slugs highlight.js) OU
     vide (fallback `plaintext`, accepté).

Extraction du filename via 4 stratégies fallback :
  (a) ligne précédente matche `^[\\w/.\\-]+\\.(\\w+)$` (filename
      explicite au-dessus du bloc).
  (b) markdown bold `**filename.ext**` dans les 200 chars précédents.
  (c) 1er commentaire du bloc (`# filename.py` Python, `// filename.js`
      JS/Dart, `<!-- filename.html -->` HTML, etc.).
  (d) fallback `main.{ext}` via mapping language → extension.

Si même fallback (d) ne peut pas dériver d'extension (language inconnu),
filename = `code-snippet.txt`.

Aucun appel LLM, aucun I/O, aucun side-effect — module pur synchrone,
testable en isolation. Cap content 100k chars aligné schéma Pydantic.
"""

from __future__ import annotations

import re
from typing import Pattern

from app.features.rich_content.schemas import RichContentPayload

# ── Constantes ────────────────────────────────────────────────────────

# Cap content min pour activer la carte (anti snippet trivial).
_CODE_FILE_CONTENT_MIN_CHARS = 30

# Cap content max aligné `CodeFileDraftData.content` Pydantic.
_CODE_FILE_CONTENT_MAX_CHARS = 100_000


# Regex pour extraire tous les blocs ```language\n...\n```
# - Groupe 1 : language (optionnel, vide si ```\n... sans annotation)
# - Groupe 2 : content (multi-ligne via re.DOTALL)
#
# Le `\w*` accepte les chars alphanumériques + `_` mais PAS `+`/`-`.
# Pour `c++`, `objective-c`, `f#`, le LLM utilise généralement `cpp`,
# `objc`, `fsharp` dans le fence — c'est OK. Si rare cas `c++` exact,
# le détecteur skip le language (matchera "" puis le _normalize_language
# côté Pydantic rejettera) → fallback `code-snippet.txt`.
_CODE_BLOCK_RE: Pattern[str] = re.compile(
    r"```([a-zA-Z0-9_+\-]*)\n(.*?)```",
    re.DOTALL,
)


# Whitelist des slugs language reconnus côté Flutter NxCodeBlock C4.1
# (alignement strict avec `_normalizeLanguage` du widget — voir
# nexya_front_end/lib/shared/widgets/business/nx_code_block.dart).
# Si un slug arrive ici hors whitelist, le détecteur l'accepte quand
# même (le widget Flutter fallback `plaintext` monospace plat) — c'est
# permissif côté backend, strict côté affichage.
_KNOWN_LANGUAGES = frozenset(
    {
        "python",
        "dart",
        "javascript",
        "js",
        "typescript",
        "ts",
        "tsx",
        "jsx",
        "java",
        "kotlin",
        "swift",
        "go",
        "rust",
        "c",
        "cpp",
        "csharp",
        "cs",
        "objc",
        "objectivec",
        "php",
        "ruby",
        "rb",
        "scala",
        "haskell",
        "elixir",
        "lua",
        "perl",
        "r",
        "matlab",
        "html",
        "css",
        "scss",
        "less",
        "sql",
        "bash",
        "sh",
        "shell",
        "zsh",
        "fish",
        "powershell",
        "ps1",
        "yaml",
        "yml",
        "json",
        "toml",
        "xml",
        "markdown",
        "md",
        "dockerfile",
        "makefile",
        "nginx",
        "graphql",
        "proto",
        "vim",
        "lisp",
        "clojure",
        "scheme",
        "fsharp",
        "fs",
        "ocaml",
        "erlang",
        "dart",
        "groovy",
        "plaintext",
        "text",
    }
)


# Mapping language → extension fichier pour fallback filename
# (stratégie d dans `_extract_filename`).
_LANGUAGE_TO_EXTENSION: dict[str, str] = {
    "python": "py",
    "dart": "dart",
    "javascript": "js",
    "js": "js",
    "typescript": "ts",
    "ts": "ts",
    "tsx": "tsx",
    "jsx": "jsx",
    "java": "java",
    "kotlin": "kt",
    "swift": "swift",
    "go": "go",
    "rust": "rs",
    "c": "c",
    "cpp": "cpp",
    "csharp": "cs",
    "cs": "cs",
    "objc": "m",
    "objectivec": "m",
    "php": "php",
    "ruby": "rb",
    "rb": "rb",
    "scala": "scala",
    "haskell": "hs",
    "elixir": "ex",
    "lua": "lua",
    "perl": "pl",
    "r": "r",
    "matlab": "m",
    "html": "html",
    "css": "css",
    "scss": "scss",
    "less": "less",
    "sql": "sql",
    "bash": "sh",
    "sh": "sh",
    "shell": "sh",
    "zsh": "sh",
    "fish": "sh",
    "powershell": "ps1",
    "ps1": "ps1",
    "yaml": "yml",
    "yml": "yml",
    "json": "json",
    "toml": "toml",
    "xml": "xml",
    "markdown": "md",
    "md": "md",
    "dockerfile": "Dockerfile",  # nom de fichier sans extension
    "makefile": "Makefile",
    "nginx": "conf",
    "graphql": "graphql",
    "proto": "proto",
    "vim": "vim",
    "lisp": "lisp",
    "clojure": "clj",
    "scheme": "scm",
    "fsharp": "fs",
    "fs": "fs",
    "ocaml": "ml",
    "erlang": "erl",
    "groovy": "groovy",
    "plaintext": "txt",
    "text": "txt",
}


# ── Helpers extraction filename ───────────────────────────────────────

# Regex stricte pour matcher un filename plausible :
# - alphanumériques, `_`, `-`, `.`, `/`
# - extension obligatoire (`.\w+`)
# - pas de path absolu ni traversal (filtré côté Pydantic _validate_filename_path_safe)
_FILENAME_LINE_RE = re.compile(r"^[\w/.\-]+\.\w+$")

# Regex markdown bold autour d'un filename : `**main.py**` ou `__app.dart__`
_FILENAME_MARKDOWN_BOLD_RE = re.compile(
    r"(?:\*\*|__)([\w/.\-]+\.\w+)(?:\*\*|__)"
)

# Regex commentaires en TÊTE de bloc pour extraire filename inline.
# - Python : `# filename.py` (1ère ligne du bloc)
# - JS/Dart/Java/Go/Rust : `// filename.js`
# - HTML/XML : `<!-- filename.html -->`
# - CSS : `/* filename.css */`
# - Bash : `# filename.sh`
_FILENAME_INLINE_COMMENT_PATTERNS: tuple[Pattern[str], ...] = (
    re.compile(r"^\s*#\s*([\w/.\-]+\.\w+)\s*$", re.MULTILINE),  # Python, Bash
    re.compile(r"^\s*//\s*([\w/.\-]+\.\w+)\s*$", re.MULTILINE),  # JS, Dart, Go
    re.compile(r"^\s*<!--\s*([\w/.\-]+\.\w+)\s*-->\s*$", re.MULTILINE),  # HTML
    re.compile(r"^\s*/\*\s*([\w/.\-]+\.\w+)\s*\*/\s*$", re.MULTILINE),  # CSS
)


def _extract_filename(
    *,
    block_content: str,
    block_language: str,
    preceding_text: str,
) -> str:
    """Extrait un filename depuis 4 stratégies fallback.

    Args:
        block_content: contenu textuel du bloc de code (sans ```)
        block_language: slug language extrait du fence ```python (peut être vide)
        preceding_text: texte AVANT le bloc (jusqu'à 500 chars) pour
            chercher un filename explicite (stratégies a et b).

    Returns:
        filename validé path-safe (jamais None — fallback final
        `code-snippet.{ext}` ou `code-snippet.txt`).
    """
    # Stratégie (a) : ligne précédente immédiate matche pattern filename.
    # On regarde la dernière ligne non-vide du preceding_text.
    lines = [ln.strip() for ln in preceding_text.rstrip().split("\n") if ln.strip()]
    if lines:
        last_line = lines[-1]
        # Strip markdown bold/italic résiduel si présent
        last_line_clean = re.sub(r"^[*_]+|[*_]+$", "", last_line).strip()
        # Strip backticks (cas `**main.py**:` ou `main.py :`)
        last_line_clean = last_line_clean.rstrip(":").strip()
        if _FILENAME_LINE_RE.match(last_line_clean) and 1 <= len(last_line_clean) <= 200:
            return last_line_clean

    # Stratégie (b) : markdown bold `**filename.ext**` dans les 200 chars
    # précédents (cas typique « voici le fichier **main.py** : ```python ... »).
    tail = preceding_text[-200:] if len(preceding_text) > 200 else preceding_text
    bold_matches = _FILENAME_MARKDOWN_BOLD_RE.findall(tail)
    if bold_matches:
        # Prend le dernier match (le plus proche du bloc).
        candidate = bold_matches[-1]
        if 1 <= len(candidate) <= 200:
            return candidate

    # Stratégie (c) : 1er commentaire du bloc (top 500 chars du content).
    block_head = block_content[:500]
    for pattern in _FILENAME_INLINE_COMMENT_PATTERNS:
        match = pattern.search(block_head)
        if match:
            candidate = match.group(1)
            if 1 <= len(candidate) <= 200:
                return candidate

    # Stratégie (d) : fallback `main.{ext}` via mapping language → extension.
    normalized_lang = block_language.strip().lower()
    # Cas spécial : language vide/plaintext/text → "code-snippet.txt" plus
    # parlant côté UX que "main.txt" (générique, pas un fichier nommable).
    if normalized_lang in {"", "plaintext", "text"}:
        return "code-snippet.txt"
    if normalized_lang in _LANGUAGE_TO_EXTENSION:
        ext = _LANGUAGE_TO_EXTENSION[normalized_lang]
        # Cas spécial Dockerfile / Makefile : pas d'extension, c'est le nom.
        if ext in {"Dockerfile", "Makefile"}:
            return ext
        return f"main.{ext}"

    # Fallback ultime : language inconnu → snippet text.
    return "code-snippet.txt"


# ── Détecteur ──────────────────────────────────────────────────────────


def detect_rich_content_code_file(
    user_message: str,
    assistant_text: str,
) -> dict | None:
    """Détecte un brouillon UN SEUL fichier de code dans la réponse IA.

    Retourne un dict conforme à `RichContentPayload.code_file()`
    (sérialisé via `.model_dump()`) ou None si pas de match.

    Conditions strictes (toutes requises) :
      1. `assistant_text` est str non-vide.
      2. Le texte contient EXACTEMENT 1 bloc ```language\n...\n```.
         (0 → no-op skip. 2+ → Code Project tentera ensuite OU skip.)
      3. Le content du bloc fait ≥ 30 chars (anti snippet trivial).
      4. Le content du bloc fait ≤ 100k chars (cap Pydantic).

    Le `user_message` est IGNORÉ ici (pas d'intent classifier requis,
    body-driven) — passé en signature pour cohérence avec les autres
    détecteurs de la cascade.

    Fail-safe absolu : toute exception Pydantic (filename path-unsafe,
    cap content dépassé, etc.) → return None (la carte n'apparaît pas,
    le bloc reste visible dans le markdown brut côté Flutter).
    """
    if not isinstance(assistant_text, str) or not assistant_text.strip():
        return None

    # Extract tous les blocs de code via regex.
    matches = _CODE_BLOCK_RE.findall(assistant_text)
    if len(matches) != 1:
        # 0 bloc → pas de code. 2+ blocs → Code Project tentera.
        return None

    raw_language, raw_content = matches[0]

    # Cap content min/max.
    if len(raw_content) < _CODE_FILE_CONTENT_MIN_CHARS:
        return None
    if len(raw_content) > _CODE_FILE_CONTENT_MAX_CHARS:
        # On pourrait tronquer mais c'est ambigu pour du code (où couper ?).
        # Mieux : refuser la carte, laisser le bloc visible dans le markdown.
        return None

    # Normalise language (vide → "plaintext" pour Pydantic).
    language = raw_language.strip().lower() or "plaintext"

    # Trouve le preceding_text (le texte AVANT ce bloc, max 500 chars).
    block_start_idx = assistant_text.find("```" + raw_language)
    if block_start_idx == -1:
        # Cas rare : language vide → cherche "```\n"
        block_start_idx = assistant_text.find("```")
    preceding_text = (
        assistant_text[:block_start_idx]
        if block_start_idx > 0
        else ""
    )

    # Extract filename via 4 stratégies fallback.
    filename = _extract_filename(
        block_content=raw_content,
        block_language=language,
        preceding_text=preceding_text,
    )

    # Construit le payload via factory Pydantic (validation stricte).
    try:
        payload = RichContentPayload.code_file(
            filename=filename,
            content=raw_content,
            language=language,
            description=None,
        )
    except Exception:  # noqa: BLE001
        # Pydantic ValidationError ou autre — la carte n'apparaît pas,
        # le bloc reste visible dans le markdown brut.
        return None

    return payload.model_dump()
