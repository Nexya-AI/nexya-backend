"""
Tâches arq liées au domaine Chat.

Pour l'instant : `generate_conversation_title` — génère automatiquement un
titre concis pour une conversation après son deuxième échange complet
(condition vérifiée à l'enqueue ET à l'exécution pour résister aux races).

Le worker appelle Gemini Flash (le modèle le moins cher de la flotte) avec
un prompt court qui demande un titre ≤ 60 caractères en français. Le titre
est stocké dans `Conversation.title`, et `Conversation.title_generated_at`
est posé en sentinelle one-shot — la tâche s'auto-désamorce sur les
exécutions ultérieures.

L'enqueue est routé depuis `_finalize_in_fresh_session` (router chat) :
quand le compteur `message_count >= 4` ET `status='completed'` ET
`title_generated_at IS NULL`, on enqueue. Le seuil `>= 4` (et non `== 4`)
est délibéré : si l'enqueue échoue (Redis flap, bug arq), un échange
ultérieur déclenchera une nouvelle tentative — la sentinelle protège
de tout doublon.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from sqlalchemy import update

from app.ai.providers import ChatCompletionRequest
from app.ai.providers import ChatMessage as AiChatMessage
from app.ai.providers.base import ProviderError
from app.ai.runtime import get_ai_router
from app.config import settings
from app.core.database.postgres import AsyncSessionLocal
from app.features.chat.models import Conversation, Message

if TYPE_CHECKING:
    from arq.connections import ArqRedis

log = structlog.get_logger()

# ── Paramètres du titre auto ──────────────────────────────────────
# **D1.5-fix (2026-05-03)** — cap réduit 60 → 40 chars et prompt durci
# pour empêcher Gemini Flash de retourner une phrase complète narrative
# (ex: "L'utilisateur veut savoir comment configurer son émulateur"
# tronquée à 60 chars → titre illisible) au lieu d'un groupe nominal
# court (ex: "Configuration émulateur Flutter").
TITLE_MAX_CHARS = 40
TITLE_PROMPT = (
    "Tu es un générateur de titres concis pour conversations.\n"
    "\n"
    "RÈGLES STRICTES :\n"
    "- 3 à 5 mots MAXIMUM.\n"
    "- Format : groupe nominal court, PAS une phrase complète.\n"
    "- Pas d'article au début (\"Configuration émulateur\", PAS "
    "\"La configuration de l'émulateur\").\n"
    "- Pas de verbe conjugué.\n"
    "- Pas de phrase narrative comme \"L'utilisateur veut...\" ou "
    "\"Discussion sur...\".\n"
    "- En français.\n"
    "\n"
    "EXEMPLES VALIDES :\n"
    "- \"Configuration émulateur Flutter\"\n"
    "- \"Recettes pâtes carbonara\"\n"
    "- \"Algorithme tri rapide\"\n"
    "- \"Génération image chat orange\"\n"
    "\n"
    "EXEMPLES INVALIDES :\n"
    "- \"L'utilisateur demande comment configurer son émulateur\" (phrase)\n"
    "- \"Voici un titre pour la discussion\" (méta-discours)\n"
    "- \"Discussion sur les chats orange\" (commence par 'Discussion')\n"
    "\n"
    "Réponds UNIQUEMENT par le titre, rien d'autre. "
    "Pas de guillemets, pas de ponctuation finale."
)
# Borne dure côté worker : quelques tokens suffisent pour un titre, plafond
# défensif contre une dérive du modèle.
TITLE_MAX_TOKENS = 40

# Modèle utilisé pour le titre — Gemini Flash : ~$0.00005 par titre, soit
# ~$475/mois worst-case si tous les 950k users démarrent une conversation
# par jour. Acceptable pour un coût sans incidence UX.
TITLE_PROVIDER_KEY = "general"  # expert_id qui résout vers Gemini Flash


# ══════════════════════════════════════════════════════════════
# ENQUEUE — appelé depuis le router chat
# ══════════════════════════════════════════════════════════════

# Pool partagé : un seul ArqRedis pour toute la lifetime du process
# applicatif. Reconstruit à la première utilisation et conservé.
# Import paresseux d'arq pour ne pas exiger la dépendance lors de l'import du
# module (ex: tests d'API qui patchent `enqueue_title_generation`).
_arq_pool: ArqRedis | None = None


async def _get_arq_pool() -> ArqRedis:
    """Pool arq paresseux — créé une seule fois par process."""
    global _arq_pool
    if _arq_pool is None:
        from arq.connections import RedisSettings, create_pool

        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _arq_pool


async def enqueue_title_generation(conversation_id: UUID) -> None:
    """Enqueue la tâche `generate_conversation_title` pour une conv.

    Échec silencieux (log + return) si Redis est down — la génération
    de titre est cosmétique, jamais bloquante pour l'utilisateur. Une
    panne du pool arq ne doit pas faire planter la finalisation d'un
    stream chat.

    Module-level (et pas méthode) volontairement : facilite le
    monkeypatch dans les tests d'intégration sans toucher au worker.
    """
    try:
        pool = await _get_arq_pool()
        await pool.enqueue_job(
            "generate_conversation_title",
            str(conversation_id),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "chat.title.enqueue_failed",
            conversation_id=str(conversation_id),
            error=str(exc),
            error_type=type(exc).__name__,
        )


# ══════════════════════════════════════════════════════════════
# WORKER — generate_conversation_title
# ══════════════════════════════════════════════════════════════


async def generate_conversation_title(ctx: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    """Génère et persiste un titre pour une conversation.

    Idempotence stricte : double-check `title IS NULL AND title_generated_at
    IS NULL` après lecture. Si entre l'enqueue et l'exécution, une autre
    occurrence du job a déjà tourné (très improbable, mais possible si
    Redis a re-livré un job), on retourne `{skipped: true}` sans appel IA.

    Stratégie de coût : on charge les 6 derniers messages `completed` (≈ 3
    tours user/assistant) pour donner du contexte au LLM. Plus court n'aide
    pas, plus long fait exploser les tokens d'entrée pour un gain nul.
    """
    conv_uuid = UUID(conversation_id)
    log.info("chat.title.job_start", conversation_id=conversation_id)

    async with AsyncSessionLocal() as db:
        conversation = await db.get(Conversation, conv_uuid)
        if conversation is None:
            log.warning("chat.title.conversation_missing", conversation_id=conversation_id)
            return {"skipped": True, "reason": "missing"}

        # Double-check sentinelle : un autre worker a peut-être déjà
        # tourné, ou la conv a été soft-deletée entre-temps.
        if conversation.title_generated_at is not None or conversation.title is not None:
            log.info(
                "chat.title.already_generated",
                conversation_id=conversation_id,
                has_title=conversation.title is not None,
                has_sentinel=conversation.title_generated_at is not None,
            )
            return {"skipped": True, "reason": "already_generated"}

        if conversation.deleted_at is not None:
            log.info("chat.title.skip_deleted", conversation_id=conversation_id)
            return {"skipped": True, "reason": "deleted"}

        # Charger les 6 derniers messages completed pour fournir du contexte
        # au modèle. Ordre DESC en SQL puis reverse en Python pour ré-aligner
        # chronologiquement avant de passer au LLM.
        msg_stmt = (
            Message.__table__.select()
            .where(
                Message.conversation_id == conv_uuid,
                Message.deleted_at.is_(None),
                Message.status == "completed",
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(6)
        )
        result = await db.execute(msg_stmt)
        rows = list(result.fetchall())
        rows.reverse()

        if len(rows) < 2:
            # Garde-fou : sans au moins un user + un assistant terminé,
            # le titre n'aurait aucun sens. On laisse la sentinelle vide
            # pour permettre une nouvelle tentative au prochain tour.
            log.info(
                "chat.title.not_enough_messages",
                conversation_id=conversation_id,
                count=len(rows),
            )
            return {"skipped": True, "reason": "not_enough_messages"}

        ai_messages = [AiChatMessage(role=row.role, content=row.content) for row in rows]

        # Appel IA — la moindre erreur est swallowed : le titre n'est pas
        # critique, on n'écrit rien et on laisse retenter plus tard.
        try:
            title = await _call_llm_for_title(ai_messages)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "chat.title.llm_failed",
                conversation_id=conversation_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return {"skipped": True, "reason": "llm_failed"}

        title = _sanitize_title(title)
        if not title:
            log.warning("chat.title.empty_response", conversation_id=conversation_id)
            return {"skipped": True, "reason": "empty"}

        now = datetime.now(UTC)
        await db.execute(
            update(Conversation)
            .where(
                Conversation.id == conv_uuid,
                Conversation.title_generated_at.is_(None),
            )
            .values(title=title, title_generated_at=now, updated_at=now)
        )
        await db.commit()

    log.info(
        "chat.title.generated",
        conversation_id=conversation_id,
        title=title,
    )
    return {"skipped": False, "title": title}


# ══════════════════════════════════════════════════════════════
# Helpers internes
# ══════════════════════════════════════════════════════════════


async def _call_llm_for_title(history: list[AiChatMessage]) -> str:
    """Appelle Gemini Flash et retourne la concaténation du stream.

    `expert_id='general'` route automatiquement vers Gemini Flash (cf.
    `app/ai/experts.py`). On consomme le générateur en entier et on
    concatène les `delta` — le coût est dérisoire pour quelques tokens
    de sortie.
    """
    resolution = get_ai_router().resolve(TITLE_PROVIDER_KEY)
    request = ChatCompletionRequest(
        messages=history,
        system_prompt=TITLE_PROMPT,
        model=resolution.model,
        temperature=0.4,
        max_tokens=TITLE_MAX_TOKENS,
    )
    parts: list[str] = []
    try:
        async for chunk in resolution.provider.stream_chat(request):
            if chunk.delta:
                parts.append(chunk.delta)
    except ProviderError:
        # Re-lève pour que l'appelant log proprement (le scope try/except
        # du worker capture toutes les exceptions et retourne `skipped`).
        raise
    return "".join(parts)


def _sanitize_title(raw: str) -> str:
    """Nettoie le titre : strip, dégarnit guillemets, tronque proprement.

    **D1.5-fix (2026-05-03)** — coupe sur un espace pour ne jamais
    casser un mot au milieu (ex: "Configuration émul…" au lieu de
    "Configuration émulateur Flutter" tronqué bêtement à 40 chars
    qui aurait donné "Configuration émulateur Flutter sur" → cassé
    en plein milieu sur du contenu plus long).
    """
    title = raw.strip()
    # Si le LLM a retourné plusieurs lignes (ex: explications + titre),
    # on prend la 1ère ligne non-vide uniquement.
    for line in title.splitlines():
        candidate = line.strip()
        if candidate:
            title = candidate
            break

    # Retire d'éventuels guillemets typographiques en bornes
    for ch in ('"', "'", "“", "”", "«", "»"):
        if title.startswith(ch):
            title = title[1:]
        if title.endswith(ch):
            title = title[:-1]
    title = title.strip().rstrip(".!?:;,")

    if len(title) > TITLE_MAX_CHARS:
        # Coupe propre sur un espace pour ne pas tronquer un mot
        cut = title[:TITLE_MAX_CHARS - 1]
        last_space = cut.rfind(" ")
        if last_space > TITLE_MAX_CHARS // 2:
            cut = cut[:last_space]
        title = cut.rstrip() + "…"
    return title
