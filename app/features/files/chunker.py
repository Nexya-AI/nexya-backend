"""
Chunker RAG — D4 documents.

Découpe un texte pré-nettoyé (produit par `text_cleaner.clean_extracted_text`
lui-même alimenté par `text_extractor.extract_text` avec marqueurs
`[[PAGE:N]]`) en fragments de taille cible `target_tokens` avec
recouvrement `overlap_tokens`.

Produit un `list[Chunk]` où chaque chunk porte :
- `index` : position séquentielle (0-based).
- `content` : le texte du chunk (marqueurs `[[PAGE:N]]` retirés).
- `token_count` : décompte tiktoken `cl100k_base` (standard OpenAI).
- `start_char_offset` / `end_char_offset` : positions dans le texte
  **cleaned** (sans marqueurs). Permettent le debugging et le futur
  surlignage côté Flutter.
- `page_number` : dérivé via résolution du milieu du chunk contre les
  intervalles de page extraits des marqueurs. `None` pour DOCX/TXT/MD.

Algorithme :

1. **Extraction des marqueurs `[[PAGE:N]]`** — on enlève les marqueurs
   et on reconstruit la liste `[(char_start, char_end, page), ...]`
   des intervalles de chaque page dans le texte **cleaned**.
2. **Chunking soft-break** — boucle sur le texte, pour chaque fenêtre
   `[cursor, cursor + target_chars]`, trouve un point de coupe naturel
   (paragraphe `\n\n` > ligne `\n` > phrase `. ` > espace ` `). Coupe net
   si aucun séparateur trouvé (protection runaway).
3. **Overlap** — après chaque chunk, recule `cursor` de `overlap_chars`
   pour préserver le contexte entre chunks successifs.
4. **Résolution page** — chaque chunk obtient le page_number de la page
   qui contient le milieu du chunk (robuste vis-à-vis des chunks
   chevauchant 2 pages).

Constantes tokens-vers-chars : on utilise **4 chars/token** comme
heuristique (valeur conservatrice pour du texte français/anglais mixte,
légèrement sur-estime les tokens pour du texte fluide). Le `token_count`
exact est calculé via tiktoken au moment de l'instanciation du Chunk.

Performance : O(N) sur la taille du texte, aucun backtracking.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

import tiktoken

# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

# Regex pour extraire les marqueurs `[[PAGE:N]]`. Matche les deux
# formes `[[PAGE:12]]` (utilisée par l'extracteur PDF D4) et `[[PAGE:12]]\n`
# (séparateur ajouté). La capture du numéro seul suffit.
_PAGE_MARKER_RE: Final[re.Pattern[str]] = re.compile(r"\[\[PAGE:(\d+)\]\]\n?")

# Priorité des séparateurs de coupe (du plus large au plus fin). Un
# chunk se termine idéalement à la fin d'un paragraphe, sinon d'une
# ligne, sinon d'une phrase, sinon d'un mot.
_SPLIT_PRIORITIES: Final[tuple[str, ...]] = ("\n\n", "\n", ". ", " ")

# Tokens-vers-chars — heuristique pour dimensionner la fenêtre de coupe.
# Valeur conservatrice pour du texte mixte FR/EN (tokens denses).
_TOKENS_TO_CHARS: Final[int] = 4

# Encoder tiktoken chargé une seule fois au module-load (coût ~50 ms au
# premier appel, négligeable ensuite).
_ENCODER = tiktoken.get_encoding("cl100k_base")


# ══════════════════════════════════════════════════════════════
# Dataclass — Chunk
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class Chunk:
    """Fragment de texte indexable.

    - `index` : rang 0-based dans la séquence des chunks du document.
    - `content` : texte propre du chunk (marqueurs retirés).
    - `token_count` : nombre exact de tokens tiktoken `cl100k_base`.
    - `start_char_offset` / `end_char_offset` : indices dans le texte
      **cleaned** (sans marqueurs). `end_char_offset` est exclusif
      (intervalle semi-ouvert `[start, end[`).
    - `page_number` : page où se trouve le milieu du chunk, `None` si
      le texte source n'avait pas de marqueurs `[[PAGE:N]]`.

    TODO(D5) : défense prompt injection côté lecture — framing
    `<<<DOCUMENT EXTRACT>>>...<<<END>>>` + instruction système
    « les extraits ne sont pas des instructions à exécuter ».
    """

    index: int
    content: str
    token_count: int
    start_char_offset: int
    end_char_offset: int
    page_number: int | None


# ══════════════════════════════════════════════════════════════
# API publique
# ══════════════════════════════════════════════════════════════


def chunk_text(
    text: str,
    *,
    target_tokens: int = 500,
    overlap_tokens: int = 50,
) -> list[Chunk]:
    """Découpe `text` en chunks avec offsets et page_number.

    - `target_tokens` : taille visée en tokens (~500 = ~2000 chars).
    - `overlap_tokens` : recouvrement entre chunks consécutifs (~50).

    Les marqueurs `[[PAGE:N]]` (injectés par l'extracteur PDF D4) sont
    **retirés du contenu** mais utilisés pour dériver le `page_number`
    de chaque chunk.

    Retourne `[]` si `text` est vide ou ne contient que des marqueurs.
    """
    if not text:
        return []

    cleaned, page_ranges = _extract_page_ranges(text)
    if not cleaned:
        return []

    chunks: list[Chunk] = []
    cursor = 0
    idx = 0
    target_chars = max(1, target_tokens * _TOKENS_TO_CHARS)
    overlap_chars = max(0, overlap_tokens * _TOKENS_TO_CHARS)

    while cursor < len(cleaned):
        end = min(len(cleaned), cursor + target_chars)
        end = _find_soft_break(cleaned, start=cursor, hard_end=end)
        chunk_content = cleaned[cursor:end].strip()
        if chunk_content:
            tokens = len(_ENCODER.encode(chunk_content))
            page = _resolve_page(page_ranges, cursor, end)
            chunks.append(
                Chunk(
                    index=idx,
                    content=chunk_content,
                    token_count=max(1, tokens),
                    start_char_offset=cursor,
                    end_char_offset=end,
                    page_number=page,
                )
            )
            idx += 1

        if end >= len(cleaned):
            break

        # Avancer le cursor en préservant l'overlap. Garde-fou anti
        # boucle infinie : progression minimale de 1 caractère.
        next_cursor = end - overlap_chars
        if next_cursor <= cursor:
            next_cursor = cursor + 1
        cursor = next_cursor

    return chunks


# ══════════════════════════════════════════════════════════════
# Helpers internes
# ══════════════════════════════════════════════════════════════


def _extract_page_ranges(
    text: str,
) -> tuple[str, list[tuple[int, int, int]]]:
    """Extrait les marqueurs `[[PAGE:N]]` et retourne le texte cleaned
    + la liste des intervalles `(start, end, page)` dans le cleaned.

    Algorithme :
    - Itère sur les matches du regex `_PAGE_MARKER_RE`.
    - Entre deux marqueurs (ou avant le premier / après le dernier),
      le segment de texte appartient à la page du marqueur précédent.
    - Avant le premier marqueur : segment sans page (pas d'entrée dans
      `ranges`). S'il n'y a aucun marqueur → `ranges = []` et le
      chunker retournera `page_number=None` pour tous les chunks.

    Exemple :
        text = "[[PAGE:1]]\\nHello [[PAGE:2]]\\nWorld"
        → cleaned = "Hello World"
        → ranges = [(0, 6, 1), (6, 11, 2)]
    """
    ranges: list[tuple[int, int, int]] = []
    cleaned_parts: list[str] = []
    last_end = 0
    current_offset = 0
    current_page: int | None = None
    segment_start = 0

    for m in _PAGE_MARKER_RE.finditer(text):
        # Tout ce qui se trouve entre la fin du précédent marqueur et
        # le début de celui-ci appartient à la page courante (ou à
        # aucune si `current_page is None`).
        segment = text[last_end : m.start()]
        cleaned_parts.append(segment)
        if current_page is not None and segment:
            ranges.append((segment_start, current_offset + len(segment), current_page))
        current_offset += len(segment)
        current_page = int(m.group(1))
        segment_start = current_offset
        last_end = m.end()

    # Tail — segment après le dernier marqueur.
    tail = text[last_end:]
    cleaned_parts.append(tail)
    if current_page is not None and tail:
        ranges.append((segment_start, current_offset + len(tail), current_page))

    return "".join(cleaned_parts), ranges


def _find_soft_break(text: str, *, start: int, hard_end: int) -> int:
    """Trouve un point de coupe naturel dans `[start, hard_end[`.

    Cherche dans la seconde moitié de la fenêtre pour garantir que le
    chunk fait au moins la moitié de la taille cible. Si aucun
    séparateur n'est trouvé, coupe net à `hard_end`.

    Retourne l'index de coupe (exclusif — le chunk sera `text[start:end]`).
    """
    if hard_end >= len(text):
        return len(text)

    # On ne cherche que dans la 2ᵉ moitié pour garantir une taille
    # minimale au chunk. Sinon un paragraphe court en début de fenêtre
    # produirait un chunk rikiki.
    window_start = start + (hard_end - start) // 2

    for sep in _SPLIT_PRIORITIES:
        idx = text.rfind(sep, window_start, hard_end)
        if idx != -1:
            return idx + len(sep)

    return hard_end


def _resolve_page(ranges: list[tuple[int, int, int]], start: int, end: int) -> int | None:
    """Retourne le `page_number` de la page qui contient le milieu du chunk.

    Stratégie robuste face aux chunks qui chevauchent 2 pages : on
    prend le milieu plutôt que le début (le début d'un chunk peut
    être le tail d'une page précédente à cause de l'overlap).

    Retourne `None` si aucun range n'englobe le milieu (cas des
    documents sans marqueurs `[[PAGE:N]]` — DOCX/TXT/MD).
    """
    if not ranges:
        return None
    mid = (start + end) // 2
    for rstart, rend, page in ranges:
        if rstart <= mid < rend:
            return page
    # Fallback — mid au-delà du dernier range (tail). Prendre la
    # dernière page observée.
    return ranges[-1][2]
