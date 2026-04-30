"""
Experts RAG package (G1).

Corpus globaux par expert — un « bien commun » de la plateforme qui enrichit
le system prompt de chaque mode expert activé via `ExpertConfig.corpus_enabled`.

Distinct des autres sous-systèmes RAG :
- D1/D3 `memory` : faits user-scope injectés dans tous les experts.
- D4/D5 `rag` + `files` : documents user-scope retrieval via `/rag/query`.
- G1 `experts` : **corpus système partagé** scopé par `expert_slug`.

Exports publics :
    ExpertCorpusChunk   ORM model (table expert_corpus_chunks)
    ExpertChunkResult   dataclass retour de `ExpertCorpusService.search`
    ExpertCorpusService service de retrieval SQL cosinus
    build_expert_corpus_context  helper d'injection system prompt
"""

from __future__ import annotations

from app.features.experts.context_builder import build_expert_corpus_context
from app.features.experts.models import ExpertCorpusChunk
from app.features.experts.service import ExpertChunkResult, ExpertCorpusService

__all__ = [
    "ExpertCorpusChunk",
    "ExpertChunkResult",
    "ExpertCorpusService",
    "build_expert_corpus_context",
]
