"""
Worker arq — extraction automatique de faits durables post-conversation.

Session D2 du Bloc D Mémoire IA. Miroir strict du pattern B5 auto-titre
(`workers/chat_tasks.generate_conversation_title`) : enqueue silencieux
depuis le router chat, sentinelle one-shot `memory_extracted_at` posée
par le worker, double-check idempotence, fail-safe sur LLM down.

Pipeline du worker `extract_durable_facts(conversation_id)` :

    1. Parse UUID + log start.
    2. Ouvre AsyncSessionLocal() — session fraîche (pas celle du router).
    3. Charge Conversation + short-circuits :
       - missing → skip
       - deleted_at IS NOT NULL → skip
       - memory_extracted_at IS NOT NULL → skip (idempotence)
    4. Charge N derniers messages completed (≥ EXTRACTION_MIN_MESSAGES).
    5. Charge User propriétaire (skip si purgé RGPD).
    6. Appel LLM (Gemini Flash, temp=0.2) → JSON strict {"facts": [...]}.
    7. Parse tolérant 3 passes (direct / markdown-wrapped / fallback []).
    8. Pour chaque fait :
       - filtre sensibilité (santé, finance, religion, politique, orientation) → skip
       - `MemoryStore.add(source='extracted', source_conversation_id=conv_uuid,
                          importance=3, metadata_json={...})`
       - La dédup SHA de D1 gère les doublons cross-conversation.
    9. Pose `memory_extracted_at = NOW()` + commit (sentinelle posée même
       si 0 fait extrait — évite de re-tenter indéfiniment).
    10. Return stats `{skipped: False, facts_extracted: N, facts_skipped: M}`.

**Filtre sensibilité** : keyword-based conservateur, appliqué en plus du
prompt LLM (défense en profondeur). Recall > precision — mieux vaut rater
1 fait légitime que stocker 1 donnée sensible sans consentement RGPD.
Phase 12 pourra raffiner avec un LLM de modération dédié.

**Dédup** : la dédup SHA-256 de D1 (`MemoryStore.add` avec
`INSERT ... ON CONFLICT DO NOTHING RETURNING`) fait le boulot
automatiquement. Si « L'utilisateur est dev Flutter » est extrait de
10 conversations différentes, 1 seule row en DB, 9 économies d'appel API
embeddings.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final
from uuid import UUID

import structlog
from sqlalchemy import select, update

from app.ai.providers import ChatCompletionRequest
from app.ai.providers import ChatMessage as AiChatMessage
from app.ai.providers.base import ProviderError
from app.ai.runtime import get_ai_router
from app.config import settings
from app.core.database.postgres import AsyncSessionLocal
from app.core.errors.exceptions import (
    EmbeddingsUnavailableException,
    MemoryQuotaExceededException,
    RateLimitExceededException,
    ValidationException,
)
from app.features.auth.models import User
from app.features.chat.models import Conversation, Message
from app.features.memory.service import MemoryStore

if TYPE_CHECKING:
    from arq.connections import ArqRedis

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes — ajustables par config si besoin Phase 12
# ══════════════════════════════════════════════════════════════

# Seuil minimum pour qu'une extraction ait un sens. 6 messages = 3 tours
# user/assistant complets. Sous ce seuil, le LLM ne dispose pas d'assez
# de signal pour extraire un fait durable fiable.
EXTRACTION_MIN_MESSAGES: Final[int] = 6

# Cap contexte LLM — 20 messages couvrent ~10 tours, assez pour identifier
# des faits récurrents sans exploser la facture tokens.
EXTRACTION_MAX_CONTEXT_MESSAGES: Final[int] = 20

# Plafond dur anti-dérive LLM. Si le modèle sort 10 faits, on tronque à 3.
EXTRACTION_MAX_FACTS: Final[int] = 3

# Longueur max d'un fait individuel (aligné sur `embeddings_content_max_chars`
# côté MemoryStore — l'embed bornera de toute façon à 2000, 200 est une
# contrainte métier plus stricte adaptée à un fait « durable »).
EXTRACTION_FACT_MAX_CHARS: Final[int] = 200
EXTRACTION_FACT_MIN_CHARS: Final[int] = 10

# Expert_id → route vers Gemini Flash (le moins cher de la flotte). Même
# discipline que B5 auto-titre.
EXTRACTION_PROVIDER_KEY: Final[str] = "general"

# Budget tokens réponse LLM — large mais borné.
EXTRACTION_LLM_MAX_TOKENS: Final[int] = 500


# ══════════════════════════════════════════════════════════════
# Prompt système — JSON strict, multi-langue, anti-dérive
# ══════════════════════════════════════════════════════════════

EXTRACTION_SYSTEM_PROMPT: Final[
    str
] = """Tu analyses une conversation pour en extraire des FAITS DURABLES sur l'utilisateur. Un fait durable = information qui reste vraie au-delà de la conversation actuelle (identité, profession, localisation, préférences long terme, projets, compétences stables).

