"""
Détecteur MIME par magic-bytes — pure Python, zéro dépendance externe.

Pourquoi pas `python-magic` ?
- Wrapper de `libmagic` (binaire C). Nécessite installation système
  (`apt install libmagic1`, binaires Windows pénibles via `python-magic-bin`
  qui ne supporte pas Py 3.12+).
- Douloureux en CI (Alpine nécessite `libmagic-dev` compilé, temps build).
- Pour notre usage (12 formats strictement whitelistés), un détecteur
  home-made de ~100 lignes fait le job exactement et n'ajoute aucune dep.

La stratégie en deux temps :

1. **Match des signatures magic-bytes** sur les N premiers octets. Couvre
   les formats avec un en-tête unique (PDF, PNG, JPEG, GIF, MP4, etc.).
2. **Discrimination OOXML via inspection du ZIP** pour les formats Office
   2007+ qui sont tous des fichiers ZIP structurés. On ouvre le ZIP en
   mémoire et on cherche le fichier marqueur : `word/document.xml`
   (DOCX), `xl/workbook.xml` (XLSX), `ppt/presentation.xml` (PPTX).

Double validation critique côté `FileUploadService` :
- Le MIME **annoncé** par le client (UploadFile.content_type) doit être
  dans la whitelist (settings.files_allowed_mimes).
- Le MIME **détecté** par ce module doit correspondre au MIME annoncé
  (avec tolérance sur les alias courants : image/jpeg ≡ image/jpg).

Anti-smuggling : sans cette double vérification, un attaquant peut poster
un `.exe` avec Content-Type `image/png`. Le backend écrirait sur MinIO un
fichier nommé ".png" qui servirait du code exécutable à un client qui le
télécharge. Le detect_mime_type coupe cette attaque à la source.
"""

from __future__ import annotations

import io
import zipfile
from typing import Final

import structlog

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Signatures magic-bytes — (offset, prefix, mime_type)
# ══════════════════════════════════════════════════════════════
#
# Ordre d'évaluation significatif :
# - Les patterns spécifiques (GIF87a/89a, MP3 frames) AVANT les génériques.
# - RIFF est ambigu (webp, wav, avi) → traité séparément.
# - ZIP est traité en dernier — les OOXML sont discriminés après.

_SIGNATURES: Final[list[tuple[int, bytes, str]]] = [
    # PDF — commence toujours par %PDF- (spec 1.0-1.7+).
    (0, b"%PDF-", "application/pdf"),
    # PNG — magic strict 8 bytes.
    (0, b"\x89PNG\r\n\x1a\n", "image/png"),
    # JPEG — 3 bytes minimum (SOI marker + premier marker APP/JFIF/EXIF).
    (0, b"\xff\xd8\xff", "image/jpeg"),
    # GIF — deux versions.
    (0, b"GIF87a", "image/gif"),
    (0, b"GIF89a", "image/gif"),
    # MP4 — box ftyp au début (4 bytes de longueur puis "ftyp").
    # Patterns courants : 24-byte ftyp box (major_brand + minor + compat).
    (4, b"ftyp", "video/mp4"),
    # MP3 — frame sync bits (0xFF 0xFB/F3/F2) OU tag ID3 en tête.
    (0, b"ID3", "audio/mpeg"),
    (0, b"\xff\xfb", "audio/mpeg"),
    (0, b"\xff\xf3", "audio/mpeg"),
    (0, b"\xff\xf2", "audio/mpeg"),
    # OGG — OggS magic.
    (0, b"OggS", "audio/ogg"),
]

# RIFF containers (WebP, WAV, AVI). Discriminés par le sub-type à l'offset 8.
_RIFF_SUBTYPES: Final[dict[bytes, str]] = {
    b"WEBP": "image/webp",
    b"WAVE": "audio/wav",
    b"AVI ": "video/avi",  # pas dans whitelist par défaut, mais documenté.
}

