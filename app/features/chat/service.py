"""
ConversationService — logique métier du fil de chat persisté NEXYA.

Calqué sur `features/auth/service.py` : logique stateless, commit en fin de
méthode publique, logs structlog structurés, exceptions typées du catalogue.

Points critiques (ne pas contourner sans raison sérieuse) :

- **Isolation cross-user (IDOR).** Toute opération qui touche une conversation
  précise passe par `_get_owned_conversation()`. En cas de mismatch user_id,
  on lève `ResourceNotFoundException` (404), jamais `PermissionDenied` (403).
  403 révèle déjà que la ressource existe — c'est une fuite d'information
  exploitable pour énumérer des UUID valides.

- **Pagination cursor-based (keyset).** Jamais d'`OFFSET SQL`. Le curseur
  encode `(sort_ts, id)` en base64 opaque. Postgres utilise l'index composite
  pour un `(a, b) < (x, y)` en un seul seek, là où `OFFSET N` scanne et jette
  les N premières lignes — coût linéaire catastrophique au-delà de 10k.

- **Tri conversations : `COALESCE(last_message_at, created_at)`.**
  `last_message_at` peut être NULL (conv créée mais jamais ouverte). Un
  `ORDER BY last_message_at DESC NULLS LAST` ne se combine pas proprement
  avec un keyset `(last_message_at, id) < (...)` — Postgres ne sait pas
  comparer un tuple contenant NULL. Le COALESCE supprime le NULL en amont :
  une conv neuve apparaît à sa date de création (comportement UI attendu).
  Si le volume explose, un index fonctionnel sur l'expression COALESCE
  rattrape la perte de l'index natif sur `last_message_at` — à évaluer
  après quelques kilo-utilisateurs, pas avant.

- **Compteurs dénormalisés (`message_count`, `last_message_at`).** Mis à
  jour par `_bump_counters()` via `UPDATE ... SET col = col + 1, last = NOW()`
  en un seul aller-retour SQL. Jamais de lecture-puis-écriture côté service :
  deux streams concurrents sur la même conv produiraient un compteur faux
  (race condition au niveau application, gagnée par le dernier writer).
  Postgres sérialise l'UPDATE au niveau ligne, l'atomicité est gratuite.

- **`_bump_counters` ne commit pas.** Le Lot 4 (refactor /chat/stream
  persisté) l'appellera DANS la même transaction que l'INSERT du message
  assistant, pour garantir l'atomicité « insertion + incrément » — sinon,
  sur crash entre les deux, on a compteur=3 pour 2 messages en DB.
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Final

import structlog
from sqlalchemy import exists as sa_exists
from sqlalchemy import func, or_, select, text, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.providers import ChatMessage as AiChatMessage
from app.core.errors.exceptions import (
    DuplicateReportException,
    ResourceNotFoundException,
    ValidationException,
)
from app.features.auth.models import User
from app.features.chat.models import AbuseReport, Conversation, Message
from app.features.chat.schemas import (
    AbuseReportCreate,
    ConversationCreate,
    ConversationUpdate,
)

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

_DEFAULT_LIMIT: Final[int] = 20
_MAX_LIMIT: Final[int] = 50
_CURSOR_SEP: Final[str] = "|"

# Taille du contexte (en messages) rejoué à l'IA quand le Flutter envoie un
# `conversation_id` au lieu d'un `history` inline. 30 = ~15 tours utilisateur-
# assistant, suffisant pour la cohérence conversationnelle sans faire exploser
# la facture de tokens sur des fils longs.
_CONTEXT_MESSAGES_DEFAULT: Final[int] = 30


# ══════════════════════════════════════════════════════════════
# DTO internes — service ↔ router
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ConversationsPageOrm:
    """Page paginée de conversations (ORM) + curseur vers la page suivante.

    Suffixe `Orm` pour éviter la collision avec `ConversationsPage` Pydantic
    (cf. `schemas.py`) — le service parle ORM, le router parle Pydantic.
    """

    items: list[Conversation]
    next_cursor: str | None


@dataclass(frozen=True, slots=True)
class MessagesPageOrm:
    """Page paginée de messages (ORM) + curseur vers la page suivante.

    Suffixe `Orm` pour éviter la collision avec `MessagesPage` Pydantic
    (cf. `schemas.py`) — le service parle ORM, le router parle Pydantic.
    """

    items: list[Message]
    next_cursor: str | None


# ══════════════════════════════════════════════════════════════
# Helpers curseur — opaques côté client
# ══════════════════════════════════════════════════════════════


def _encode_cursor(sort_ts: datetime, row_id: uuid.UUID) -> str:
    """Encode `(sort_ts, row_id)` en base64 urlsafe — opaque côté client.

    Le format interne `{iso}|{uuid}` n'est PAS un contrat public. Le client
    reçoit la chaîne, la renvoie telle quelle via `?cursor=...`. Base64 ici
    sert uniquement à décourager les tentatives de forge (et à éviter les
    collisions avec les caractères réservés URL).
    """
    raw = f"{sort_ts.isoformat()}{_CURSOR_SEP}{row_id}"
    return base64.urlsafe_b64encode(raw.encode("ascii")).decode("ascii")


def _decode_cursor(cursor: str) -> tuple[datetime, uuid.UUID]:
    """Décode un curseur en `(sort_ts, row_id)`.

    Un curseur invalide lève `ValidationException` (422). Cela couvre quatre
    cas : base64 cassé, encodage non-ASCII, séparateur absent, ISO ou UUID
    non parsable. Le log garde un aperçu tronqué pour le debug sans exposer
    le curseur entier (qui pourrait venir d'un lien partagé).
    """
    try:
        raw = base64.urlsafe_b64decode(cursor.encode("ascii")).decode("ascii")
        iso, sep, row_id_str = raw.partition(_CURSOR_SEP)
        if not sep or not iso or not row_id_str:
            raise ValueError("cursor missing fields")
        sort_ts = datetime.fromisoformat(iso)
        if sort_ts.tzinfo is None:
            sort_ts = sort_ts.replace(tzinfo=UTC)
        row_id = uuid.UUID(row_id_str)
    except (binascii.Error, UnicodeDecodeError, ValueError, TypeError) as exc:
        log.warning("chat.cursor.invalid", cursor=cursor[:40], error=str(exc))
        raise ValidationException("Curseur de pagination invalide.") from exc
    return sort_ts, row_id


def _clamp_limit(limit: int | None) -> int:
    """Borne le `limit` demandé dans `[1, _MAX_LIMIT]`, défaut `_DEFAULT_LIMIT`."""
    if limit is None or limit <= 0:
        return _DEFAULT_LIMIT
    return min(limit, _MAX_LIMIT)


# ══════════════════════════════════════════════════════════════
# ConversationService — namespace de la logique métier Chat
# ══════════════════════════════════════════════════════════════


class ConversationService:
    """Logique métier Chat — CRUD des conversations + lecture paginée des messages.

    Toutes les méthodes sont `@staticmethod` : aucun état injecté par DI,
    l'`AsyncSession` traverse en paramètre. Regroupement sous une classe
    pour exposer un namespace explicite `ConversationService.<méthode>`
    aux routers, sans renoncer à la légèreté des fonctions pures.
    """

    # ── Helper d'isolation — seul rempart IDOR ──────────────────
    @staticmethod
    async def _get_owned_conversation(
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        db: AsyncSession,
    ) -> Conversation:
        """Charge une conversation dont l'utilisateur courant est propriétaire.

        Contrat strict :
        - `WHERE id = :id AND user_id = :user AND deleted_at IS NULL`
        - Aucune correspondance → `ResourceNotFoundException` (404).
          Jamais 403 : on ne révèle pas l'existence d'une ressource à
          quelqu'un qui n'y a pas droit. Un scanner qui teste des UUID ne
          peut pas distinguer « n'existe pas » de « pas à vous ».

        À appeler au début de TOUTE méthode qui opère sur une conversation
        cible (get, update, soft_delete, list_messages, insertion future
        d'un message). Oublier cet appel = ouvrir une IDOR.
        """
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
        conversation = result.scalar_one_or_none()
        if conversation is None:
            raise ResourceNotFoundException("Conversation")
        return conversation

    # ── Helper d'isolation — version corbeille (symétrique) ──────
    @staticmethod
    async def _get_owned_conversation_in_trash(
        conversation_id: uuid.UUID,
        user_id: uuid.UUID,
        db: AsyncSession,
    ) -> Conversation:
        """Charge une conversation **soft-deletée** dont l'user est propriétaire.

        Miroir strict de `_get_owned_conversation` pour les opérations qui
        agissent SUR la corbeille (`restore`, `permanent_delete`,
        `list_trash_for_user`). Le filtre est inversé :

        - `WHERE id = :id AND user_id = :user AND deleted_at IS NOT NULL`
        - Aucune correspondance → `ResourceNotFoundException` (404).

        Pourquoi un helper dédié plutôt que bypasser le filtre du helper
        normal ? Parce que les deux mondes (actif / corbeille) doivent rester
        étanches : un restore ou un purge ne doit JAMAIS pouvoir toucher
        une conversation active par accident (ex : bug de callsite qui
        passerait un id non-supprimé). Le filtre `IS NOT NULL` dans le
        WHERE garantit l'isolation des deux états, même en cas d'erreur
        humaine en amont. Même politique anti-énumération que l'helper
        principal : 404 et jamais 403.
        """
        result = await db.execute(
            select(Conversation).where(
                Conversation.id == conversation_id,
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_not(None),
            )
        )
        conversation = result.scalar_one_or_none()
        if conversation is None:
            raise ResourceNotFoundException("Conversation")
        return conversation

    # ── CREATE ───────────────────────────────────────────────────
    @staticmethod
    async def create(
        body: ConversationCreate,
        user: User,
        db: AsyncSession,
    ) -> Conversation:
        """Crée une nouvelle conversation pour l'utilisateur courant.

        `title` reste nullable à la création — le worker arq du Lot 5
        remplira automatiquement après le premier échange complet.
        `expert_id` par défaut `'general'` (aligné sur le serveur_default
        de la colonne).

        `body.project_id` (D3 — 2026-05-04) : si fourni, l'ownership du
        projet est validé via `ProjectService._get_owned_project` qui
        lève `ResourceNotFoundException("Projet")` 404 IDOR-safe en cas
        de mismatch user (jamais 403 — anti-énumération UUID, pattern
        aligné `_get_owned_conversation` + `_get_owned_message`).

        Le `project_id` est ensuite persisté sur la nouvelle ligne — la
        FK `conversations.project_id ON DELETE SET NULL` (migration 006)
        détache automatiquement la conv si le projet est purgé physiquement
        (RGPD Article 17). Le soft-delete projet C2 fait l'équivalent via
        UPDATE explicite côté `ProjectService.soft_delete`.

        **Anti-MissingGreenlet** (4ᵉ occurrence du pattern, voir §15
        entrée 2026-04-21 ReportService + 2026-05-03 ProjectService.create
        + ProjectService.update) : on capture `user_id_str = str(user.id)`
        AVANT `db.commit()` pour que le log forensic n'accède plus aux
        attributs ORM expirés en cas de rollback (un `IntegrityError`
        sur la FK `project_id` invalide post-rollback expirerait
        `user.id` et le lazy-load déclencherait `pool_pre_ping` →
        setter sync sur connexion async → MissingGreenlet 500).
        """
        # Local import pour casser le cycle d'import projects ↔ chat.
        # Le module `app.features.projects.service` importe directement
        # `Conversation` (cf. UPDATE conversations SET project_id=NULL
        # dans soft_delete) — un import top-level ici déclencherait un
        # ImportError circulaire au boot.
        from app.features.projects.service import ProjectService

        # Capture en str AVANT commit (anti-MissingGreenlet post-rollback).
        user_id_str = str(user.id)

        # Validation ownership projet AVANT INSERT — un projet inconnu
        # ou pas owner → 404 IDOR-safe + zéro écriture DB.
        if body.project_id is not None:
            await ProjectService._get_owned_project(body.project_id, user.id, db)

        conversation = Conversation(
            user_id=user.id,
            title=body.title,
            expert_id=body.expert_id or "general",
            project_id=body.project_id,
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
        log.info(
            "chat.conversation.created",
            user_id=user_id_str,
            conversation_id=str(conversation.id),
            expert_id=conversation.expert_id,
            project_id=str(body.project_id) if body.project_id else None,
        )
        return conversation

    # ── LIST (paginée cursor-based, DESC) ───────────────────────
    @staticmethod
    async def list_for_user(
        user: User,
        db: AsyncSession,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        is_archived: bool = False,
        is_favorite: bool | None = None,
        expert_id: str | None = None,
        q: str | None = None,
        project_id: uuid.UUID | None = None,
    ) -> ConversationsPageOrm:
        """Liste paginée des conversations d'un utilisateur.

        Sémantique des filtres :
        - `is_archived=False` (défaut) : onglet principal du Flutter.
          `is_archived=True` : onglet Archivées.
        - `is_favorite=None` (défaut) : pas de filtre. `True` ou `False` :
          favoris seuls / non-favoris seuls.
        - `expert_id=None` (défaut) : toutes expertises confondues. Valeur
          explicite (`'general'`, `'informatique'`, etc.) : conversations
          liées à ce mode expert uniquement. Utile pour les écrans « par
          expertise » côté Flutter — évite de charger puis filtrer côté
          client sur un historique potentiellement lourd.
        - `q=None` (défaut) : pas de recherche. Non-vide après strip : une
          conversation matche si **son titre contient `q`** (ILIKE `%q%`
          accéléré par l'index GIN trigram `idx_conversations_title_trgm`)
          **ou** si **au moins un de ses messages** (non supprimé) matche
          `plainto_tsquery('french', q)` via la colonne générée
          `messages.search_vector` (index GIN `idx_messages_search_vector`).
          La sous-requête `EXISTS` court-circuite dès le premier match et
          évite un JOIN + DISTINCT qui casserait le keyset.
          Tri inchangé : **on ne trie pas par `ts_rank`** pour préserver
          le contrat du curseur (même clé de tri quelle que soit `q`).
          Une itération ultérieure pourra offrir `?sort=relevance` en
          remplaçant la clé de tri par `ts_rank` *et* le format du curseur.
        - `project_id=None` (défaut) : pas de filtre projet. UUID explicite :
          ne renvoie que les conversations attachées à ce projet (Session C2
          — endpoint `GET /projects/{id}/conversations`). L'index partiel
          `idx_conversations_project` rend ce filtre O(log N).

        Algorithme keyset :
        - Tri sur `COALESCE(last_message_at, created_at) DESC, id DESC`.
        - On récupère `N+1` lignes : si la `(N+1)ᵉ` revient, on sait qu'il
          existe une page suivante et on encode un curseur sur la N-ième
          (l'extra n'est pas retourné au client).
        """
        effective_limit = _clamp_limit(limit)
        sort_expr = func.coalesce(Conversation.last_message_at, Conversation.created_at)

        conditions = [
            Conversation.user_id == user.id,
            Conversation.deleted_at.is_(None),
            Conversation.is_archived.is_(is_archived),
        ]
        if is_favorite is not None:
            conditions.append(Conversation.is_favorite.is_(is_favorite))
        if expert_id is not None:
            conditions.append(Conversation.expert_id == expert_id)
        if project_id is not None:
            conditions.append(Conversation.project_id == project_id)

        if q is not None:
            q_stripped = q.strip()
            if q_stripped:
                # Fuzzy match trigram sur le titre (index gin_trgm_ops).
                title_match = Conversation.title.ilike(f"%{q_stripped}%")
                # FTS français sur `messages.search_vector` (colonne générée
                # STORED, index GIN). `text()` + bind param pour passer
                # l'opérateur `@@` et `plainto_tsquery` que l'ORM ne modélise
                # pas nativement — tout en restant à l'abri de l'injection
                # (le paramètre est bindé, pas interpolé).
                fts_clause = text(
                    "messages.search_vector @@ plainto_tsquery('french', :q_fts)"
                ).bindparams(q_fts=q_stripped)
                message_match = sa_exists(
                    select(Message.id).where(
                        Message.conversation_id == Conversation.id,
                        Message.deleted_at.is_(None),
                        fts_clause,
                    )
                )
                conditions.append(or_(title_match, message_match))

        if cursor:
            cursor_ts, cursor_id = _decode_cursor(cursor)
            # Keyset DESC : la ligne suivante a un (sort_ts, id) strictement
            # inférieur à (cursor_ts, cursor_id) en ordre lexicographique.
            conditions.append(tuple_(sort_expr, Conversation.id) < tuple_(cursor_ts, cursor_id))

        stmt = (
            select(Conversation)
            .where(*conditions)
            .order_by(sort_expr.desc(), Conversation.id.desc())
            .limit(effective_limit + 1)
        )
        result = await db.execute(stmt)
        rows = list(result.scalars().all())

        has_next = len(rows) > effective_limit
        items = rows[:effective_limit]
        next_cursor: str | None = None
        if has_next and items:
            last = items[-1]
            last_ts = last.last_message_at or last.created_at
            next_cursor = _encode_cursor(last_ts, last.id)

        return ConversationsPageOrm(items=items, next_cursor=next_cursor)

    # ── GET BY ID ────────────────────────────────────────────────
    @staticmethod
    async def get_by_id(
        conversation_id: uuid.UUID,
        user: User,
        db: AsyncSession,
    ) -> Conversation:
        """Retourne une conversation précise (propriétaire uniquement)."""
        return await ConversationService._get_owned_conversation(conversation_id, user.id, db)

    # ── UPDATE (partiel) ─────────────────────────────────────────
    @staticmethod
    async def update(
        conversation_id: uuid.UUID,
        body: ConversationUpdate,
        user: User,
        db: AsyncSession,
    ) -> Conversation:
        """Met à jour partiellement une conversation.

        Seuls les champs présents dans le corps (`model_dump(exclude_unset=True)`)
        sont modifiés — aucun champ explicite à `null` n'est interprété comme
        un effacement (sémantique PATCH stricte). `expert_id` n'est
        volontairement pas modifiable (pas dans `ConversationUpdate`) pour
        éviter le contournement de tarification / disclaimer après création.
        """
        conversation = await ConversationService._get_owned_conversation(
            conversation_id, user.id, db
        )
        update_data = body.model_dump(exclude_unset=True)
        if not update_data:
            # Aucun champ envoyé → no-op, on évite un UPDATE inutile et un
            # bump artificiel de updated_at.
            return conversation
        for field, value in update_data.items():
            setattr(conversation, field, value)
        conversation.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(conversation)
        log.info(
            "chat.conversation.updated",
            user_id=str(user.id),
            conversation_id=str(conversation.id),
            fields=list(update_data.keys()),
        )
        return conversation

    # ── SOFT DELETE ──────────────────────────────────────────────
    @staticmethod
    async def soft_delete(
        conversation_id: uuid.UUID,
        user: User,
        db: AsyncSession,
    ) -> None:
        """Soft-delete : renseigne `deleted_at = NOW()`.

        La conversation disparaît de tous les listings actifs (filtre
        `deleted_at IS NULL` omniprésent) et rejoint la corbeille, exposée
        via `list_trash_for_user` / `restore` / `permanent_delete`. Les
        messages liés restent en DB : pas de ON DELETE cascade appliqué ici
        puisqu'on ne fait pas un DELETE SQL. Suppression définitive côté
        utilisateur = `permanent_delete`; côté RGPD = `DELETE /user/account`
        qui purge la ligne users et déclenche le `ON DELETE CASCADE` au
        niveau FK.
        """
        conversation = await ConversationService._get_owned_conversation(
            conversation_id, user.id, db
        )
        now = datetime.now(UTC)
        conversation.deleted_at = now
        conversation.updated_at = now
        await db.commit()
        log.info(
            "chat.conversation.soft_deleted",
            user_id=str(user.id),
            conversation_id=str(conversation.id),
        )

    # ── LIST TRASH (paginée cursor-based, tri par date de suppression) ─
    @staticmethod
    async def list_trash_for_user(
        user: User,
        db: AsyncSession,
        *,
        cursor: str | None = None,
        limit: int | None = None,
        expert_id: str | None = None,
    ) -> ConversationsPageOrm:
        """Liste paginée des conversations **soft-deletées** de l'utilisateur.

        Symétrique de `list_for_user`, mais avec deux différences clés :

        - **Filtre inversé** : `deleted_at IS NOT NULL` (au lieu de IS NULL).
          `is_archived` et `is_favorite` ne sont plus pertinents dans la
          corbeille (UX standard : on voit tout ce qui a été supprimé, sans
          re-filtrer par archivage ou favori) — on ne les expose donc pas.

        - **Clé de tri : `deleted_at DESC`** (au lieu de
          `COALESCE(last_message_at, created_at) DESC`). Dans la corbeille,
          l'utilisateur veut voir « ce que j'ai supprimé récemment » en
          premier — pas « les conversations dont l'activité était
          récente ». Pas de COALESCE nécessaire : `deleted_at` est non-NULL
          par construction du filtre.

        `expert_id` optionnel : même rôle que dans `list_for_user`, utile
        pour une corbeille filtrée par mode expert côté Flutter.
        """
        effective_limit = _clamp_limit(limit)
        sort_expr = Conversation.deleted_at

        conditions = [
            Conversation.user_id == user.id,
            Conversation.deleted_at.is_not(None),
        ]
        if expert_id is not None:
            conditions.append(Conversation.expert_id == expert_id)

        if cursor:
            cursor_ts, cursor_id = _decode_cursor(cursor)
            conditions.append(tuple_(sort_expr, Conversation.id) < tuple_(cursor_ts, cursor_id))

        stmt = (
            select(Conversation)
            .where(*conditions)
            .order_by(sort_expr.desc(), Conversation.id.desc())
            .limit(effective_limit + 1)
        )
        result = await db.execute(stmt)
        rows = list(result.scalars().all())

        has_next = len(rows) > effective_limit
        items = rows[:effective_limit]
        next_cursor: str | None = None
        if has_next and items:
            last = items[-1]
            # `deleted_at` est garanti non-null par le WHERE.
            if last.deleted_at is not None:
                next_cursor = _encode_cursor(last.deleted_at, last.id)

        return ConversationsPageOrm(items=items, next_cursor=next_cursor)

    # ── RESTORE (sortie de corbeille) ───────────────────────────
    @staticmethod
    async def restore(
        conversation_id: uuid.UUID,
        user: User,
        db: AsyncSession,
    ) -> Conversation:
        """Restaure une conversation depuis la corbeille — `deleted_at = NULL`.

        La conversation réapparaît dans les listings actifs, à sa place de
        tri habituelle (`last_message_at` inchangé — on ne remonte pas
        artificiellement la conv restaurée, sinon l'UX devient étrange au
        bout de quelques va-et-vient). Owner check + IS NOT NULL via
        `_get_owned_conversation_in_trash` : tenter de restaurer une
        conversation active (ou inconnue, ou appartenant à un autre user)
        retourne 404.
        """
        conversation = await ConversationService._get_owned_conversation_in_trash(
            conversation_id, user.id, db
        )
        conversation.deleted_at = None
        conversation.updated_at = datetime.now(UTC)
        await db.commit()
        await db.refresh(conversation)
        log.info(
            "chat.conversation.restored",
            user_id=str(user.id),
            conversation_id=str(conversation.id),
        )
        return conversation

    # ── PERMANENT DELETE (purge définitive) ─────────────────────
    @staticmethod
    async def permanent_delete(
        conversation_id: uuid.UUID,
        user: User,
        db: AsyncSession,
    ) -> None:
        """Supprime définitivement une conversation déjà dans la corbeille.

        Contrat strict :
        - La conversation DOIT être soft-deletée au préalable
          (`deleted_at IS NOT NULL`). Un endpoint `DELETE .../permanent`
          qui purgerait directement une conv active serait un pistolet
          vers le pied côté UX (clic accidentel → perte irréversible).
          On force donc le flux en deux temps : `soft_delete` → `permanent_delete`.

        - Vrai `DELETE SQL` : les messages et les `AbuseReport` liés sont
          supprimés via `ON DELETE CASCADE` défini au niveau FK en DB
          (cf. migration 002). Pas de purge applicative, Postgres s'en
          charge atomiquement dans la même transaction.

        Owner check via `_get_owned_conversation_in_trash`. Tenter de
        purger une conv active retourne 404 (le helper requiert
        `deleted_at IS NOT NULL`), ce qui protège l'invariant « on ne
        purge que depuis la corbeille ».
        """
        conversation = await ConversationService._get_owned_conversation_in_trash(
            conversation_id, user.id, db
        )
        await db.delete(conversation)
        await db.commit()
        log.info(
            "chat.conversation.permanent_deleted",
            user_id=str(user.id),
            conversation_id=str(conversation_id),
        )

    # ── LIST MESSAGES (paginée cursor-based, ASC) ───────────────
    @staticmethod
    async def list_messages(
        conversation_id: uuid.UUID,
        user: User,
        db: AsyncSession,
        *,
        cursor: str | None = None,
        limit: int | None = None,
    ) -> MessagesPageOrm:
        """Liste paginée des messages d'une conversation — ordre ASC.

        Tri ascendant : la lecture côté Flutter est chronologique (du plus
        ancien au plus récent, comme un fil de discussion). Pour charger
        l'historique depuis le haut de l'écran, le Flutter demande la
        première page sans curseur, puis utilise `next_cursor` pour la page
        suivante (messages plus récents).

        Owner check obligatoire avant toute requête : un utilisateur qui
        envoie un `conversation_id` qu'il ne possède pas reçoit 404, pas
        une liste vide — on ne laisse aucune place au doute.
        """
        await ConversationService._get_owned_conversation(conversation_id, user.id, db)
        effective_limit = _clamp_limit(limit)

        conditions = [
            Message.conversation_id == conversation_id,
            Message.deleted_at.is_(None),
        ]
        if cursor:
            cursor_ts, cursor_id = _decode_cursor(cursor)
            # Keyset ASC : la ligne suivante a un (created_at, id) strictement
            # supérieur à (cursor_ts, cursor_id). L'index idx_messages_conv_time
            # sur (conversation_id, created_at, id) est exactement aligné.
            conditions.append(tuple_(Message.created_at, Message.id) > tuple_(cursor_ts, cursor_id))

        stmt = (
            select(Message)
            .where(*conditions)
            .order_by(Message.created_at.asc(), Message.id.asc())
            .limit(effective_limit + 1)
        )
        result = await db.execute(stmt)
        rows = list(result.scalars().all())

        has_next = len(rows) > effective_limit
        items = rows[:effective_limit]
        next_cursor: str | None = None
        if has_next and items:
            last = items[-1]
            next_cursor = _encode_cursor(last.created_at, last.id)

        return MessagesPageOrm(items=items, next_cursor=next_cursor)

    # ── BUMP COUNTERS (atomique, sans commit) ───────────────────
    @staticmethod
    async def _bump_counters(
        conversation_id: uuid.UUID,
        db: AsyncSession,
        *,
        delta: int = 1,
    ) -> None:
        """Incrémente `message_count` de `delta` et refresh `last_message_at`.

        Volontairement SANS `db.commit()` : cette méthode est appelée DANS
        la même transaction que les INSERTs des messages. Garantit
        l'invariant « N messages insérés ⇔ compteur +N » même en cas de
        crash (rollback SQL uniformément).

        `delta` par défaut à 1 (ajout d'un message simple), mais `start_
        stream_turn` l'utilise avec `delta=2` pour insérer atomiquement le
        message utilisateur + le placeholder assistant en une seule touche
        du compteur.

        Pattern SQLAlchemy 2.0 : on émet un `UPDATE ... SET col = col + N`
        côté SQL, jamais un `SELECT` suivi d'un `UPDATE`. Postgres sérialise
        l'opération au niveau ligne, pas besoin de verrou applicatif.

        Ne vérifie PAS le propriétaire : la méthode est privée, appelée
        uniquement depuis des chemins qui ont déjà validé la propriété via
        `_get_owned_conversation`.
        """
        now = datetime.now(UTC)
        stmt = (
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(
                message_count=Conversation.message_count + delta,
                last_message_at=now,
                updated_at=now,
            )
        )
        await db.execute(stmt)

    # ══════════════════════════════════════════════════════════════
    # STREAM PERSISTÉ — cycle de vie d'un tour de chat SSE
    # ══════════════════════════════════════════════════════════════
    # Orchestre la persistance d'un tour complet `/chat/stream` :
    #
    #   ensure_conversation_for_stream  → résout la conv (créée si absent)
    #   load_context_messages           → rejoue l'historique à l'IA
    #   start_stream_turn               → insère user + placeholder, status=streaming
    #   [SSE stream en vol, géré par StreamHandler]
    #   finalize_assistant_stream       → UPDATE atomique avec status final
    #
    # Invariants :
    # - Toute erreur d'IDOR est attrapée dans `ensure_conversation_for_stream`
    #   via `_get_owned_conversation`. Les étapes suivantes travaillent sur
    #   une conv déjà vérifiée (pas de re-check).
    # - `start_stream_turn` commit pour rendre le placeholder visible (le
    #   Flutter pourra afficher un spinner "assistant is typing" dès le
    #   retour de l'endpoint si besoin).
    # - `finalize_assistant_stream` utilise une session FRAÎCHE (ouverte par
    #   le router dans son `finally` shieldé) pour être robuste à une
    #   déconnexion client : la session de la requête HTTP est peut-être
    #   en rollback à ce moment-là.
    # ══════════════════════════════════════════════════════════════

    # ── Résolution de la conversation cible ────────────────────────
    @staticmethod
    async def ensure_conversation_for_stream(
        conversation_id: uuid.UUID | None,
        user: User,
        db: AsyncSession,
        *,
        expert_id_hint: str | None = None,
        project_id: uuid.UUID | None = None,
    ) -> Conversation:
        """Retourne la conversation cible du stream — la crée si besoin.

        - `conversation_id=None` → crée une nouvelle conversation pour
          l'utilisateur. L'`expert_id_hint` permet au Flutter de passer
          l'expert choisi dans le même appel (sinon `'general'`).
        - `conversation_id=<UUID>` → charge la conv existante via
          `_get_owned_conversation` (404 IDOR-safe si pas propriétaire).
          L'`expert_id_hint` est ignoré pour ce chemin : l'expert d'une
          conv est figé à la création (cf. note `ConversationUpdate`).

        `project_id` (D3 — 2026-05-04) :

        - `conversation_id=None` ET `project_id=<UUID>` → ownership check
          du projet via `ProjectService._get_owned_project` (404 IDOR-safe)
          puis création de la conv attachée (`project_id` peuplé sur la
          ligne, FK `ON DELETE SET NULL` migration 006 + soft-delete projet
          C2 fait UPDATE explicite côté `ProjectService.soft_delete`).
        - `conversation_id=<UUID>` ET `project_id=<UUID>` → **ignoré
          silencieusement** + log debug. Le rattachement d'une conv
          existante à un projet ne passe PAS par `/chat/stream` (V1) —
          un futur `PATCH /chat/conversations/{id}` exposera la mutation
          `project_id` quand le besoin sera prouvé. Décision V1 : contrat
          simple côté front (un seul appel par message, pas de mutation
          cross-feature transparente qui surprendrait l'user).
        - `project_id=None` → comportement legacy strictement préservé.

        Commit en fin de création pour que le router puisse ajouter ses
        messages dans une transaction distincte (facilite le debug et
        permet à un crash sur l'INSERT message de laisser la conv vide
        plutôt que de tout rollback).

        **Anti-MissingGreenlet** : capture `user_id_str = str(user.id)`
        AVANT `db.commit()` (pattern aligné `create` ci-dessus).
        """
        # Mode existing : charge + retourne, ignore project_id avec log debug.
        if conversation_id is not None:
            if project_id is not None:
                log.debug(
                    "chat.stream.project_id_ignored_on_existing_conv",
                    conversation_id=str(conversation_id),
                    project_id=str(project_id),
                    reason=(
                        "Le rattachement d'une conv existante à un projet "
                        "ne passe pas par /chat/stream (V1)."
                    ),
                )
            return await ConversationService._get_owned_conversation(conversation_id, user.id, db)

        # Local import pour casser le cycle projects ↔ chat (cf. note
        # exhaustive sur `create` ci-dessus).
        from app.features.projects.service import ProjectService

        # Capture en str AVANT commit (anti-MissingGreenlet post-rollback).
        user_id_str = str(user.id)

        # Mode new + project_id : ownership check AVANT INSERT — 404
        # IDOR-safe + zéro écriture DB si projet inconnu / pas owner.
        if project_id is not None:
            await ProjectService._get_owned_project(project_id, user.id, db)

        conversation = Conversation(
            user_id=user.id,
            title=None,
            expert_id=expert_id_hint or "general",
            project_id=project_id,
        )
        db.add(conversation)
        await db.commit()
        await db.refresh(conversation)
        log.info(
            "chat.conversation.created_for_stream",
            user_id=user_id_str,
            conversation_id=str(conversation.id),
            expert_id=conversation.expert_id,
            project_id=str(project_id) if project_id else None,
        )
        return conversation

    # ── Chargement du contexte IA depuis la DB ─────────────────────
    @staticmethod
    async def load_context_messages(
        conversation: Conversation,
        db: AsyncSession,
        *,
        limit: int = _CONTEXT_MESSAGES_DEFAULT,
    ) -> list[AiChatMessage]:
        """Renvoie les `limit` derniers messages `completed` au format IA.

        Seuls les messages terminés proprement (`status='completed'`) sont
        rejoués. On ne renvoie JAMAIS à l'IA :
        - les placeholders `streaming` (contenu vide ou partiel, corromprait
          le contexte),
        - les `failed` ou `cancelled` (contenu tronqué ou invalide),
        - les messages soft-deletés.

        On charge DESC puis on inverse : Postgres est optimisé pour
        retourner les N dernières lignes via l'index
        `(conversation_id, created_at, id)`, pas les N premières.
        """
        stmt = (
            select(Message)
            .where(
                Message.conversation_id == conversation.id,
                Message.deleted_at.is_(None),
                Message.status == "completed",
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        rows = list(result.scalars().all())
        rows.reverse()
        # `role` en DB est contraint à `user`/`assistant`/`system` par CHECK,
        # on peut donc le passer tel quel au provider IA.
        return [
            AiChatMessage(role=r.role, content=r.content)
            for r in rows  # type: ignore[arg-type]
        ]

    # ── Début d'un tour : user + placeholder assistant ─────────────
    @staticmethod
    async def start_stream_turn(
        conversation: Conversation,
        user_text: str,
        db: AsyncSession,
    ) -> tuple[Message, Message]:
        """Insère le message utilisateur + un placeholder assistant.

        Atomique : les deux INSERTs + l'incrément `message_count += 2` sont
        dans la même transaction. Un crash au milieu laisse la conv dans
        l'état d'avant l'appel.

        Le placeholder `role='assistant', status='streaming', content=''`
        sert de cible pour la finalisation : `finalize_assistant_stream`
        UPDATE cette ligne par son `id`. Ça garantit qu'un appel au GET
        `/conversations/{id}/messages` entre le début et la fin du stream
        voit l'assistant "en cours de frappe" (on peut même streamer côté
        UI en polling si besoin).

        Retour : `(user_message, placeholder_assistant_message)`. Le caller
        utilise `placeholder.id` pour identifier la cible de finalisation
        et `conversation.id` (déjà connu) pour recharger la conv plus tard.
        """
        user_message = Message(
            conversation_id=conversation.id,
            role="user",
            content=user_text,
            status="completed",
        )
        placeholder = Message(
            conversation_id=conversation.id,
            role="assistant",
            content="",
            status="streaming",
        )
        db.add(user_message)
        db.add(placeholder)

        await ConversationService._bump_counters(conversation.id, db, delta=2)

        await db.commit()
        await db.refresh(user_message)
        await db.refresh(placeholder)

        log.info(
            "chat.stream.turn_started",
            conversation_id=str(conversation.id),
            user_message_id=str(user_message.id),
            placeholder_id=str(placeholder.id),
        )
        return user_message, placeholder

    # ── Finalisation du placeholder assistant ──────────────────────
    @staticmethod
    async def finalize_assistant_stream(
        assistant_message_id: uuid.UUID,
        conversation_id: uuid.UUID,
        db: AsyncSession,
        *,
        content: str,
        status: str,
        provider: str | None,
        model: str | None,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        total_tokens: int | None,
        cost_usd: Decimal | float | None,
        error_code: str | None,
    ) -> None:
        """UPDATE atomique du placeholder assistant avec son état final.

        `status` doit être l'un de `'completed' | 'failed' | 'cancelled'` —
        cohérent avec le CHECK SQL. Le caller (router) décide du status à
        partir de la raison émise dans l'événement SSE `done` (`stop`,
        `error`, `cancelled`).

        Met à jour `conversation.last_message_at` et `updated_at` dans la
        foulée pour que le tri "récence DESC" soit correct après la fin du
        stream (le `_bump_counters` initial l'avait déjà fait au moment de
        l'INSERT du placeholder, on rafraîchit ici pour refléter la fin
        réelle — ça ne casse rien puisqu'on n'a pas d'invariant « last =
        début du stream »).

        Commit obligatoire : cette méthode est appelée depuis une session
        DB fraîche (ouverte par le router dans son `finally`), et l'appelant
        ne fera pas de commit après.
        """
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError(f"status invalide : {status!r}")

        # Decimal est requis pour NUMERIC(10, 6). On accepte float en entrée
        # (estimate_cost_usd retourne float) et on convertit proprement pour
        # éviter les artefacts de représentation binaire.
        cost_value: Decimal | None
        if cost_usd is None:
            cost_value = None
        elif isinstance(cost_usd, Decimal):
            cost_value = cost_usd
        else:
            cost_value = Decimal(str(cost_usd))

        now = datetime.now(UTC)
        await db.execute(
            update(Message)
            .where(Message.id == assistant_message_id)
            .values(
                content=content,
                status=status,
                provider=provider,
                model=model,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                cost_usd=cost_value,
                error_code=error_code,
                finished_at=now,
                updated_at=now,
            )
        )
        await db.execute(
            update(Conversation)
            .where(Conversation.id == conversation_id)
            .values(last_message_at=now, updated_at=now)
        )
        await db.commit()
        log.info(
            "chat.stream.turn_finalized",
            conversation_id=str(conversation_id),
            assistant_message_id=str(assistant_message_id),
            status=status,
            provider=provider,
            model=model,
            total_tokens=total_tokens,
            error_code=error_code,
        )


# ══════════════════════════════════════════════════════════════
# ReportService — signalements de messages abusifs
# ══════════════════════════════════════════════════════════════


class ReportService:
    """Logique métier des signalements `AbuseReport` (App Store §1.2).

    Vit dans le même module que `ConversationService` parce que ses
    invariants sont étroitement couplés au domaine Chat (owner-check d'un
    Message, dénormalisation de `conversation_id`). Garde le pattern
    static-method namespace pour rester cohérent avec le reste du
    feature.
    """

    # ── Helper d'isolation — cherche un message ET vérifie la propriété ──
    @staticmethod
    async def _get_owned_message(
        message_id: uuid.UUID,
        user_id: uuid.UUID,
        db: AsyncSession,
    ) -> Message:
        """Charge un message dont la conversation appartient à l'user.

        Owner-check via JOIN en une seule requête : on ne peut signaler
        qu'un message d'une conversation qu'on possède. Évite deux SELECT
        en cascade (Message puis Conversation) — un seul aller-retour SQL,
        sécurité strictement équivalente.

        - Mismatch user → `ResourceNotFoundException` (404, jamais 403),
          même règle anti-énumération que `_get_owned_conversation`.
        - Conversation soft-deletée OU message soft-deleté → 404 aussi :
          on ne signale pas un message déjà retiré.
        """
        stmt = (
            select(Message)
            .join(Conversation, Conversation.id == Message.conversation_id)
            .where(
                Message.id == message_id,
                Message.deleted_at.is_(None),
                Conversation.user_id == user_id,
                Conversation.deleted_at.is_(None),
            )
        )
        result = await db.execute(stmt)
        message = result.scalar_one_or_none()
        if message is None:
            raise ResourceNotFoundException("Message")
        return message

    # ── CREATE ──────────────────────────────────────────────────
    @staticmethod
    async def create_report(
        user: User,
        body: AbuseReportCreate,
        db: AsyncSession,
    ) -> AbuseReport:
        """Crée un signalement sur un message possédé par l'utilisateur.

        Pipeline :
        1. Vérifie via JOIN que le message existe ET appartient à l'user
           (sinon 404, IDOR-safe).
        2. INSERT avec `conversation_id` dénormalisé (pris sur le message
           déjà chargé — pas de second SELECT).
        3. Si la contrainte UNIQUE `(user_id, message_id)` saute, on
           rollback et lève `DuplicateReportException` (409). On ne
           pré-check pas en SELECT : pattern TOCTOU classique, deux
           clients concurrents passeraient tous les deux le test avant
           de tenter d'insérer. La DB est seule source de vérité.

        L'absence de pré-check rend la trame anti-spam bord-à-bord :
        Postgres atomise l'unicité, on traduit son verdict en HTTP propre.
        """
        message = await ReportService._get_owned_message(body.message_id, user.id, db)

        # Capture des identifiants en str AVANT le commit : après un rollback,
        # SQLAlchemy expire toutes les colonnes ORM, et un simple `str(user.id)`
        # déclencherait un lazy-load qui plante en `MissingGreenlet` (le
        # pool_pre_ping fait un setattr sync sur la connexion psycopg).
        user_id_str = str(user.id)
        message_id_str = str(message.id)
        conversation_id_str = str(message.conversation_id)

        report = AbuseReport(
            user_id=user.id,
            message_id=message.id,
            conversation_id=message.conversation_id,
            reason=body.reason,
            detail=body.detail,
        )
        db.add(report)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            log.info(
                "chat.report.duplicate",
                user_id=user_id_str,
                message_id=message_id_str,
            )
            raise DuplicateReportException() from None

        await db.refresh(report)
        log.info(
            "chat.report.created",
            user_id=user_id_str,
            report_id=str(report.id),
            message_id=message_id_str,
            conversation_id=conversation_id_str,
            reason=report.reason,
        )
        return report
