"""
NEXYA Couche IA — Cache prompt Redis (brique B2).

Le `PromptCache` mémorise la réponse complète d'un LLM pour une paire
`(model, messages)` donnée et la rejoue en streaming simulé si la même
requête retombe dans les 24 h. Économies visées sur un corpus à forte
redondance (FAQ, onboarding, questions scolaires ultra-fréquentes) :
40-60 % du coût LLM effacé, latence divisée par 10 (pas d'attente TTFB
côté provider).

Contrat :
- Clé : `prompt_cache:v1:{sha256(canonical_payload)}` où `canonical_payload`
  est un JSON trié déterministe de `(model, messages, system_prompt,
  temperature, max_tokens, expert_id)`. Deux requêtes équivalentes
  produisent exactement la même clé.
- Valeur : JSON `{text, provider, model, usage}` — on ne rejoue que le
  texte final, pas la séquence exacte des chunks originaux (le consommateur
  peut re-découper s'il veut simuler le streaming).
- TTL : `settings.prompt_cache_ttl_seconds` (24 h par défaut).

Garde-fous critiques (sécurité & éthique) :
- **Jamais** de cache pour les experts tagués `safety-critical`
  (médecine, légal). Chaque réponse doit être recalculée pour que le
  disclaimer soit fraîchement attaché et qu'une erreur sur un user ne
  se propage pas à tous les suivants.
- **Jamais** de cache-put sur une réponse qui a terminé en erreur ou
  filtrage (`error_code != None` ou `status != "completed"`).
- **Jamais** de cache-put sur un `max_tokens` atteint (`LENGTH`) — la
  réponse serait tronquée, rejouer la même troncature à 1 000 users est
  pire que de la régénérer.
- **Jamais** de cache sur un prompt personnalisé (mode "existing
  persisted") qui embarque du contexte utilisateur privé — seuls les
  deltas stateless sont cachés (mode "legacy" ou "new persisted" sans
  historique user).

Fail-open : toute exception Redis est swallowed avec un log warning.
Le cache est une optimisation, jamais un chemin critique. Un Redis
down ne doit jamais bloquer `/chat/stream`.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import structlog

from app.ai.experts import ExpertConfig
from app.ai.providers.base import ChatMessage, ChatUsage
from app.config import settings
from app.core.database.redis import get_redis

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# CONSTANTES
# ═══════════════════════════════════════════════════════════════════

_CACHE_VERSION = "v1"
_CACHE_PREFIX = f"prompt_cache:{_CACHE_VERSION}:"

# Experts sur lesquels on refuse ABSOLUMENT le cache — une réponse
# médicale/juridique fausse servie à 10 000 users ferait plus de dégâts
# qu'un coût LLM multiplié par 10 000.
_SAFETY_CRITICAL_TAG = "safety-critical"


# ═══════════════════════════════════════════════════════════════════
# TYPE DE RETOUR
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class CachedResponse:
    """Réponse LLM complète rejouée depuis le cache."""

    text: str
    provider: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int

    @property
    def usage(self) -> ChatUsage:
        return ChatUsage(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.completion_tokens,
            total_tokens=self.total_tokens,
        )


# ═══════════════════════════════════════════════════════════════════
# PromptCache
# ═══════════════════════════════════════════════════════════════════


class PromptCache:
    """Cache Redis des réponses LLM indexé par hash canonique de la requête.

    API :
    - `is_cacheable(config, messages)` : garde-fous métier (safety-critical +
      historique utilisateur privé). Renvoie `False` pour bypasser
      proprement le cache sans incident.
    - `build_key(...)` : hash déterministe `(model, messages, sys_prompt,
      temperature, max_tokens, expert_id)`. Deux requêtes équivalentes →
      même clé.
    - `get(key)` : lookup Redis, retourne `CachedResponse | None`.
    - `put(key, response)` : set avec TTL. Rejette si la réponse est
      invalide (erreur, troncature, texte vide).
    - `invalidate(key)` : delete explicite (utile si on détecte qu'une
      réponse cachée est fausse — feedback user, audit, etc.).

    Toutes les opérations Redis sont fail-open : sur exception, on log
    warning et on renvoie `None` / no-op.
    """

    def __init__(
        self,
        *,
        ttl_seconds: int | None = None,
        enabled: bool | None = None,
    ) -> None:
        self._ttl = ttl_seconds if ttl_seconds is not None else settings.prompt_cache_ttl_seconds
        self._enabled = enabled if enabled is not None else settings.prompt_cache_enabled

    # ─── Introspection ────────────────────────────────────────────

    @property
    def enabled(self) -> bool:
        return self._enabled

    @property
    def ttl_seconds(self) -> int:
        return self._ttl

    # ─── Garde-fous métier ────────────────────────────────────────

    def is_cacheable(
        self,
        config: ExpertConfig,
        messages: Sequence[ChatMessage],
    ) -> bool:
        """Renvoie True si la paire `(expert, messages)` peut être cachée.

        Critères de rejet :
        - Kill-switch `prompt_cache_enabled=False`.
        - Expert tagué `safety-critical` (médecine, légal).
        - Conversation "existing persisted" avec historique — heuristique :
          si la séquence contient plus d'un tour user+assistant, c'est
          du contenu personnalisé qu'on ne cross-cache pas entre users.
          On cache uniquement les requêtes "one-shot" (1 ou 2 messages).
        """
        if not self._enabled:
            return False
        if _SAFETY_CRITICAL_TAG in config.tags:
            return False
        if _count_user_turns(messages) > 1:
            return False
        return True

    # ─── Clé de cache ─────────────────────────────────────────────

    @staticmethod
    def build_key(
        *,
        model: str,
        messages: Sequence[ChatMessage],
        system_prompt: str | None,
        temperature: float,
        max_tokens: int | None,
        expert_id: str,
    ) -> str:
        """Construit la clé Redis canonique pour cette requête.

        La canonicalisation (JSON trié + séparateurs fixes) garantit que
        deux requêtes sémantiquement identiques produisent le même hash,
        même si l'ordre des kwargs diffère côté caller.
        """
        payload: dict[str, Any] = {
            "model": model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "system_prompt": system_prompt or "",
            "temperature": round(float(temperature), 4),
            "max_tokens": max_tokens,
            "expert_id": expert_id,
        }
        serialized = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        digest = hashlib.sha256(serialized.encode("utf-8")).hexdigest()
        return f"{_CACHE_PREFIX}{digest}"

    # ─── Lecture ──────────────────────────────────────────────────

    async def get(self, key: str) -> CachedResponse | None:
        """Retourne la réponse cachée ou `None` si absente/illisible.

        Fail-open : sur exception Redis ou JSON corrompu, on log et on
        renvoie `None`. Le caller appellera simplement le LLM normalement.
        """
        if not self._enabled:
            return None
        try:
            raw = await get_redis().get(key)
        except Exception as exc:  # noqa: BLE001 — fail-open obligatoire
            log.warning("ai.cache.get_failed", error=str(exc), key=key)
            return None
        if raw is None:
            return None
        try:
            data = json.loads(raw)
            return CachedResponse(
                text=data["text"],
                provider=data["provider"],
                model=data["model"],
                prompt_tokens=int(data.get("prompt_tokens", 0)),
                completion_tokens=int(data.get("completion_tokens", 0)),
                total_tokens=int(data.get("total_tokens", 0)),
            )
        except (ValueError, KeyError, TypeError) as exc:
            log.warning("ai.cache.corrupted_entry", error=str(exc), key=key)
            try:
                await get_redis().delete(key)
            except Exception:  # noqa: BLE001
                pass
            return None

    # ─── Écriture ─────────────────────────────────────────────────

    async def put(
        self,
        key: str,
        *,
        text: str,
        provider: str,
        model: str,
        usage: ChatUsage | None = None,
        status: str = "completed",
        error_code: str | None = None,
        finish_reason: str | None = None,
    ) -> bool:
        """Stocke la réponse en cache si elle est valide.

        Rejette silencieusement (sans lever) les cas où le cache serait
        dangereux ou inutile :
        - Cache désactivé.
        - Texte vide ou whitespace-only.
        - Réponse en erreur (`status != "completed"` ou `error_code` posé).
        - Troncature (`finish_reason == "length"`) — un `max_tokens` atteint
          signale une réponse incomplète qu'il ne faut pas figer.

        Retourne `True` si la clé a bien été posée, `False` sinon.
        """
        if not self._enabled:
            return False
        if not text or not text.strip():
            return False
        if status != "completed" or error_code:
            return False
        if finish_reason and finish_reason.lower() == "length":
            return False
        payload = {
            "text": text,
            "provider": provider,
            "model": model,
            "prompt_tokens": usage.prompt_tokens if usage else 0,
            "completion_tokens": usage.completion_tokens if usage else 0,
            "total_tokens": usage.total_tokens if usage else 0,
        }
        try:
            await get_redis().set(
                key,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                ex=self._ttl,
            )
            return True
        except Exception as exc:  # noqa: BLE001 — fail-open
            log.warning("ai.cache.put_failed", error=str(exc), key=key)
            return False

    # ─── Invalidation ─────────────────────────────────────────────

    async def invalidate(self, key: str) -> None:
        """Supprime une entrée (feedback négatif, audit, incident)."""
        try:
            await get_redis().delete(key)
        except Exception as exc:  # noqa: BLE001
            log.warning("ai.cache.invalidate_failed", error=str(exc), key=key)


# ═══════════════════════════════════════════════════════════════════
# Helpers internes
# ═══════════════════════════════════════════════════════════════════


def _count_user_turns(messages: Sequence[ChatMessage]) -> int:
    """Nombre de messages `role=='user'` dans la séquence.

    Sert à distinguer une requête stateless (1 tour user) d'une conv
    persistée multi-tours (>1 tour user = contexte privé, on ne cache pas).
    """
    return sum(1 for m in messages if m.role == "user")


# ═══════════════════════════════════════════════════════════════════
# Singleton process-wide
# ═══════════════════════════════════════════════════════════════════

_cache_instance: PromptCache | None = None


def get_prompt_cache() -> PromptCache:
    """Accesseur partagé — même instance pour tous les endpoints."""
    global _cache_instance
    if _cache_instance is None:
        _cache_instance = PromptCache()
    return _cache_instance


def _reset_cache_for_tests() -> None:
    """Réinitialise le singleton — à appeler uniquement dans les tests."""
    global _cache_instance
    _cache_instance = None
