"""
Pré-nettoyage du texte avant chunking — D4 RAG documents.

Le texte extrait par `text_extractor.py` (PDF via pypdf, DOCX via
zipfile+xml) contient souvent des artefacts typographiques qui nuisent
à la qualité des embeddings :

- Graphèmes Unicode ambigus (`é` précomposé vs `e` + accent combinant).
  Deux chunks identiques sémantiquement peuvent avoir des SHA différents
  et des embeddings légèrement différents.
- Coupures de mots en fin de ligne (`développe-\nment` doit redevenir
  `développement`) — pypdf insère le `-\n` tel quel.
- En-têtes / pieds de page répétés à chaque page (`3/10`, `Page 3`)
  qui polluent le contexte chunké.
- Whitespace excessif (tabulations, espaces insécables, sauts de ligne
  redondants) — le tokenizer LLM les compte tous.

Design :

- **Fonction pure synchrone** `clean_extracted_text(raw) -> str`, pas
  d'état global. CPU-bound, rapide (~1ms pour un texte de 10 000 chars).
- **Préserve les marqueurs `[[PAGE:N]]`** injectés par l'extracteur PDF.
  Les regex headers/footers ne matchent que des patterns de chiffres
  isolés, pas le format `[[PAGE:N]]`.
- **Ordre des passes** : NFC d'abord (normalise avant tout traitement),
  puis déhyphénation (avant le collapse de newlines), puis strip
  headers/footers (par ligne), puis collapse whitespace (en dernier car
  les passes précédentes peuvent en introduire).
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

# ══════════════════════════════════════════════════════════════
# Regex compilées
# ══════════════════════════════════════════════════════════════

# Collapse whitespace horizontal : espaces, tabs, espaces insécables
# (` `), espaces fines (` `) → un seul espace. Pas de `\n`.
_WS_COLLAPSE_RE: Final[re.Pattern[str]] = re.compile(r"[ \t   ]+")

# Collapse ≥ 3 sauts de ligne en 2 (préserve les frontières de paragraphe
# sans exploser les lignes vides).
_NEWLINE_COLLAPSE_RE: Final[re.Pattern[str]] = re.compile(r"\n{3,}")

# Déhyphénation : `mot-\nsuite` → `motsuite`. Uniquement si les deux
# côtés sont des lettres (évite de casser `-\n` dans les listes ou les
# formules). La classe `\w` couvre lettres + chiffres + `_` ce qui est
# acceptable (un mot avec chiffre : `Python3-\nbased` → `Python3based`).
_DEHYPHEN_RE: Final[re.Pattern[str]] = re.compile(r"(\w)-\n(\w)")

# En-têtes / pieds de page basiques — une ligne contenant uniquement :
# - un numéro de page (ex: `3`, `12`)
# - une fraction type `3/10` ou `3 / 10`
# - la mention `Page 3`, `page 3`, `Page 3 sur 10`
# Suppression en mode MULTILINE (^ et $ sur chaque ligne).
# Le marqueur `[[PAGE:N]]` ne matche PAS car il contient `[[` et `]]`.
_PAGE_HEADER_FOOTER_RE: Final[re.Pattern[str]] = re.compile(
    r"^[ \t]*("
    r"\d{1,4}"
    r"|\d{1,4}\s*/\s*\d{1,4}"
    r"|[Pp]age\s+\d{1,4}(\s+(sur|of|de)\s+\d{1,4})?"
    r")[ \t]*$",
    re.MULTILINE,
)


# ══════════════════════════════════════════════════════════════
# API publique
# ══════════════════════════════════════════════════════════════


def clean_extracted_text(raw: str) -> str:
    """Applique le pipeline de pré-nettoyage complet.

    Pipeline :
    1. Normalisation Unicode NFC (défusionne les graphèmes ambigus).
    2. Déhyphénation en fin de ligne (`mot-\\nsuite` → `motsuite`).
    3. Strip des en-têtes / pieds de page (lignes `3/10`, `Page 3`).
    4. Collapse whitespace horizontal → un seul espace.
    5. Collapse ≥ 3 sauts de ligne → 2 (préserve paragraphes).
    6. Strip final (lead/trail).

    Les marqueurs `[[PAGE:N]]` sont **préservés** — le chunker les utilise
    pour dériver le `page_number` de chaque chunk.
    """
    if not raw:
        return ""

    text = unicodedata.normalize("NFC", raw)
    text = _DEHYPHEN_RE.sub(r"\1\2", text)
    text = _PAGE_HEADER_FOOTER_RE.sub("", text)
    text = _WS_COLLAPSE_RE.sub(" ", text)
    text = _NEWLINE_COLLAPSE_RE.sub("\n\n", text)
    return text.strip()