Réponds UNIQUEMENT par un JSON strict :
{"facts": ["fait 1", "fait 2", "fait 3"]}

RÈGLES :
- Maximum 3 faits.
- Chaque fait : 10 à 200 caractères, phrase complète à la 3ème personne commençant par "L'utilisateur ..." (ou "The user ..." si la conversation est en anglais).
- UNIQUEMENT des faits durables. EXCLURE les actions ponctuelles, émotions momentanées, questions posées, réponses de l'IA.
- EXCLURE les données sensibles sans consentement explicite : santé, diagnostics médicaux, finances privées, religion, opinions politiques, vie sexuelle, orientation sexuelle, appartenance syndicale.
- Langue : même langue que la conversation. Détecter automatiquement.
- Si aucun fait durable détectable, retourne {"facts": []}.

Ne retourne RIEN d'autre que le JSON. Pas de markdown, pas de commentaire, pas de préfixe."""


# ══════════════════════════════════════════════════════════════
# Filtre sensibilité — keyword-based FR + EN
# ══════════════════════════════════════════════════════════════
#
# Filtre défensif appliqué en plus du prompt LLM. Pattern conservateur :
# recall > precision (mieux vaut rater 1 fait légitime que stocker 1
# donnée RGPD Article 9 sans consentement).
#
# Phase 12 ajoutera un LLM de modération contextuel pour éliminer les
# faux positifs (« traitement de texte » ≠ traitement médical).

SENSITIVE_KEYWORDS: Final[frozenset[str]] = frozenset(
    {
        # Santé
        "maladie",
        "diagnostic",
        "médicament",
        "medicament",
        "hiv",
        "sida",
        "cancer",
        "diabète",
        "diabete",
        "dépression",
        "depression",
        "anxiété",
        "anxiete",
        "traitement",
        "thérapie",
        "therapie",
        "disease",
        "medication",
        "diagnosed",
        "therapy",
        "disorder",
        # Finances privées
        "salaire",
        "revenu",
        "dette",
        "crédit",
        "credit",
        "prêt",
        "pret",
        "découvert",
        "decouvert",
        "salary",
        "income",
        "debt",
        "loan",
        "overdraft",
        # Religion / politique
        "musulman",
        "chrétien",
        "chretien",
        "juif",
        "bouddhiste",
        "athée",
        "athee",
        "muslim",
        "christian",
        "jewish",
        "atheist",
        "buddhist",
        "socialiste",
        "libéral",
        "liberal",
        "extrême droite",
        "extreme droite",
        "extrême gauche",
        "extreme gauche",
        # Orientation
        "homosexuel",
        "bisexuel",
        "hétérosexuel",
        "heterosexuel",
        "transgenre",
        "gay",
        "lesbian",
        "bisexual",
        "transgender",
        # Appartenance syndicale
        "syndicat",
        "syndicaliste",
        "union member",
    }
)


