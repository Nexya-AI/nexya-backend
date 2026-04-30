"""
Memory context builder — ferme la boucle Bloc D (D1 indexe → D2 extrait → D3 injecte).

Ce module encapsule la logique de récupération et de formatage des mémoires
en un bloc textuel injectable dans le `system_prompt` du LLM avant chaque
appel `/chat/stream`. Il est consommé **uniquement** par le router chat —
aucun endpoint HTTP public n'est exposé.

Discipline :

- **Fail-safe absolue** : une erreur dans MemoryStore.search (pgvector
  lent, embeddings down, quota embeddings dépassé) retourne `None`.
  Le chat ne doit JAMAIS être bloqué par un dysfonctionnement mémoire.
  Le user préfère une réponse sans contexte mémoire à une réponse
  impossible.

- **Seuil `min_similarity=0.7`** par défaut — filtre les mémoires
  tangentielles. Un fait à similarité 0.3 sur la query courante
  pollue le contexte LLM plus qu'il n'aide.

- **Cap `max_chars=2000`** sur le bloc total — protège contre l'explosion
  du prompt si les memories sont longues ou nombreuses. Troncature avec
  marqueur `[...]` pour signaler au LLM qu'il y a plus de contexte.

- **Format structuré avec instructions d'usage** — le bloc ne contient
  pas juste les faits, il dit au LLM **quoi en faire** (« utilise-les
  si pertinent, ne les mentionne pas explicitement sauf si l'utilisateur
  demande »). Évite le comportement « l'IA radote vos infos à chaque
  message ».

- **Single Source of Truth** — ce module formate le bloc prêt-à-coller,
  mais la concat avec `config.system_prompt` se fait UNIQUEMENT dans
  `_stream_link` (streaming.py). Le router propage via `StreamContext.
  memory_context`. Pas deux endroits qui composent différemment.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Final

import structlog

from app.config import settings
from app.features.memory.service import MemorySearchResult, MemoryStore

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.ai.embeddings import EmbeddingsProvider
    from app.features.auth.models import User

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes de formatage
# ══════════════════════════════════════════════════════════════

# En-tête du bloc mémoire — explique au LLM ce qu'il voit et quoi en
# faire. Volontairement en FR : le projet NEXYA cible l'Afrique
# francophone, les LLM comprennent tous parfaitement le FR.
_BLOCK_HEADER: Final[str] = (
    "[Contexte sur l'utilisateur]\n"
    "Voici quelques faits durables que tu sais sur l'utilisateur. "
    "Utilise-les uniquement s'ils sont pertinents pour sa question actuelle. "
    "Ne les mentionne pas explicitement sauf si l'utilisateur te demande "
    "ce que tu sais de lui.\n\n"
)
_BLOCK_FOOTER: Final[str] = "\n[/Contexte]"

# Marqueur de troncature lisible par le LLM.
_TRUNCATION_MARKER: Final[str] = "\n[... contexte tronqué pour respecter la limite de taille]"


# ══════════════════════════════════════════════════════════════
# API publique
# ══════════════════════════════════════════════════════════════


async def build_memory_context(
    user: User,
    db: AsyncSession,
    *,
    query: str,
    k: int | None = None,
    min_similarity: float | None = None,
    provider: EmbeddingsProvider | None = None,
) -> str | None:
    """Retourne un bloc textuel prêt à injecter dans le system prompt, ou None.

    Pipeline :
    1. Short-circuit si `settings.memory_injection_enabled=False`.
    2. Short-circuit si `query.strip() == ""`.
    3. Appel `MemoryStore.search(user, db, query=query, k=k,
         min_similarity=min_similarity, provider=provider)`.
    4. Si liste vide → `None`.
    5. Formate via `_format_memories_block`.
    6. Fail-safe absolue : toute exception → log warning + return None.

    Args:
        user: utilisateur propriétaire des memories (scope user strict).
        db: session async en cours.
        query: texte de recherche (typiquement dernier message user).
        k: override `settings.memory_injection_k=5`.
        min_similarity: override `settings.memory_injection_min_similarity=0.7`.
        provider: injection testabilité — utile pour forcer le
            MockEmbeddingsProvider dans les tests.

    Returns:
        Bloc textuel formaté (markdown-like) ou `None` si rien à injecter.
    """
    if not settings.memory_injection_enabled:
        return None

    stripped_query = (query or "").strip()
    if not stripped_query:
        return None

    effective_k = k if k is not None else settings.memory_injection_k
    effective_min_sim = (
        min_similarity if min_similarity is not None else settings.memory_injection_min_similarity
    )

    try:
        results = await MemoryStore.search(
            user,
            db,
            query=stripped_query,
            k=effective_k,
            min_similarity=effective_min_sim,
            provider=provider,
        )
    except Exception as exc:  # noqa: BLE001 — fail-safe absolue
        # Pgvector lent, embeddings API down, quota embeddings dépassé, etc.
        # Le chat ne doit JAMAIS être bloqué par la mémoire.
        log.warning(
            "memory.context.search_failed",
            user_id=str(user.id),
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None

    if not results:
        return None

    return _format_memories_block(results)


# ══════════════════════════════════════════════════════════════
# Helper — formatage du bloc
# ══════════════════════════════════════════════════════════════


def _format_memories_block(results: list[MemorySearchResult]) -> str:
    """Formate une liste de `MemorySearchResult` en bloc markdown.

    Format :
        [Contexte sur l'utilisateur]
        Voici quelques faits ... (instructions d'usage LLM)

        - L'utilisateur est dev Flutter (pertinence: 0.92)
        - L'utilisateur habite au Cameroun (pertinence: 0.85)
        - L'utilisateur travaille sur NEXYA (pertinence: 0.71)
        [/Contexte]

    Les results sont supposés déjà triés par similarité décroissante
    (c'est le contrat de `MemoryStore.search`). On ne re-trie pas.

    Troncature : si le bloc final dépasse `settings.memory_injection_max_chars`,
    on coupe et on ajoute un marqueur `[...]` lisible par le LLM.
    """
    if not results:
        # Sanity : ne devrait pas arriver car l'appelant filtre déjà,
        # mais garde-fou défensif.
        return ""

    # Construction des lignes de faits, une par mémoire.
    lines: list[str] = []
    for result in results:
        content = result.memory.content.strip()
        similarity = result.similarity
        # Format `- <fait> (pertinence: 0.XX)` — précision 2 décimales,
        # suffisant pour que le LLM pondère l'usage sans surcharger.
        lines.append(f"- {content} (pertinence: {similarity:.2f})")

    body = "\n".join(lines)
    block = _BLOCK_HEADER + body + _BLOCK_FOOTER

    # Troncature si dépassement du cap applicatif. On préserve l'en-tête
    # et on tronque le body, en préfixant le marqueur au footer pour
    # garder une structure parsable par le LLM.
    max_chars = settings.memory_injection_max_chars
    if len(block) <= max_chars:
        return block

    # Budget pour le body = max_chars - len(header) - len(footer) -
    # len(truncation_marker). On s'arrête au dernier `\n` pour ne pas
    # couper une ligne à mi-chemin.
    budget = max_chars - len(_BLOCK_HEADER) - len(_BLOCK_FOOTER) - len(_TRUNCATION_MARKER)
    if budget <= 0:
        # Cap absurde (inférieur à l'overhead) — on renvoie juste
        # l'en-tête + footer minimal. Ne devrait jamais arriver avec
        # `max_chars=2000` et overhead ~300 chars.
        return _BLOCK_HEADER.rstrip() + _BLOCK_FOOTER

    truncated_body = body[:budget]
    # Coupe au dernier `\n` pour garder des lignes complètes.
    last_newline = truncated_body.rfind("\n")
    if last_newline > 0:
        truncated_body = truncated_body[:last_newline]

    return _BLOCK_HEADER + truncated_body + _TRUNCATION_MARKER + _BLOCK_FOOTER
