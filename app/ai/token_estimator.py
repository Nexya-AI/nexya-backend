"""
NEXYA Couche IA — Estimation pré-appel des tokens + coût (brique B2).

Rôle : AVANT d'envoyer un prompt au LLM, estimer combien de tokens il
coûtera pour :

1. **Bloquer les abus** — un user dont le prochain appel dépasserait son
   budget mensuel est coupé par un `LlmQuotaExceededException` (402)
   **avant** que le provider facture.
2. **Guider le routage** — un prompt trop long pour Flash (>8k tokens)
   est dévié vers Pro (contexte >128k) sans que le provider renvoie
   une 400 après avoir consommé de la bande passante.
3. **Budgéter la sortie** — `max_tokens` raisonnable à passer au
   provider selon le budget restant de l'utilisateur.

Pourquoi pas juste "appeler le LLM et voir" ?
- Coût : un appel GPT-4o à 8k tokens en entrée ≈ $0.02 — multiplié par
  950 k users × 50 req/jour = $950 000/jour **avant** de connaître leur
  budget. Estimer en amont ramène ça à zéro.
- UX : un user qui franchit son quota doit recevoir une erreur claire
  et instantanée, pas attendre 5 s pour apprendre qu'on annule.

Stratégie par provider (conforme à la réalité SDK 2026-04) :

- **OpenAI** : `tiktoken` exact (BPE officiel, chargé en lazy). Support
  des modèles `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo` via l'encoder
  `o200k_base`, et `o1`/`o1-mini` via le même (Reasoning models partagent
  ce BPE). Couvre 100 % des appels OpenAI facturés.
- **Qwen** : tokenizer Qwen pas trivial à embarquer côté Python sans
  dépendre de `transformers` (trop lourd pour un sidecar API). On utilise
  `tiktoken` avec `cl100k_base` — approximation raisonnable pour un texte
  mixte FR/EN/ZH (Qwen est entraîné à partir d'un BPE proche de celui
  de GPT-3.5). Biais : ~10 % de sur-estimation sur le chinois, ~5 % sur
  le français. Acceptable pour un garde-fou budget.
- **Anthropic** : le SDK expose un endpoint `client.messages.count_tokens`
  mais il est facturé (sic). En première version on utilise l'heuristique
  `chars / settings.token_estimate_chars_per_token` (3.0 par défaut),
  majorée de +15 % pour être pessimiste. Si une dérive est observée,
  on bascule vers l'endpoint officiel en version 2.
- **Gemini** : idem Anthropic — l'API expose `countTokens` mais on
  préfère l'heuristique pour éviter un aller-retour réseau bloquant le
  TTFB.

Toutes les valeurs retournées sont des `int` — on arrondit au supérieur
pour rester du côté "safe" (sur-estimation = budget plus strict, OK ;
sous-estimation = user facturé au-delà de son budget, pas OK).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from app.ai.observability import estimate_cost_usd
from app.ai.providers.base import ChatMessage, ChatUsage
from app.config import settings

if TYPE_CHECKING:
    import tiktoken

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# CONSTANTES
# ═══════════════════════════════════════════════════════════════════

# Surcoût par message : OpenAI documente ~3 tokens par message (rôle +
# séparateurs) + ~3 tokens pour démarrer la réponse. On applique le même
# ordre de grandeur pour tous les providers.
_TOKENS_PER_MESSAGE_OVERHEAD = 3
_REPLY_PRIMER_TOKENS = 3

# Facteur de sécurité appliqué aux heuristiques non-tiktoken — on préfère
# sur-estimer de 15 % que sous-estimer et facturer par surprise.
_HEURISTIC_SAFETY_MULTIPLIER = 1.15

# Provision par défaut pour la completion quand on n'a pas de max_tokens
# explicite. 1 024 tokens de sortie = réponse chat typique (~4 000 chars).
_DEFAULT_COMPLETION_TOKENS = 1_024

# Mapping provider → encoder tiktoken. Tous les modèles récents OpenAI et
# la quasi-totalité des APIs compat OpenAI utilisent `o200k_base` ou
# `cl100k_base`. On utilise `o200k_base` en priorité pour gpt-4o qui est
# notre modèle principal côté OpenAI.
_TIKTOKEN_ENCODER_BY_PROVIDER: dict[str, str] = {
    "openai": "o200k_base",
    "qwen": "cl100k_base",
}


# ═══════════════════════════════════════════════════════════════════
# RÉSULTAT D'ESTIMATION
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class TokenEstimate:
    """Résultat d'une estimation pré-appel.

    - `prompt_tokens` : tokens en entrée (système + historique + user).
    - `max_completion_tokens` : ce qu'on s'autorise à générer en sortie
      (selon `max_tokens` fourni ou défaut).
    - `estimated_total_tokens` : somme — utilisée pour comparer à la
      fenêtre de contexte du modèle.
    - `estimated_cost_usd` : coût worst-case (max_completion_tokens
      complètement consommés).
    """

    provider: str
    model: str
    prompt_tokens: int
    max_completion_tokens: int

    @property
    def estimated_total_tokens(self) -> int:
        return self.prompt_tokens + self.max_completion_tokens

    @property
    def estimated_cost_usd(self) -> float:
        synthetic = ChatUsage(
            prompt_tokens=self.prompt_tokens,
            completion_tokens=self.max_completion_tokens,
            total_tokens=self.estimated_total_tokens,
        )
        return estimate_cost_usd(self.provider, self.model, synthetic)


# ═══════════════════════════════════════════════════════════════════
# API PUBLIQUE
# ═══════════════════════════════════════════════════════════════════


def estimate_prompt_tokens(
    *,
    provider: str,
    model: str,
    messages: Sequence[ChatMessage],
    system_prompt: str | None = None,
) -> int:
    """Nombre estimé de tokens pour les messages d'entrée.

    Découpe le calcul en (a) texte brut compté par l'encoder approprié,
    (b) surcoût de structure (séparateurs par message + primer assistant).

    Ne lève jamais : si tiktoken échoue ou n'est pas installé, on log un
    warning et on bascule sur l'heuristique caractères → tokens.
    """
    texts: list[str] = []
    if system_prompt:
        texts.append(system_prompt)
    for m in messages:
        texts.append(m.content)

    encoder_name = _TIKTOKEN_ENCODER_BY_PROVIDER.get(provider)
    if encoder_name is not None:
        encoder = _load_tiktoken_encoder(encoder_name)
        if encoder is not None:
            try:
                total = sum(len(encoder.encode(t)) for t in texts)
                structural = (
                    len(messages) + (1 if system_prompt else 0)
                ) * _TOKENS_PER_MESSAGE_OVERHEAD + _REPLY_PRIMER_TOKENS
                return total + structural
            except Exception as exc:  # noqa: BLE001 — on bascule en heuristique
                log.warning(
                    "ai.token_estimator.tiktoken_encode_failed",
                    provider=provider,
                    model=model,
                    error=str(exc),
                )

    return _estimate_heuristic(texts, messages_count=len(messages) + (1 if system_prompt else 0))


def estimate_completion_budget(
    *,
    requested_max_tokens: int | None,
) -> int:
    """Nombre de tokens de sortie qu'on provisionne pour l'appel."""
    if requested_max_tokens is None or requested_max_tokens <= 0:
        return _DEFAULT_COMPLETION_TOKENS
    return int(requested_max_tokens)


def estimate(
    *,
    provider: str,
    model: str,
    messages: Sequence[ChatMessage],
    system_prompt: str | None = None,
    max_tokens: int | None = None,
) -> TokenEstimate:
    """Estimation complète : prompt + completion + coût worst-case."""
    prompt_tokens = estimate_prompt_tokens(
        provider=provider,
        model=model,
        messages=messages,
        system_prompt=system_prompt,
    )
    completion = estimate_completion_budget(requested_max_tokens=max_tokens)
    return TokenEstimate(
        provider=provider,
        model=model,
        prompt_tokens=prompt_tokens,
        max_completion_tokens=completion,
    )


# ═══════════════════════════════════════════════════════════════════
# INTERNE — Chargement lazy tiktoken
# ═══════════════════════════════════════════════════════════════════

_tiktoken_cache: dict[str, tiktoken.Encoding] = {}
_tiktoken_unavailable_logged = False


def _load_tiktoken_encoder(encoder_name: str) -> tiktoken.Encoding | None:
    """Charge (et mémoïse) l'encoder tiktoken demandé.

    Import lazy pour que le module soit utilisable même si tiktoken n'est
    pas encore installé dans l'environnement (fail-open : on bascule sur
    l'heuristique, on log un warning unique).
    """
    global _tiktoken_unavailable_logged
    if encoder_name in _tiktoken_cache:
        return _tiktoken_cache[encoder_name]
    try:
        import tiktoken as _tiktoken
    except ImportError:
        if not _tiktoken_unavailable_logged:
            log.warning("ai.token_estimator.tiktoken_not_installed")
            _tiktoken_unavailable_logged = True
        return None
    try:
        encoder = _tiktoken.get_encoding(encoder_name)
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "ai.token_estimator.tiktoken_encoder_load_failed",
            encoder=encoder_name,
            error=str(exc),
        )
        return None
    _tiktoken_cache[encoder_name] = encoder
    return encoder


def _reset_tiktoken_cache_for_tests() -> None:
    """Reset du cache — pour que les tests puissent monkeypatch tiktoken."""
    global _tiktoken_unavailable_logged
    _tiktoken_cache.clear()
    _tiktoken_unavailable_logged = False


# ═══════════════════════════════════════════════════════════════════
# INTERNE — Heuristique caractères → tokens (fallback universel)
# ═══════════════════════════════════════════════════════════════════


def _estimate_heuristic(texts: Sequence[str], *, messages_count: int) -> int:
    """Estimation caractères / ratio + marge de sécurité +15 %.

    Utilisée pour Gemini, Anthropic, ou en secours si tiktoken plante.
    Le ratio vient de `settings.token_estimate_chars_per_token` (3.0
    par défaut = pessimiste mais safe).
    """
    chars_per_token = max(settings.token_estimate_chars_per_token, 1.0)
    total_chars = sum(len(t) for t in texts)
    raw_tokens = total_chars / chars_per_token
    structural = messages_count * _TOKENS_PER_MESSAGE_OVERHEAD + _REPLY_PRIMER_TOKENS
    return math.ceil(raw_tokens * _HEURISTIC_SAFETY_MULTIPLIER) + structural