def _is_sensitive(fact: str) -> bool:
    """Vrai si le fait contient au moins un mot-clé sensible.

    Conservateur : matching substring case-insensitive. Peut produire des
    faux positifs (« traitement de texte »), c'est voulu — on préfère
    sur-filtrer que sous-filtrer sur des données RGPD Article 9.
    """
    lower = fact.lower()
    return any(kw in lower for kw in SENSITIVE_KEYWORDS)


# ══════════════════════════════════════════════════════════════
# Parser JSON tolérant — 3 passes
# ══════════════════════════════════════════════════════════════

# Regex qui capture le premier bloc `{...}` (greedy) — permet de parser
# une réponse LLM wrappée en markdown ```json {...} ```.
_JSON_BLOCK_RE: Final[re.Pattern[str]] = re.compile(r"\{.*\}", re.DOTALL)


def _parse_facts_json(raw: str) -> list[str]:
    """Parser tolérant en 3 passes.

    1. `json.loads` direct sur la chaîne stripée.
    2. Si échec : extraction regex du premier bloc `{...}` puis `json.loads`.
    3. Si encore échec : log warning + retourne `[]`.

    Post-parse filtering :
    - chaque élément doit être `str`
    - strip + skip les whitespace-only
    - truncate à `EXTRACTION_FACT_MAX_CHARS`
    - dédup interne (anti-LLM qui répète le même fait)
    - cap `EXTRACTION_MAX_FACTS` (tronque la fin)
    """
    if not raw:
        return []

    data: Any = None
    # Passe 1 : direct.
    stripped = raw.strip()
    try:
        data = json.loads(stripped)
    except (ValueError, TypeError):
        data = None

    # Passe 2 : extraction de bloc balancé.
    if data is None:
        match = _JSON_BLOCK_RE.search(stripped)
        if match is not None:
            try:
                data = json.loads(match.group(0))
            except (ValueError, TypeError):
                data = None

    # Passe 3 : fallback.
    if not isinstance(data, dict) or "facts" not in data:
        log.warning("memory.extract.json_unparseable", raw_preview=raw[:200])
        return []

    raw_facts = data.get("facts")
    if not isinstance(raw_facts, list):
        log.warning(
            "memory.extract.facts_not_list",
            type_seen=type(raw_facts).__name__,
        )
        return []

    # Post-parse filtering.
    seen: set[str] = set()
    cleaned: list[str] = []
    for item in raw_facts:
        if not isinstance(item, str):
            continue
        fact = item.strip()
        if not fact:
            continue
        if len(fact) > EXTRACTION_FACT_MAX_CHARS:
            fact = fact[:EXTRACTION_FACT_MAX_CHARS].rstrip()
        # Dédup interne — clé normalisée lowercase pour absorber les
        # variations de casse entre faits quasi-identiques.
        dedup_key = fact.lower()
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        cleaned.append(fact)
        if len(cleaned) >= EXTRACTION_MAX_FACTS:
            break

    return cleaned


# ══════════════════════════════════════════════════════════════
# Pool arq lazy — identique chat_tasks
# ══════════════════════════════════════════════════════════════

_arq_pool: ArqRedis | None = None


async def _get_arq_pool() -> ArqRedis:
    """Pool arq paresseux — créé une seule fois par process."""
    global _arq_pool
    if _arq_pool is None:
        from arq.connections import RedisSettings, create_pool  # noqa: PLC0415

        _arq_pool = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    return _arq_pool


# ══════════════════════════════════════════════════════════════
# ENQUEUE — appelé depuis le router chat `_finalize_in_fresh_session`
# ══════════════════════════════════════════════════════════════


async def enqueue_memory_extraction(conversation_id: UUID) -> None:
    """Enqueue la tâche `extract_durable_facts` pour une conversation.

    Échec silencieux (log warning + return) si Redis est down — l'extraction
    est **cosmétique, jamais bloquante** pour l'utilisateur. Une panne du
    pool arq ne doit pas faire planter la finalisation d'un stream chat.

    Module-level (pas méthode d'une classe) volontairement : facilite le
    monkeypatch dans les tests sans toucher au worker.
    """
    try:
        pool = await _get_arq_pool()
        await pool.enqueue_job(
            "extract_durable_facts",
            str(conversation_id),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "memory.extract.enqueue_failed",
            conversation_id=str(conversation_id),
            error=str(exc),
            error_type=type(exc).__name__,
        )


