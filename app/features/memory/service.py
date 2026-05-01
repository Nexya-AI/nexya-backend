"""
MemoryStore — CRUD atomique + recherche cosinus pour la Mémoire IA.

Pattern NEXYA aligné `ConversationService` (C1/C2), `LibraryService` (C3),
`FileUploadService` (E3) : méthodes statiques, `AsyncSession` en paramètre,
commit en fin de méthode publique, 404 IDOR-safe partout.

Discipline :

- **Mock-first** : `MemoryStore.add` / `.search` utilisent
  `get_embeddings_provider()` qui bascule automatiquement sur Mock quand
  la clé OpenAI est absente. Permet à toute la chaîne (test, dev, CI)
  de tourner sans secret.

- **Dédup par content_sha256** : un re-add du même contenu par le même
  user retourne l'entrée existante via `INSERT ... ON CONFLICT DO
  NOTHING RETURNING` (même pattern Library C3 + E3). Économie directe
  mesurable côté facture OpenAI (pas de double appel API pour un
  contenu identique).

- **Normalisation content avant SHA** : trim + collapse whitespace
  interne. « Ivan est  dev  Flutter   » et « Ivan est dev Flutter »
  produisent le même SHA, donc la même mémoire — pas de pollution de
  la biblio par des variations de formatage.

- **Quota pré-flight + budget embeddings** : double garde-fou avant le
  moindre appel API. `MemoryQuotaExceededException` (402) si le user
  a atteint sa limite plan, `RateLimitExceededException` (429) si le
  budget embeddings/jour est épuisé. Aucun token brûlé inutilement.

- **RGPD `delete_for_user`** : hard DELETE physique (pas soft) car une
  demande RGPD explicite doit purger la donnée. Retour du count pour
  audit. À brancher sur `DELETE /user/account` existant (Phase J RGPD).

- **Recherche cosinus via pgvector `<=>`** : l'opérateur retourne une
  distance (`0 = identique, 2 = opposé`). On convertit en similarity
  `1 - distance` côté API pour que l'UX Flutter voie `[0..1]` (1 =
  parfait match). L'index HNSW `vector_cosine_ops` accélère la query
  en O(log N).

- **Contrat interne uniquement** : aucune exposition HTTP au D1. Les
  endpoints publics viendront en D5 (`/memory/search`, `/memory/index`,
  `DELETE /memory/{id}`).
"""

from __future__ import annotations

import base64
import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

import structlog
from sqlalchemy import delete, func, select, text, tuple_
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.budget_tracker import get_budget_tracker
from app.ai.embeddings import (
    EmbeddingsError,
    EmbeddingsProvider,
    get_embeddings_provider,
)
from app.config import settings
from app.core.errors.exceptions import (
    EmbeddingsUnavailableException,
    MemoryQuotaExceededException,
    ResourceNotFoundException,
    ValidationException,
)
from app.features.auth.models import User
from app.features.memory.models import Memory

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

_DEFAULT_SEARCH_K: Final[int] = 5
_MAX_SEARCH_K: Final[int] = 50
_WHITESPACE_PATTERN: Final[re.Pattern[str]] = re.compile(r"\s+")


# ══════════════════════════════════════════════════════════════
# DTO interne — résultat de recherche
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class MemoriesPage:
    """Page keyset retournée par `MemoryStore.list_for_user`.

    `next_cursor=None` signifie la dernière page. Sinon, le caller
    renvoie le curseur opaque au client pour le prochain appel.
    """

    items: list[Memory]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class MemorySearchResult:
    """Mémoire retournée par `search()` + score de similarité.

    `similarity` ∈ [-1, 1] mais en pratique [0, 1] pour nos embeddings
    normalisés L2 (OpenAI + Mock). `1.0 = parfait match`, `0.0 = orthogonal`.
    Le caller (D3, D5) filtre généralement `>= 0.7` pour n'injecter que
    des mémoires vraiment pertinentes dans le system prompt.
    """

    memory: Memory
    similarity: float


# ══════════════════════════════════════════════════════════════
# Helpers privés
# ══════════════════════════════════════════════════════════════


def _normalize_content(content: str) -> str:
    """Trim + collapse whitespace interne.

    « Ivan est  dev  Flutter   » → « Ivan est dev Flutter »

    Garantit que deux formulations équivalentes produisent le même
    content_sha256 → évite les mémoires « fantômes » dupliquées dans
    la biblio user.

    Garde la casse (« Flutter » != « flutter » côté sémantique — les
    noms propres, acronymes, etc. portent du sens).
    """
    stripped = content.strip()
    return _WHITESPACE_PATTERN.sub(" ", stripped)


