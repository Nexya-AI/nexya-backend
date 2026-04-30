"""
RagQueryService — recherche vectorielle dans `document_chunks` (D5).

Pipeline strict :

1. **Validation** de la query (non vide, déjà strippée par Pydantic).
2. **Clamping k** dans [_MIN_K, _MAX_K] = [1, 20].
3. **Budget embeddings** pré-flight → 429 `RATE_LIMIT_EXCEEDED` si
   saturé (consomme 1 crédit pour embed de la query).
4. **Embed query** via `EmbeddingsProvider` → vecteur 1536 dim.
5. **SQL cosinus avec JOIN strict** sur `uploaded_files` :
   - `WHERE dc.user_id = :uid` (rempart IDOR, anti cross-user).
   - `AND uf.deleted_at IS NULL` (pas de chunks de fichiers soft-deleted).
   - `AND similarity >= :min_sim` (filtre seuil).
   - `AND dc.file_id = ANY(:file_ids)` si filtre présent.
   - `ORDER BY embedding <=> :q_vec LIMIT :k` (HNSW en O(log N)).
6. **Framing anti-injection** via `build_rag_framed_context` — chunks
   wrappés dans `<<<DOCUMENT EXTRACT>>>...<<<END EXTRACT N>>>` + ligne
   système anti-injection.
7. **Log forensic** `rag.query.completed` avec n_chunks, query_len,
   model, k, min_similarity (pas de payload — on ne logue pas la query
   elle-même pour ne pas fuiter le contenu user dans les logs).

Zéro cache Redis : chaque query peut voir de nouveaux documents
indexés entre-temps. Un cache cross-user serait aussi un vecteur de
fuite RGPD. Coût embed query négligeable ($0.000001).
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.budget_tracker import get_budget_tracker
from app.ai.embeddings.base import EmbeddingsError
from app.ai.embeddings.runtime import get_embeddings_provider
from app.core.errors.exceptions import (
    EmbeddingsUnavailableException,
    ValidationException,
)
from app.features.auth.models import User
from app.features.files.rag_framing import build_rag_framed_context
from app.features.rag.schemas import (
    RagChunkItem,
    RagQueryResponse,
    RagSourceItem,
)

log = structlog.get_logger()


_MIN_K: Final[int] = 1
_MAX_K: Final[int] = 20
_DEFAULT_K: Final[int] = 5
_DEFAULT_MIN_SIMILARITY: Final[float] = 0.6


class RagQueryService:
    """Service de recherche RAG dans `document_chunks`."""

    @staticmethod
    async def query(
        user: User,
        db: AsyncSession,
        *,
        query: str,
        k: int = _DEFAULT_K,
        min_similarity: float = _DEFAULT_MIN_SIMILARITY,
        file_ids: list[uuid.UUID] | None = None,
    ) -> RagQueryResponse:
        """Recherche sémantique top-K + framing anti-injection.

        Lève :
        - `ValidationException` (422) : query vide après strip.
        - `RateLimitExceededException` (429) : budget embeddings épuisé.
        - `EmbeddingsUnavailableException` (503) : provider down.
        """
        # 1. Validation query.
        normalized = (query or "").strip()
        if not normalized:
            raise ValidationException("La requête RAG ne peut pas être vide.")

        # 2. Clamping k.
        effective_k = max(_MIN_K, min(_MAX_K, int(k)))

        # 3. Budget embeddings (1 crédit pour embed de la query).
        await get_budget_tracker().check_and_consume_embeddings(str(user.id), cost=1)

        # 4. Embed query.
        provider = get_embeddings_provider()
        try:
            response = await provider.embed([normalized])
        except EmbeddingsError as exc:
            log.warning(
                "rag.query.embed_failed",
                user_id=str(user.id),
                provider=exc.provider,
                error=str(exc),
            )
            raise EmbeddingsUnavailableException(provider=exc.provider, reason=str(exc)) from exc

        if not response.vectors:
            raise EmbeddingsUnavailableException(
                provider=provider.name, reason="no_vector_returned"
            )
        q_vec = response.vectors[0].values
        # Sérialisation literal pgvector `[0.1,0.2,...]`.
        pgvec_literal = "[" + ",".join(str(x) for x in q_vec) + "]"

        # 5. SQL cosinus avec JOIN strict (IDOR-safe + exclusion deleted).
        # Clause file_ids conditionnelle pour ne binder le param que si
        # présent (SQLAlchemy text() n'aime pas un param inutilisé).
        file_ids_clause = ""
        bindparams: dict[str, object] = {
            "q_vec": pgvec_literal,
            "uid": str(user.id),
            "min_sim": float(min_similarity),
            "k": effective_k,
        }
        if file_ids:
            file_ids_clause = "AND dc.file_id = ANY(CAST(:file_ids AS uuid[]))"
            bindparams["file_ids"] = [str(fid) for fid in file_ids]

        sql = text(
            f"""
            SELECT
                dc.id AS id,
                dc.file_id AS file_id,
                dc.chunk_index AS chunk_index,
                dc.content AS content,
                dc.token_count AS token_count,
                dc.start_char_offset AS start_char_offset,
                dc.end_char_offset AS end_char_offset,
                dc.page_number AS page_number,
                uf.original_filename AS original_filename,
                uf.mime_type AS mime_type,
                1 - (dc.embedding <=> CAST(:q_vec AS vector)) AS similarity
            FROM document_chunks dc
            JOIN uploaded_files uf ON uf.id = dc.file_id
            WHERE dc.user_id = CAST(:uid AS uuid)
              AND uf.deleted_at IS NULL
              AND (1 - (dc.embedding <=> CAST(:q_vec AS vector))) >= :min_sim
              {file_ids_clause}
            ORDER BY dc.embedding <=> CAST(:q_vec AS vector)
            LIMIT :k
            """
        ).bindparams(**bindparams)

        result = await db.execute(sql)
        rows = result.mappings().all()

        chunks: list[RagChunkItem] = [
            RagChunkItem(
                id=int(r["id"]),
                file_id=r["file_id"],
                chunk_index=int(r["chunk_index"]),
                content=r["content"],
                token_count=int(r["token_count"]),
                start_char_offset=int(r["start_char_offset"]),
                end_char_offset=int(r["end_char_offset"]),
                page_number=r["page_number"],
                similarity=float(r["similarity"]),
                original_filename=r["original_filename"],
                mime_type=r["mime_type"],
            )
            for r in rows
        ]
        sources: list[RagSourceItem] = [
            RagSourceItem(
                file_id=c.file_id,
                chunk_index=c.chunk_index,
                start_char_offset=c.start_char_offset,
                end_char_offset=c.end_char_offset,
                page_number=c.page_number,
                similarity=c.similarity,
                original_filename=c.original_filename,
            )
            for c in chunks
        ]

        # 6. Framing anti-injection.
        framing = build_rag_framed_context(chunks)

        # 7. Log forensic (sans payload query — éviter de logger le
        # contenu user).
        log.info(
            "rag.query.completed",
            user_id=str(user.id),
            query_len=len(normalized),
            k=effective_k,
            min_similarity=float(min_similarity),
            n_chunks=len(chunks),
            file_ids_filter=bool(file_ids),
            embedding_model=provider.default_model,
        )

        return RagQueryResponse(
            chunks=chunks,
            sources=sources,
            framed_context=framing.framed_context,
            instruction=framing.instruction,
        )
