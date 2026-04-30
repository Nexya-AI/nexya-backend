"""
Sanitizer — nettoyage défensif des entrées utilisateur côté backend.

Ce module NE fait PAS d'échappement HTML : ce rôle appartient au frontend
au moment du rendu, ou à Jinja2 (`autoescape=True`) côté emails. On ne
veut surtout pas appliquer un échappement à l'entrée — sinon on stocke
du texte déjà échappé en DB, et il sera doublement échappé au rendu.

Ce que fait ce module :
1. **Supprimer les null bytes (`\\x00`)**. PostgreSQL TEXT interdit le
   `\\x00` UTF-8 valide et lève une `DataError` au `INSERT`. Pire : un
   null byte au milieu d'un `display_name` peut casser des outils tiers
   (exports, CSV, parsers de logs).
2. **Normaliser Unicode en NFC**. Sans ça, « é » peut être encodé comme
   `U+00E9` (1 codepoint) ou `U+0065 U+0301` (2 codepoints combinants).
   Conséquence : `"Eléa" != "Eléa"` dans une comparaison byte-à-byte,
   alors que l'utilisateur ne voit aucune différence. On normalise en
   NFC (forme composée) à l'entrée pour que la DB, les index et les
   logs soient cohérents.
3. **Retirer les caractères de contrôle invisibles** sauf `\\n`, `\\r`,
   `\\t`. Un utilisateur qui colle du contenu depuis un éditeur Windows
   ou une page web peut injecter des `U+200B` (zero-width space),
   `U+202E` (right-to-left override — fait lire un texte à l'envers,
   utilisé dans des scams de phishing), ou des `U+FEFF` (BOM). On les
   retire silencieusement — aucun usage légitime pour ces caractères
   dans un nom de compte ou un message utilisateur.
4. **Borner la longueur**. Protection en profondeur au-delà des
   `max_length` Pydantic — si un service reçoit un input qui a
   contourné la validation (ex. flux interne), la longueur reste bornée.

Appels : toutes les strings publiques (display_name, bio, titre de
conversation, contenu de message, reason d'un signalement). La
Pydantic field_validator reste la première ligne ; `clean_text` est
la seconde ligne, appelée dans le service avant persistance DB.
"""

from __future__ import annotations

import unicodedata

# Caractères de contrôle Unicode dangereux / inutiles qu'on retire.
# On garde \n (0x0A), \r (0x0D), \t (0x09) qui sont légitimes en texte.
_CONTROL_CHARS_TO_STRIP = {
    ch for ch in (chr(i) for i in range(0x00, 0x20)) if ch not in ("\n", "\r", "\t")
}
# Zero-width + bidi + BOM
_INVISIBLE_CHARS = {
    "\u200b",  # zero-width space
    "\u200c",  # zero-width non-joiner
    "\u200d",  # zero-width joiner
    "\u200e",  # left-to-right mark
    "\u200f",  # right-to-left mark
    "\u202a",  # left-to-right embedding
    "\u202b",  # right-to-left embedding
    "\u202c",  # pop directional formatting
    "\u202d",  # left-to-right override
    "\u202e",  # right-to-left override (phishing vector)
    "\u2066",  # left-to-right isolate
    "\u2067",  # right-to-left isolate
    "\u2068",  # first strong isolate
    "\u2069",  # pop directional isolate
    "\ufeff",  # byte-order mark / zero-width no-break space
}
_STRIP_SET = _CONTROL_CHARS_TO_STRIP | _INVISIBLE_CHARS


def clean_text(
    value: str | None,
    *,
    max_length: int | None = None,
    collapse_whitespace: bool = False,
) -> str | None:
    """Nettoie une chaîne utilisateur. Retourne `None` si l'entrée est `None`.

    Args:
        value: chaîne à nettoyer (peut être `None` pour les champs optionnels).
        max_length: si fourni, tronque le résultat après nettoyage.
        collapse_whitespace: si `True`, remplace toute séquence d'espaces
            consécutifs par un seul espace (utile pour `display_name` où
            `"John    Doe"` devient `"John Doe"`). Laissé à `False` par
            défaut pour préserver les messages utilisateurs contenant
            volontairement des retours à la ligne ou des indentations.

    Returns:
        Chaîne nettoyée, `strip()`-ée, ou `None` si l'entrée était `None`.

    Opérations effectuées, dans l'ordre :
        1. Retire null bytes.
        2. Normalise en NFC.
        3. Retire caractères de contrôle invisibles / bidi.
        4. `strip()` les espaces au bord.
        5. Optionnel : collapse les whitespaces internes.
        6. Optionnel : tronque à `max_length`.

    Cette fonction NE lève JAMAIS d'exception sur une entrée légitime —
    elle est conçue pour être appelée unconditionally dans le service.
    """
    if value is None:
        return None

    # 1. Null bytes — indispensable avant tout autre traitement
    cleaned = value.replace("\x00", "")

    # 2. Normalisation NFC
    cleaned = unicodedata.normalize("NFC", cleaned)

    # 3. Retirer contrôle + invisible
    if any(ch in _STRIP_SET for ch in cleaned):
        cleaned = "".join(ch for ch in cleaned if ch not in _STRIP_SET)

    # 4. Strip bord
    cleaned = cleaned.strip()

    # 5. Collapse whitespace (optionnel)
    if collapse_whitespace and cleaned:
        cleaned = " ".join(cleaned.split())

    # 6. Tronquer
    if max_length is not None and len(cleaned) > max_length:
        cleaned = cleaned[:max_length].rstrip()

    return cleaned


def clean_email(value: str) -> str:
    """Normalise un email pour un stockage / lookup cohérent.

    - `strip()` des espaces (un utilisateur colle parfois avec des espaces)
    - Lowercase (les emails sont case-insensitive dans la plupart des RFC)
    - Retrait des null bytes / caractères invisibles

    On n'applique PAS de validation de format ici — c'est le rôle de
    Pydantic `EmailStr` (qui utilise `email-validator`). Cette fonction
    suppose que l'email a déjà passé la validation Pydantic.
    """
    cleaned = clean_text(value) or ""
    return cleaned.lower()


def is_safe_identifier(value: str, *, max_length: int = 128) -> bool:
    """Vérifie qu'une chaîne est utilisable comme identifiant safe.

    Un « identifiant safe » est un string sans caractères spéciaux,
    utilisable en clé de cache Redis, en paramètre d'URL, dans un header
    HTTP, ou dans un nom de fichier. Acceptés : `[a-zA-Z0-9_-]`.

    Utilisation typique : valider un `X-Device-Id` avant de l'utiliser
    comme clé de quota. Rejet d'un device_id contenant `\\n` qui pourrait
    être utilisé pour faire du header injection.

    Args:
        value: chaîne à tester.
        max_length: longueur max acceptable (défaut 128).

    Returns:
        True si la chaîne est non-vide, <= max_length, et contient
        uniquement des caractères `[a-zA-Z0-9_-]`.
    """
    if not value or len(value) > max_length:
        return False
    return all(ch.isalnum() or ch in ("_", "-") for ch in value)