# ZIP magic (local file header) — chaque OOXML en démarre. Signature
# secondaire `PK\x05\x06` = empty archive.
_ZIP_MAGIC: Final[tuple[bytes, ...]] = (b"PK\x03\x04", b"PK\x05\x06")

# Marqueurs distinctifs dans un ZIP OOXML pour identifier le format précis.
_OOXML_MARKERS: Final[list[tuple[str, str]]] = [
    (
        "word/document.xml",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ),
    (
        "xl/workbook.xml",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ),
    (
        "ppt/presentation.xml",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ),
]


# ══════════════════════════════════════════════════════════════
# API publique
# ══════════════════════════════════════════════════════════════


def detect_mime_type(data: bytes) -> str | None:
    """Inspecte les octets de tête et retourne le MIME détecté, ou None.

    `data` peut être court (≥ 16 bytes recommandé). Si `data` est vide,
    retourne None immédiatement.

    La fonction est totalement synchrone et sans I/O réseau. Elle n'allouera
    d'objets significatifs QUE pour discriminer un ZIP OOXML
    (`zipfile.ZipFile(io.BytesIO(data))` — nécessite les premiers kilo-bytes).
    """
    if not data:
        return None

    # 1. Signatures magic-bytes standards.
    for offset, prefix, mime in _SIGNATURES:
        if len(data) < offset + len(prefix):
            continue
        if data[offset : offset + len(prefix)] == prefix:
            return mime

    # 2. RIFF discriminé par sub-type à l'offset 8.
    if len(data) >= 12 and data[0:4] == b"RIFF":
        subtype = data[8:12]
        if subtype in _RIFF_SUBTYPES:
            return _RIFF_SUBTYPES[subtype]
        # RIFF inconnu — on ne prétend pas savoir.
        return None

    # 3. ZIP — éventuellement OOXML. On discrimine.
    if any(data.startswith(magic) for magic in _ZIP_MAGIC):
        ooxml_mime = _discriminate_ooxml(data)
        return ooxml_mime or "application/zip"

    return None


def _discriminate_ooxml(data: bytes) -> str | None:
    """Ouvre le ZIP en mémoire et cherche un marqueur OOXML.

    Retourne le MIME OOXML précis (docx/xlsx/pptx) ou None si le ZIP
    n'est pas un format OOXML reconnu (archive user arbitraire).

    Swallow toutes les exceptions `zipfile` : un ZIP mal formé n'est pas
    OOXML, fin de l'histoire. Le caller traitera ça comme un `application/
    zip` générique.
    """
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            names = set(zf.namelist())
    except (zipfile.BadZipFile, ValueError, OSError) as exc:
        log.debug("mime.zip.unreadable", error=str(exc))
        return None

    for marker, mime in _OOXML_MARKERS:
        if marker in names:
            return mime
    return None


# ══════════════════════════════════════════════════════════════
# Tolérance sur les alias MIME courants
# ══════════════════════════════════════════════════════════════

# Aliases acceptés pour la comparaison entre MIME annoncé et MIME détecté :
# deux chaînes équivalentes d'un point de vue byte-layout mais parfois
# formalisées différemment par les clients HTTP.
_MIME_ALIASES: Final[dict[str, set[str]]] = {
    "image/jpeg": {"image/jpeg", "image/jpg", "image/pjpeg"},
    "text/plain": {"text/plain", "text/markdown", "text/csv", "text/x-markdown"},
}


def mimes_compatible(announced: str, detected: str) -> bool:
    """Vrai si le MIME annoncé par le client est cohérent avec le MIME
    détecté par magic-bytes.

    Tolère les alias courants (image/jpeg ≡ image/jpg, text/plain famille),
    strict sur tout le reste.
    """
    if announced == detected:
        return True
    for canonical, aliases in _MIME_ALIASES.items():
        if announced in aliases and detected in aliases:
            return True
    return False
