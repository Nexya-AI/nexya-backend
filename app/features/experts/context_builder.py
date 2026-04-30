"""
Expert corpus context builder — injection RAG dans le system prompt (G1).

Miroir architectural de `app/features/memory/context_builder.py` (D3).
Appelé par le router `/chat/stream` AVANT le token estimator + la cache
key, pour que le cap 30 k tokens B2 tienne compte du bloc corpus.

Distinctions avec D3 memory :
- **Scope `expert_slug`** au lieu de `user` — pas de vecteur IDOR.
- **Framing D5** via `build_rag_framed_context` — réutilise la défense
  anti-prompt-injection (`<<<DOCUMENT EXTRACT>>>...<<<END EXTRACT N>>>`
  + `RAG_SYSTEM_INSTRUCTION`). Aligne la posture sécurité avec `/rag/query`.
- **`task_type='RETRIEVAL_QUERY'`** — l'embed query côté Gemini utilise
  une projection vectorielle optimisée pour la recherche (asymétrique
  vs `RETRIEVAL_DOCUMENT` à l'ingestion, gain mesurable sur retrieval).
- **Heuristique `language_pair_hint`** — expert Langues : détecte
  « traduis en espagnol » → hint `fra-spa` pour scoper le retrieval.
  Neutre sur les autres experts (None = tous les chunks).

Discipline (rappel D3) :
- **Fail-safe absolue** : toute exception → log warning + return None.
  Le chat ne doit JAMAIS être bloqué par un dysfonctionnement corpus.
- **Single Source of Truth** : ce module produit le bloc framé, la
  concat finale avec `config.system_prompt` se fait UNIQUEMENT dans
  `_stream_link` (`app/ai/streaming.py`).
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog

from app.config import settings
from app.features.experts.service import ExpertChunkResult, ExpertCorpusService
from app.features.files.rag_framing import build_rag_framed_context

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.ai.embeddings import EmbeddingsProvider

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Détection de paire de langues (heuristique FR → X)
# ══════════════════════════════════════════════════════════════
#
# Patterns volontairement conservateurs :
# - On matche UNIQUEMENT sur des verbes explicites (« traduis », « traduire »,
#   « en + langue »). Une question générique « bonjour comment ça va » ne
#   déclenche rien → None, pas de filtre `language_pair` → fallback sur tout
#   le corpus (recall maximal).
# - Couvre les 3 paires dominantes NEXYA : FR↔EN / FR↔ES / FR↔PT.
# - Langue source implicite = FR (l'user parle FR par défaut dans NEXYA
#   Afrique francophone). Un hint `en-XX` n'est jamais injecté — l'user
#   peut toujours poser la question en anglais, le LLM l'identifiera.

_LANGUAGE_HINTS: tuple[tuple[str, str], ...] = (
    (r"\b(en\s+espagnol|in\s+spanish|traduis.+espagnol)\b", "fra-spa"),
    (r"\b(en\s+anglais|in\s+english|traduis.+anglais)\b", "fra-eng"),
    (r"\b(en\s+portugais|in\s+portuguese|traduis.+portugais)\b", "fra-por"),
    (r"\b(en\s+fran[çc]ais|in\s+french|translate.+french)\b", "eng-fra"),
)


def _detect_language_pair_hint(query: str) -> str | None:
    """Retourne un hint `lang_src-lang_tgt` ou None.

    Matching case-insensitive. Renvoie le PREMIER match (ordre défini
    ci-dessus : espagnol avant anglais pour discriminer « en espagnol »
    vs « en espagnol et anglais »).
    """
    normalized = query.lower()
    for pattern, pair in _LANGUAGE_HINTS:
        if re.search(pattern, normalized):
            return pair
    return None


# ══════════════════════════════════════════════════════════════
# Formatage & troncature
# ══════════════════════════════════════════════════════════════

_TRUNCATION_MARKER = "\n\n[... corpus tronqué pour respecter la limite de taille]"


def _format_corpus_block(results: list[ExpertChunkResult], max_chars: int) -> str:
    """Compose le bloc système final : instruction D5 + extraits framés.

    Format :
        <RAG_SYSTEM_INSTRUCTION>

        <<<DOCUMENT EXTRACT id="1" file="language/tatoeba/fra-spa" chunk="42">>>
        [FR] J'apprends le français...
        [ES] Aprendo francés...
        <<<END EXTRACT 1>>>

        ...

    `build_rag_framed_context` duck-type sur `chunk.content`, `chunk.file_id`,
    `chunk.chunk_index`, `chunk.page_number`. On passe des `ExpertChunkResult`
    qui exposent `content`, on synthétise `file_id` à partir de
    `(expert_slug?, source, language_pair)` pour donner au LLM une
    identification claire de la source (« selon l'extrait 1 — corpus
    Tatoeba FR-ES, ... »).
    """

    # Wrapper léger pour alimenter le duck-typing du framing.
    # L'attribut `file_id` sert uniquement à l'affichage dans le
    # marqueur `<<<DOCUMENT EXTRACT file="..." >>>` — on le peuple
    # avec un identifiant lisible LLM-friendly.
    class _FramingAdapter:
        __slots__ = ("content", "file_id", "chunk_index", "page_number")

        def __init__(self, r: ExpertChunkResult) -> None:
            self.content = r.content
            lang_suffix = f"/{r.language_pair}" if r.language_pair else ""
            self.file_id = f"{r.source}{lang_suffix}"
            self.chunk_index = r.id
            self.page_number = None

    adapters = [_FramingAdapter(r) for r in results]
    framed = build_rag_framed_context(adapters)
    if not framed.framed_context:
        return ""

    block = framed.instruction + "\n\n" + framed.framed_context
    if len(block) <= max_chars:
        return block

    # Troncature : on coupe au dernier `<<<END EXTRACT N>>>` qui tient
    # dans le budget pour garder une structure parsable par le LLM.
    budget = max_chars - len(_TRUNCATION_MARKER)
    if budget <= len(framed.instruction) + 50:
        # Cap absurde — on ne peut même pas garder l'instruction + un
        # extrait complet. Renvoie juste l'instruction.
        return framed.instruction + _TRUNCATION_MARKER

    truncated = block[:budget]
    last_end_marker = truncated.rfind("<<<END EXTRACT ")
    if last_end_marker > 0:
        # Trouve le premier `>>>` après `<<<END EXTRACT `.
        close = truncated.find(">>>", last_end_marker)
        if close > 0:
            truncated = truncated[: close + 3]
    return truncated + _TRUNCATION_MARKER


# ══════════════════════════════════════════════════════════════
# API publique
# ══════════════════════════════════════════════════════════════


async def build_expert_corpus_context(
    *,
    expert_slug: str,
    query: str,
    db: AsyncSession,
    provider: EmbeddingsProvider | None = None,
    k: int | None = None,
    min_similarity: float | None = None,
    max_chars: int | None = None,
    language_pair_hint: str | None = None,
) -> str | None:
    """Retourne un bloc textuel prêt à injecter dans le system prompt, ou None.

    Pipeline :
    1. Short-circuit si `settings.expert_corpus_enabled=False`.
    2. Short-circuit si `query.strip() == ""` OU `expert_slug` vide.
    3. Heuristique `_detect_language_pair_hint` (expert Langues).
    4. `provider.embed([query], task_type='RETRIEVAL_QUERY')` → vecteur.
    5. `ExpertCorpusService.search(...)` → liste chunks.
    6. Framing D5 + troncature.
    7. Fail-safe absolue : exception → log warning + None.

    Args:
        expert_slug: scope corpus (ex: 'language').
        query: texte de recherche (typiquement dernier message user).
        db: session async.
        provider: override testabilité. None = `get_embeddings_provider()`.
        k: override `settings.expert_corpus_k=5`.
        min_similarity: override `settings.expert_corpus_min_similarity=0.7`.
        max_chars: override `settings.expert_corpus_max_chars=3000`.
        language_pair_hint: force un hint (bypass heuristique). None =
            heuristique appliquée, vide chaîne '' = désactive le filtre.

    Returns:
        Bloc markdown-like prêt à concaténer, ou `None`.
    """
    if not settings.expert_corpus_enabled:
        return None

    if not expert_slug:
        return None

    stripped_query = (query or "").strip()
    if not stripped_query:
        return None

    effective_k = k if k is not None else settings.expert_corpus_k
    effective_min_sim = (
        min_similarity if min_similarity is not None else settings.expert_corpus_min_similarity
    )
    effective_max_chars = max_chars if max_chars is not None else settings.expert_corpus_max_chars

    # Hint langue : si le caller a explicitement passé une chaîne vide,
    # on désactive le filtre (utile pour tests). None = applique
    # l'heuristique.
    if language_pair_hint is None:
        effective_lang = _detect_language_pair_hint(stripped_query)
    elif language_pair_hint == "":
        effective_lang = None
    else:
        effective_lang = language_pair_hint

    try:
        # Embed de la query — task_type=RETRIEVAL_QUERY côté Gemini pour
        # une projection asymétrique optimisée search. OpenAI/Mock
        # ignorent silencieusement le paramètre.
        if provider is None:
            from app.ai.embeddings import get_embeddings_provider  # noqa: PLC0415

            provider = get_embeddings_provider()
        embed_response = await provider.embed([stripped_query], task_type="RETRIEVAL_QUERY")
        if not embed_response.vectors:
            return None
        q_vec = embed_response.vectors[0].values

        # Premier essai : avec le filtre de paire détecté.
        results = await ExpertCorpusService.search(
            db,
            expert_slug=expert_slug,
            query_embedding=q_vec,
            k=effective_k,
            min_similarity=effective_min_sim,
            language_pair=effective_lang,
        )
        # Fallback gracieux : si filtre langue actif mais rien ne matche,
        # on relâche le filtre pour garder une chance de retrieval utile.
        if not results and effective_lang is not None:
            results = await ExpertCorpusService.search(
                db,
                expert_slug=expert_slug,
                query_embedding=q_vec,
                k=effective_k,
                min_similarity=effective_min_sim,
                language_pair=None,
            )
    except Exception as exc:  # noqa: BLE001 — fail-safe absolue
        log.warning(
            "experts.corpus.context_failed",
            expert_slug=expert_slug,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None

    if not results:
        return None

    block = _format_corpus_block(results, effective_max_chars)
    if not block:
        return None
    return block