def _content_sha256(normalized: str) -> str:
    """SHA-256 hex (64 chars) du contenu normalisé, encodage UTF-8."""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _memories_quota(user: User) -> tuple[int, str]:
    """Retourne `(max_memories, plan_label)` selon `user.is_pro`."""
    if user.is_pro:
        return settings.memory_max_pro, "pro"
    return settings.memory_max_free, "free"


# ══════════════════════════════════════════════════════════════
# Curseur keyset opaque — D5
# ══════════════════════════════════════════════════════════════
# Format `{iso}|{uuid}` encodé base64url. Aligné sur le pattern
# ConversationService / ProjectService / LibraryService.


def _encode_cursor(created_at: datetime, row_id: uuid.UUID) -> str:
    payload = f"{created_at.isoformat()}|{row_id}"
    return base64.urlsafe_b64encode(payload.encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Décode un curseur opaque. Lève `ValidationException` si malformé."""
    try:
        decoded = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValidationException("Curseur de pagination invalide.") from exc
    if "|" not in decoded:
        raise ValidationException("Curseur de pagination malformé.")
    iso_part, uuid_part = decoded.split("|", 1)
    try:
        created_at = datetime.fromisoformat(iso_part)
        row_id = uuid.UUID(uuid_part)
    except (ValueError, TypeError) as exc:
        raise ValidationException("Curseur de pagination invalide.") from exc
    return created_at, row_id


# ══════════════════════════════════════════════════════════════
# MemoryStore
# ══════════════════════════════════════════════════════════════


class MemoryStore:
    """CRUD + search cosinus pour les mémoires IA utilisateur.

    Toutes les méthodes sont statiques, `AsyncSession` en paramètre,
    commit en fin de chaque méthode publique (sauf `search` et
    `count_for_user` qui sont read-only).
    """

    # ── Owner check 404 IDOR-safe ──────────────────────────────
    @staticmethod
    async def _get_owned_memory(
        memory_id: uuid.UUID,
        user_id: uuid.UUID,
        db: AsyncSession,
    ) -> Memory:
        """Charge une mémoire active propriété de l'user — 404 sinon.

        Politique anti-énumération stricte alignée sur le reste du
        backend : jamais 403, toujours 404.
        """
        result = await db.execute(
            select(Memory).where(
                Memory.id == memory_id,
                Memory.user_id == user_id,
                Memory.deleted_at.is_(None),
            )
        )
        memory = result.scalar_one_or_none()
        if memory is None:
            raise ResourceNotFoundException("Mémoire")
        return memory

    # ── ADD — indexation d'une mémoire ──────────────────────────
    @staticmethod
    async def add(
        user: User,
        db: AsyncSession,
        *,
        content: str,
        source: str = "manual",
        source_conversation_id: uuid.UUID | None = None,
        source_message_id: uuid.UUID | None = None,
        importance: int = 1,
        metadata_json: dict | None = None,
        provider: EmbeddingsProvider | None = None,
    ) -> Memory:
        """Indexe un fait durable : embed + INSERT avec dédup SHA.

        Pipeline :
        1. Normalisation du content (trim + collapse whitespace).
        2. Validation longueur (CHECK SQL 1-2000 chars dupliqué Python
           pour message d'erreur propre avant le hit DB).
        3. SHA-256 du content normalisé.
        4. Quota pré-flight → 402 `MEMORY_QUOTA_EXCEEDED`.
        5. Budget embeddings pré-flight → 429 `RATE_LIMIT_EXCEEDED`.
        6. Appel `provider.embed([normalized])` → vecteur 1536 dim.
        7. `INSERT ... ON CONFLICT DO NOTHING RETURNING` — si conflit
           déclenché (dédup), SELECT existant et retour sans
           ré-embedding.

        Lève :
        - `ValidationException` (422) : content vide ou > 2000 chars.
        - `MemoryQuotaExceededException` (402) : plafond plan atteint.
        - `RateLimitExceededException` (429) : budget jour épuisé.
        - `EmbeddingsUnavailableException` (503) : provider down après
          retry. Dans ce cas aucun INSERT n'est tenté (pas d'orphelin
          DB sans embedding valide).
        """
        # 1+2. Normalisation + validation.
        normalized = _normalize_content(content)
        if not normalized:
            raise ValidationException("Le contenu de la mémoire ne peut pas être vide.")
        if len(normalized) > settings.embeddings_content_max_chars:
            raise ValidationException(
                f"Le contenu dépasse {settings.embeddings_content_max_chars} caractères."
            )

        # 3. SHA-256.
        sha = _content_sha256(normalized)

        # 4. Quota pré-flight.
        max_memories, plan_label = _memories_quota(user)
        count_stmt = select(func.count(Memory.id)).where(
            Memory.user_id == user.id,
            Memory.deleted_at.is_(None),
        )
        active_count = (await db.execute(count_stmt)).scalar_one() or 0
        if int(active_count) >= max_memories:
            raise MemoryQuotaExceededException(
                current=int(active_count), maximum=max_memories, plan=plan_label
            )

        # 5. Budget embeddings pré-flight (compteur Redis jour user).
        await get_budget_tracker().check_and_consume_embeddings(str(user.id), cost=1)

        # 6. Embed — un seul texte en batch.
        resolved_provider = provider or get_embeddings_provider()
        try:
            response = await resolved_provider.embed([normalized])
        except EmbeddingsError as exc:
            log.warning(
                "memory.add.embed_failed",
                user_id=str(user.id),
                provider=exc.provider,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise EmbeddingsUnavailableException(provider=exc.provider, reason=str(exc)) from exc

        if not response.vectors:
            # Ne devrait jamais arriver (provider garantit N vecteurs
            # pour N textes), mais garde-fou strict.
            raise EmbeddingsUnavailableException(
                provider=resolved_provider.name, reason="no_vector_returned"
            )
        vector = response.vectors[0]

        # 7. INSERT ON CONFLICT DO NOTHING RETURNING — dédup SHA.
        insert_stmt = (
            pg_insert(Memory)
            .values(
                user_id=user.id,
                content=normalized,
                content_sha256=sha,
                embedding=vector.values,
                embedding_model=vector.model,
                embedding_dim=vector.dim,
                source=source,
                source_conversation_id=source_conversation_id,
                source_message_id=source_message_id,
                importance=importance,
                metadata_json=metadata_json,
            )
            .on_conflict_do_nothing(
                index_elements=["user_id", "content_sha256"],
                index_where=Memory.deleted_at.is_(None),
            )
            .returning(Memory)
        )
        result = await db.execute(insert_stmt)
        memory = result.scalar_one_or_none()

        if memory is None:
            # Conflit déclenché — on récupère l'existant.
            existing = await db.execute(
                select(Memory).where(
                    Memory.user_id == user.id,
                    Memory.content_sha256 == sha,
                    Memory.deleted_at.is_(None),
                )
            )
            memory = existing.scalar_one_or_none()
            if memory is None:
                # Ne devrait jamais arriver (ON CONFLICT + WHERE actifs).
                raise ValidationException("Conflit inattendu sur l'enregistrement de la mémoire.")
            log.info(
                "memory.add.dedup_hit",
                user_id=str(user.id),
                memory_id=str(memory.id),
                sha=sha[:16],
            )
        else:
            await db.commit()
            await db.refresh(memory)
            log.info(
                "memory.added",
                user_id=str(user.id),
                memory_id=str(memory.id),
                source=source,
                importance=importance,
                content_len=len(normalized),
                model=vector.model,
                sha=sha[:16],
            )

        return memory

    # ── SEARCH — top-K cosinus ─────────────────────────────────
    @staticmethod
    async def search(
        user: User,
        db: AsyncSession,
        *,
        query: str,
        k: int = _DEFAULT_SEARCH_K,
        source: str | None = None,
        min_similarity: float = 0.0,
        provider: EmbeddingsProvider | None = None,
    ) -> list[MemorySearchResult]:
        """Retourne les K mémoires sémantiquement les plus proches de `query`.

        Pipeline :
        1. Validation `k` ∈ [1, 50].
        2. Normalisation query + embed (coûte 1 crédit embeddings).
        3. Requête SQL cosinus :
           ```
           SELECT *, 1 - (embedding <=> :q_vec) AS similarity
           FROM memories
           WHERE user_id = :uid AND deleted_at IS NULL
             AND (source = :source OR :source IS NULL)
             AND 1 - (embedding <=> :q_vec) >= :min_sim
           ORDER BY embedding <=> :q_vec
           LIMIT :k
           ```
        4. Retour liste `MemorySearchResult(memory, similarity)` triée
           par similarité décroissante.

        Consomme 1 crédit embeddings au compteur jour (on paie le query
        embed comme un add — même politique mock-first).
        """
        # 1. Validation k.
        effective_k = max(1, min(k, _MAX_SEARCH_K))

        # 2. Normalisation + budget + embed.
        normalized = _normalize_content(query)
        if not normalized:
            raise ValidationException("La requête de recherche ne peut pas être vide.")

        await get_budget_tracker().check_and_consume_embeddings(str(user.id), cost=1)

        resolved_provider = provider or get_embeddings_provider()
        try:
            response = await resolved_provider.embed([normalized])
        except EmbeddingsError as exc:
            log.warning(
                "memory.search.embed_failed",
                user_id=str(user.id),
                provider=exc.provider,
                error=str(exc),
            )
            raise EmbeddingsUnavailableException(provider=exc.provider, reason=str(exc)) from exc

        query_vec = response.vectors[0].values

        # 3. Requête SQL cosinus.
        # On utilise `text()` + bindparams parce que l'opérateur `<=>`
        # pgvector n'est pas nativement exposé par SQLAlchemy ORM —
        # même pattern que C1 pour `plainto_tsquery`. Le bindparam
        # `q_vec` est un `list[float]` que le dialect pgvector
        # sérialise automatiquement via `Vector(1536)`.
        #
        # Conversion du list[float] en string format pgvector
        # `[0.1,0.2,...]` — la session DB + pgvector psycopg l'accepte
        # directement via la colonne typée Vector.
        pgvec_literal = "[" + ",".join(str(x) for x in query_vec) + "]"

        # Note : on construit la clause `(source = :src OR :src IS NULL)`
        # en branchant conditionnellement côté Python pour éviter de
        # binder 2 fois le param — SQLAlchemy n'aime pas toujours ça
        # avec text() et cast PostgreSQL.
        source_clause = ""
        bindparams: dict[str, object] = {
            "user_id": user.id,
            "q_vec": pgvec_literal,
            "min_sim": min_similarity,
            "k": effective_k,
        }
        if source is not None:
            source_clause = "AND source = :source"
            bindparams["source"] = source

        # nosec B608 — `source_clause` est une constante littérale construite
        # côté serveur (jamais user input). Tous les vrais paramètres user
        # passent par `.bindparams(**bindparams)` (sécurisé psycopg).
        sql = text(
            f"""
            SELECT
                id, user_id, content, content_sha256, embedding,
                embedding_model, embedding_dim, source,
                source_conversation_id, source_message_id,
                importance, metadata_json,
                deleted_at, created_at, updated_at,
                1 - (embedding <=> CAST(:q_vec AS vector)) AS similarity
            FROM memories
            WHERE user_id = :user_id
              AND deleted_at IS NULL
              {source_clause}
              AND 1 - (embedding <=> CAST(:q_vec AS vector)) >= :min_sim
            ORDER BY embedding <=> CAST(:q_vec AS vector)
            LIMIT :k
            """  # nosec B608
        ).bindparams(**bindparams)

        result = await db.execute(sql)
        rows = result.mappings().all()

        # Hydrate en Memory + similarity.
        # On passe par un SELECT ORM secondaire pour profiter des types
        # (Vector → list[float], JSONB → dict) sans parser à la main.
        ids = [row["id"] for row in rows]
        if not ids:
            return []

        mem_result = await db.execute(select(Memory).where(Memory.id.in_(ids)))
        by_id = {m.id: m for m in mem_result.scalars().all()}

        # Préserve l'ordre de tri SQL (par distance croissante).
        search_results: list[MemorySearchResult] = []
        for row in rows:
            memory = by_id.get(row["id"])
            if memory is None:
                continue  # row supprimée entre les 2 queries (très rare)
            search_results.append(
                MemorySearchResult(memory=memory, similarity=float(row["similarity"]))
            )
        log.debug(
            "memory.search.done",
            user_id=str(user.id),
            k=effective_k,
            hits=len(search_results),
            min_similarity=min_similarity,
        )
        return search_results

    # ── GET BY ID ────────────────────────────────────────────────
    @staticmethod
    async def get_for_user(
        memory_id: uuid.UUID,
        user: User,
        db: AsyncSession,
    ) -> Memory:
        """Retourne une mémoire active possédée par l'user — 404 sinon."""
        return await MemoryStore._get_owned_memory(memory_id, user.id, db)

    # ── SOFT DELETE ──────────────────────────────────────────────
    @staticmethod
    async def soft_delete(
        memory_id: uuid.UUID,
        user: User,
        db: AsyncSession,
    ) -> None:
        """Marque `deleted_at = NOW()` + commit. 404 IDOR-safe."""
        memory = await MemoryStore._get_owned_memory(memory_id, user.id, db)
        now = datetime.now(UTC)
        memory.deleted_at = now
        memory.updated_at = now
        await db.commit()
        log.info(
            "memory.soft_deleted",
            user_id=str(user.id),
            memory_id=str(memory.id),
        )

    # ── DELETE FOR USER (RGPD) ───────────────────────────────────
    @staticmethod
    async def delete_for_user(
        user: User,
        db: AsyncSession,
    ) -> int:
        """RGPD : hard DELETE de toutes les mémoires de l'user.

        Utilisé par `DELETE /user/account` (Phase J RGPD). Retourne le
        count supprimé pour l'audit log. Pas de soft-delete ici — une
        demande RGPD explicite DOIT purger la donnée physiquement.
        """
        result = await db.execute(
            delete(Memory).where(Memory.user_id == user.id).returning(Memory.id)
        )
        deleted_ids = list(result.scalars().all())
        await db.commit()
        log.info(
            "memory.rgpd_purged",
            user_id=str(user.id),
            count=len(deleted_ids),
        )
        return len(deleted_ids)

    # ── COUNT FOR USER (utilitaire quotas + dashboard) ───────────
    @staticmethod
    async def count_for_user(
        user: User,
        db: AsyncSession,
    ) -> int:
        """Retourne le nombre de mémoires actives de l'user."""
        result = await db.execute(
            select(func.count(Memory.id)).where(
                Memory.user_id == user.id,
                Memory.deleted_at.is_(None),
            )
        )
        return int(result.scalar_one() or 0)

    # ══════════════════════════════════════════════════════════════
    # D5 — extensions endpoints publics
    # ══════════════════════════════════════════════════════════════

    # ── LIST FOR USER (keyset pagination) ───────────────────────
    @staticmethod
    async def list_for_user(
        user: User,
        db: AsyncSession,
        *,
        cursor: str | None = None,
        limit: int = 20,
        source: str | None = None,
    ) -> MemoriesPage:
        """Liste paginée keyset `(created_at, id) DESC` des mémoires actives.

        - `cursor` opaque base64 `{iso}|{uuid}`. `None` pour le 1er appel.
        - `limit` clampé [1, 50].
        - `source` ∈ `{manual, extracted, imported, system}` optionnel.

        Retourne `MemoriesPage(items, next_cursor)` — `next_cursor=None`
        si plus de pages. Un curseur malformé lève `ValidationException`
        (422), aligné pattern ConversationService / ProjectService.
        """
        effective_limit = max(1, min(int(limit or 20), 50))

        conditions: list = [
            Memory.user_id == user.id,
            Memory.deleted_at.is_(None),
        ]
        if source is not None:
            conditions.append(Memory.source == source)

        if cursor:
            cur_created_at, cur_id = _decode_cursor(cursor)
            # Keyset DESC : rows plus anciennes que le curseur.
            # SQL tuple-compare via `tuple_()` — SQLAlchemy émet la forme
            # `(col1, col2) < (:v1, :v2)` que Postgres sait optimiser.
            conditions.append(tuple_(Memory.created_at, Memory.id) < tuple_(cur_created_at, cur_id))

        stmt = (
            select(Memory)
            .where(*conditions)
            .order_by(Memory.created_at.desc(), Memory.id.desc())
            .limit(effective_limit + 1)  # +1 pour savoir s'il reste
        )
        result = await db.execute(stmt)
        rows = list(result.scalars().all())

        has_more = len(rows) > effective_limit
        items = rows[:effective_limit]

        next_cursor: str | None = None
        if has_more and items:
            last = items[-1]
            next_cursor = _encode_cursor(last.created_at, last.id)

        return MemoriesPage(items=items, next_cursor=next_cursor)

    # ── DELETE ONE (hard-delete RGPD unitaire) ──────────────────
    @staticmethod
    async def delete_one_for_user(
        user: User,
        db: AsyncSession,
        *,
        memory_id: uuid.UUID,
    ) -> int:
        """Hard-delete physique d'une mémoire — RGPD Article 17.

        Idempotent : retourne 0 si déjà absente, 1 si supprimée. Le
        caller (router) renvoie 204 dans les deux cas pour ne pas
        révéler l'existence/absence à un attaquant.

        Politique hard-delete (vs soft-delete) : RGPD Article 17 exige
        une suppression effective, pas une rétention masquée. `soft_delete`
        reste disponible pour un usage interne (corbeille éventuelle),
        mais l'endpoint public expose cette version physique.
        """
        result = await db.execute(
            delete(Memory)
            .where(Memory.id == memory_id, Memory.user_id == user.id)
            .returning(Memory.id)
        )
        deleted_ids = list(result.scalars().all())
        await db.commit()
        log.info(
            "memory.delete_one",
            user_id=str(user.id),
            memory_id=str(memory_id),
            deleted=len(deleted_ids),
        )
        return len(deleted_ids)
