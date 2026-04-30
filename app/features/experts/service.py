"""
ExpertCorpusService — recherche vectorielle dans `expert_corpus_chunks` (G1).

Miroir architectural de `RagQueryService` (D5) adapté aux contraintes du
corpus système :

- **Pas de `user_id`** — le corpus est global, le scope de filtrage est
  uniquement `expert_slug`. Pas de JOIN `uploaded_files` ni check IDOR —
  le corpus n'appartient à personne.
- **Filtre optionnel `language_pair`** — pour l'expert Langues, si la
  heuristique détecte « traduis en espagnol » on peut scoper le retrieval
  sur les paires `fra-spa` / `spa-fra` pour améliorer la précision.
- **Dim 768** — CAST `vector` dans les literal SQLAlchemy.

Pipeline strict :
1. Validation `expert_slug` non-vide.
2. Clamping `k` dans [1, 20].
3. SQL cosinus `<=>` avec HNSW sur `(expert_slug, embedding)`.
4. Seuil plancher `min_similarity` (post-SQL filter côté WHERE).
5. Retour `list[ExpertChunkResult]` trié par similarité décroissante
   (ordre implicite donné par `ORDER BY embedding <=> :q_vec`).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = structlog.get_logger()


_MIN_K: Final[int] = 1
_MAX_K: Final[int] = 20


@dataclass(frozen=True, slots=True)
class ExpertChunkResult:
    """Résultat d'un search corpus expert.

    Champs exposés uniquement ce qui sert au framing LLM (pas de
    `content_sha256`, pas de `embedding`, pas de `created_at`).

    `content` attribué en tant que tel pour coller au duck-typing de
    `build_rag_framed_context` (D5) qui lit `chunk.content`.
    """

    id: int
    content: str
    source: str
    language_pair: str | None
    similarity: float
    metadata: dict[str, Any]


class ExpertCorpusService:
    """Service de recherche RAG dans `expert_corpus_chunks`."""

    @staticmethod
    async def search(
        db: AsyncSession,
        *,
        expert_slug: str,
        query_embedding: list[float],
        k: int = 5,
        min_similarity: float = 0.7,
        language_pair: str | None = None,
    ) -> list[ExpertChunkResult]:
        """Top-K chunks corpus triés par similarité cosinus décroissante.

        Args:
            db: session async en cours.
            expert_slug: scope obligatoire (ex: 'language', 'cooking').
            query_embedding: vecteur 768 dim déjà calculé (embed côté
                caller — le service ne touche pas au provider).
            k: limite clampée [1, 20].
            min_similarity: seuil plancher `[0, 1]` (1.0 = identique
                strict). Un chunk sous le seuil est rejeté.
            language_pair: filtre optionnel (ex: 'fra-spa'). None = pas
                de filtre.

        Returns:
            Liste possiblement vide si rien ne passe le seuil. Triée
            par similarité décroissante.
        """
        if not expert_slug:
            return []

        effective_k = max(_MIN_K, min(_MAX_K, int(k)))
        pgvec_literal = "[" + ",".join(str(x) for x in query_embedding) + "]"

        bindparams: dict[str, Any] = {
            "q_vec": pgvec_literal,
            "slug": expert_slug,
            "min_sim": float(min_similarity),
            "k": effective_k,
        }
        lang_clause = ""
        if language_pair:
            lang_clause = "AND language_pair = :lang"
            bindparams["lang"] = language_pair

        sql = text(
            f"""
            SELECT
                id,
                content,
                source,
                language_pair,
                metadata_json,
                1 - (embedding <=> CAST(:q_vec AS vector)) AS similarity
            FROM expert_corpus_chunks
            WHERE expert_slug = :slug
              {lang_clause}
              AND (1 - (embedding <=> CAST(:q_vec AS vector))) >= :min_sim
            ORDER BY embedding <=> CAST(:q_vec AS vector)
            LIMIT :k
            """
        ).bindparams(**bindparams)

        result = await db.execute(sql)
        rows = result.mappings().all()

        results: list[ExpertChunkResult] = []
        for r in rows:
            meta = r["metadata_json"] or {}
            if not isinstance(meta, dict):
                # metadata_json peut être sérialisé en str par certains
                # drivers — garde-fou défensif.
                meta = {}
            results.append(
                ExpertChunkResult(
                    id=int(r["id"]),
                    content=r["content"],
                    source=r["source"],
                    language_pair=r["language_pair"],
                    similarity=float(r["similarity"]),
                    metadata=dict(meta),
                )
            )

        log.info(
            "experts.corpus.search",
            expert_slug=expert_slug,
            k=effective_k,
            min_similarity=min_similarity,
            language_pair=language_pair,
            n_results=len(results),
        )
        return results
