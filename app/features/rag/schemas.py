"""
Schémas Pydantic — endpoint public `/rag/query` (D5).

Contrat API :
- `RagQueryRequest` : query texte, k, min_similarity, file_ids optionnels.
- `RagChunkItem` : chunk complet (content, offsets, page, similarity).
- `RagSourceItem` : équivalent allégé pour la liste « Sources » côté UI
  (pas de `content` — évite de dupliquer un bloc déjà présent dans
  `chunks`).
- `RagQueryResponse` : les deux listes + le `framed_context` + la ligne
  `instruction` système prête à injecter dans un LLM.

Discipline :
- `k` ∈ [1, 20] — Cap 20 parce qu'au-delà le contexte LLM explose et
  les sources deviennent bruit (seuil empirique).
- `min_similarity` défaut 0.6 — un peu plus tolérant que `/memory/search`
  (0.7) parce qu'un chunk de document matche moins bien qu'un fait
  normalisé.
- `file_ids` optionnel : permet à un user de scoper sa recherche à un
  sous-ensemble de ses fichiers (ex: « cherche dans ces 3 PDFs »).
- Pas de vecteur brut exposé côté client.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator

# ══════════════════════════════════════════════════════════════
# Requête
# ══════════════════════════════════════════════════════════════


class RagQueryRequest(BaseModel):
    """Body de `POST /rag/query`."""

    query: str = Field(min_length=1, max_length=1000)
    k: int = Field(default=5, ge=1, le=20)
    min_similarity: float = Field(default=0.6, ge=0.0, le=1.0)
    file_ids: list[uuid.UUID] | None = Field(default=None, max_length=50)

    @field_validator("query", mode="before")
    @classmethod
    def _strip_query(cls, v):
        return v.strip() if isinstance(v, str) else v


# ══════════════════════════════════════════════════════════════
# Items de réponse
# ══════════════════════════════════════════════════════════════


class RagChunkItem(BaseModel):
    """Chunk complet retourné par la recherche vectorielle.

    `similarity` ∈ [0, 1] — 1.0 signifie un parfait match cosinus.
    """

    id: int
    file_id: uuid.UUID
    chunk_index: int
    content: str
    token_count: int
    start_char_offset: int
    end_char_offset: int
    page_number: int | None = None
    similarity: float = Field(ge=-1.0, le=1.0)
    original_filename: str | None = None
    mime_type: str | None = None


class RagSourceItem(BaseModel):
    """Source allégée pour l'UI — sans `content` (pas de doublon).

    Le front utilise ces champs pour afficher un panneau « Sources »
    dépliable sous une réponse IA : titre du fichier + page + tap →
    viewer interne avec surlignage via `start_char_offset` et
    `end_char_offset`.
    """

    file_id: uuid.UUID
    chunk_index: int
    start_char_offset: int
    end_char_offset: int
    page_number: int | None = None
    similarity: float = Field(ge=-1.0, le=1.0)
    original_filename: str | None = None


# ══════════════════════════════════════════════════════════════
# Réponse
# ══════════════════════════════════════════════════════════════


class RagQueryResponse(BaseModel):
    """Résultat d'une requête RAG.

    - `chunks` : liste brute avec contenu, offsets, page.
    - `sources` : liste UI-friendly (même ordre que `chunks`).
    - `framed_context` : chunks wrappés `<<<DOCUMENT EXTRACT>>>...`.
      Vide si aucun chunk trouvé.
    - `instruction` : ligne système anti-injection à préfixer au
      `framed_context` avant l'appel LLM. Vide si `chunks` vide.

    Le caller (future session chat-RAG) compose :
        system_prompt = (
            instruction + "\\n\\n" +
            framed_context + "\\n\\n" +
            base_expert_prompt
        )
    """

    chunks: list[RagChunkItem]
    sources: list[RagSourceItem]
    framed_context: str
    instruction: str