# ══════════════════════════════════════════════════════════════
# WORKER — extract_durable_facts
# ══════════════════════════════════════════════════════════════


async def extract_durable_facts(ctx: dict[str, Any], conversation_id: str) -> dict[str, Any]:
    """Extrait 0-3 faits durables d'une conversation et les indexe en pgvector.

    Idempotence stricte : double-check `memory_extracted_at IS NULL` après
    lecture. Si un autre worker a déjà tourné (re-livraison arq rare mais
    possible), retourne `{skipped: True, reason: 'already_extracted'}`.

    Stratégie coût : Gemini Flash + 20 messages context + temperature=0.2
    → ~200-500 tokens input, ~50-200 tokens output, coût <$0.0001/conv.
    Sur 950k users × 1 extraction/jour = ~$95/mois worst-case.
    """
    conv_uuid = UUID(conversation_id)
    log.info("memory.extract.job_start", conversation_id=conversation_id)

    async with AsyncSessionLocal() as db:
        conversation = await db.get(Conversation, conv_uuid)
        if conversation is None:
            log.warning(
                "memory.extract.conversation_missing",
                conversation_id=conversation_id,
            )
            return {"skipped": True, "reason": "missing"}

        if conversation.deleted_at is not None:
            log.info(
                "memory.extract.skip_deleted",
                conversation_id=conversation_id,
            )
            return {"skipped": True, "reason": "deleted"}

        # Double-check sentinelle — idempotence.
        if conversation.memory_extracted_at is not None:
            log.info(
                "memory.extract.already_extracted",
                conversation_id=conversation_id,
            )
            return {"skipped": True, "reason": "already_extracted"}

        # Charger les derniers N messages completed, ordre DESC puis reverse.
        msg_stmt = (
            select(Message)
            .where(
                Message.conversation_id == conv_uuid,
                Message.deleted_at.is_(None),
                Message.status == "completed",
            )
            .order_by(Message.created_at.desc(), Message.id.desc())
            .limit(EXTRACTION_MAX_CONTEXT_MESSAGES)
        )
        result = await db.execute(msg_stmt)
        messages = list(result.scalars().all())
        messages.reverse()

        if len(messages) < EXTRACTION_MIN_MESSAGES:
            # Pas assez de contexte — on laisse la sentinelle NULL pour
            # un ré-enqueue potentiel après le prochain tour de conv.
            log.info(
                "memory.extract.not_enough_messages",
                conversation_id=conversation_id,
                count=len(messages),
                min=EXTRACTION_MIN_MESSAGES,
            )
            return {"skipped": True, "reason": "not_enough_messages"}

        # Charger l'user propriétaire — nécessaire pour MemoryStore.add.
        user_result = await db.execute(select(User).where(User.id == conversation.user_id))
        user = user_result.scalar_one_or_none()
        if user is None:
            # RGPD hard delete récent → la conv existe encore mais l'user non.
            log.warning(
                "memory.extract.user_missing",
                conversation_id=conversation_id,
                user_id=str(conversation.user_id),
            )
            return {"skipped": True, "reason": "user_missing"}

        # Préparer les messages IA pour le LLM.
        ai_messages = [
            AiChatMessage(role=m.role, content=m.content)  # type: ignore[arg-type]
            for m in messages
        ]

        # Appel LLM — fail-safe : la moindre erreur → skip, sentinelle NON
        # posée pour permettre un retry via cron fallback Phase 12.
        try:
            raw_response, provider_name, model_name = await _call_llm_for_facts(ai_messages)
        except Exception as exc:  # noqa: BLE001
            log.warning(
                "memory.extract.llm_failed",
                conversation_id=conversation_id,
                error=str(exc),
                error_type=type(exc).__name__,
            )
            return {"skipped": True, "reason": "llm_failed"}

        facts = _parse_facts_json(raw_response)

        # Pour chaque fait : filtre sensibilité + MemoryStore.add fail-safe.
        inserted = 0
        skipped_sensitive = 0
        skipped_other = 0
        metadata = {
            "extraction_model": model_name,
            "extraction_provider": provider_name,
            "extraction_timestamp": datetime.now(UTC).isoformat(),
            "conversation_message_count": conversation.message_count,
        }

        for fact in facts:
            # Validation longueur métier (post-parse truncate mais on check
            # quand même le min).
            if len(fact) < EXTRACTION_FACT_MIN_CHARS:
                skipped_other += 1
                continue
            # Filtre sensibilité — filet RGPD.
            if _is_sensitive(fact):
                log.info(
                    "memory.extract.sensitive_skipped",
                    conversation_id=conversation_id,
                    fact_preview=fact[:60],
                )
                skipped_sensitive += 1
                continue
            # Insertion via MemoryStore — la dédup SHA gère les doublons
            # cross-conversation automatiquement.
            try:
                await MemoryStore.add(
                    user,
                    db,
                    content=fact,
                    source="extracted",
                    source_conversation_id=conv_uuid,
                    importance=3,
                    metadata_json=metadata,
                )
                inserted += 1
            except (
                MemoryQuotaExceededException,
                RateLimitExceededException,
                EmbeddingsUnavailableException,
                ValidationException,
            ) as exc:
                # Fail-safe : on log + continue la boucle pour les faits
                # suivants. Un user qui a saturé son quota verra juste
                # 0 nouveaux faits indexés cette fois-ci.
                log.warning(
                    "memory.extract.memorystore_error",
                    conversation_id=conversation_id,
                    fact_preview=fact[:60],
                    error_code=getattr(exc, "code", "unknown"),
                )
                skipped_other += 1

        # Pose la sentinelle — même si 0 fait inséré (évite re-tentative
        # infinie sur une conv stérile).
        now = datetime.now(UTC)
        await db.execute(
            update(Conversation)
            .where(
                Conversation.id == conv_uuid,
                Conversation.memory_extracted_at.is_(None),
            )
            .values(memory_extracted_at=now, updated_at=now)
        )
        await db.commit()

    log.info(
        "memory.extract.completed",
        conversation_id=conversation_id,
        facts_extracted=inserted,
        facts_skipped_sensitive=skipped_sensitive,
        facts_skipped_other=skipped_other,
        facts_total_parsed=len(facts),
    )
    return {
        "skipped": False,
        "facts_extracted": inserted,
        "facts_skipped_sensitive": skipped_sensitive,
        "facts_skipped_other": skipped_other,
    }


# ══════════════════════════════════════════════════════════════
# Helper LLM — Gemini Flash via AI Router
# ══════════════════════════════════════════════════════════════


async def _call_llm_for_facts(
    history: list[AiChatMessage],
) -> tuple[str, str, str]:
    """Appelle Gemini Flash et retourne `(raw_response, provider_name, model)`.

    `temperature=0.2` pour une extraction rigoureuse (pas créative).
    `max_tokens=500` bornée pour un JSON à 3 faits courts (typique
    ~150 tokens de sortie).
    """
    resolution = get_ai_router().resolve(EXTRACTION_PROVIDER_KEY)
    request = ChatCompletionRequest(
        messages=history,
        system_prompt=EXTRACTION_SYSTEM_PROMPT,
        model=resolution.model,
        temperature=0.2,
        max_tokens=EXTRACTION_LLM_MAX_TOKENS,
    )
    parts: list[str] = []
    try:
        async for chunk in resolution.provider.stream_chat(request):
            if chunk.delta:
                parts.append(chunk.delta)
    except ProviderError:
        raise
    raw = "".join(parts)
    return raw, resolution.provider.name, resolution.model
