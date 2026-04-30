"""
Router RAG — endpoint public `/rag/query` (D5).

Un seul endpoint pour l'instant. L'intégration chat-RAG (`/chat/stream`
qui consomme `/rag/query` + préfixe le `framed_context + instruction`
au system prompt) fera l'objet d'une session ultérieure dédiée.

Discipline :
- Rate limit user-scoped **avant** toute écriture/embed (60/h/user via
  `check_user_rate_limit`). Distinct du `BudgetTracker` embeddings
  journalier — les deux cohabitent pour couper au plus tôt les
  boucles client buggées.
- Aucune logique métier ici — délégation stricte à `RagQueryService`.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import RateLimitAbuseException
from app.core.security.rate_limiter import check_user_rate_limit
from app.features.auth.models import User
from app.features.rag.schemas import RagQueryRequest, RagQueryResponse
from app.features.rag.service import RagQueryService
from app.shared.schemas import NexyaResponse

router = APIRouter(prefix="/rag", tags=["rag"])


@router.post(
    "/query",
    response_model=NexyaResponse[RagQueryResponse],
)
async def rag_query(
    body: RagQueryRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> NexyaResponse[RagQueryResponse]:
    """Recherche sémantique top-K dans les `document_chunks` de l'user.

    Retourne :
    - `chunks` : liste brute avec `content`, `offsets`, `page_number`,
      `similarity`, `original_filename`.
    - `sources` : équivalent allégé pour l'UI (sans `content`).
    - `framed_context` : chunks wrappés avec balises anti-prompt-injection,
      prêt à injecter dans un system prompt LLM.
    - `instruction` : ligne système à préfixer au `framed_context`.

    Consomme :
    - 1 crédit `BudgetTracker.check_and_consume_embeddings` (jour user).
    - 1 slot dans la fenêtre glissante `rag_query_rate_limit_per_hour`
      (défaut 60/h/user) → 429 `RATE_LIMIT_ABUSE` si saturée.
    """
    # Rate limit user-scoped (distinct du budget embeddings jour).
    await check_user_rate_limit(
        current_user.id,
        action="rag_query",
        max_requests=settings.rag_query_rate_limit_per_hour,
        window_seconds=3600,
        on_exceeded=RateLimitAbuseException,
    )

    result = await RagQueryService.query(
        current_user,
        db,
        query=body.query,
        k=body.k,
        min_similarity=body.min_similarity,
        file_ids=body.file_ids,
    )
    return NexyaResponse(success=True, data=result)
