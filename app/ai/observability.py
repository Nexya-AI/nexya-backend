"""
NEXYA Couche IA — Observabilité des streams chat.

Rôle : produire un log structuré unique et riche à la fin de chaque stream,
permettant de répondre depuis Grafana / Kibana à :

- Qui a appelé quoi ? (`user_id`, `trace_id`, `expert_id`)
- Quel provider a répondu ? (`provider`, `model`)
- Combien a coûté la requête ? (`prompt_tokens`, `completion_tokens`, `cost_usd`)
- A-t-on dû basculer en fallback ? (`fallback_used`, `attempts`)
- Combien de temps ? (`first_chunk_ms`, `total_duration_ms`)
- Quel a été le résultat ? (`outcome`: success | cancelled | failed)

Ce n'est PAS un service de métrique Prometheus — c'est une trace riche,
unique par appel, qu'on exploite via l'agrégateur de logs. Les compteurs
Prometheus (QPS, p95, error rate) seront ajoutés dans une phase ultérieure
par-dessus (OpenTelemetry, CLAUDE.md section 7).

Table de prix (`_PRICING`) : USD pour 1M de tokens, prix publics 2026-04.
Utilisée uniquement pour l'estimation — la facturation utilisateur passera
par le CostTracker DB (historique persistant).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import structlog

from app.ai.providers import ChatUsage

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# TABLE DES PRIX — USD par 1M tokens (input / output)
# ═══════════════════════════════════════════════════════════════════
#
# Source : tarifs officiels 2026-Q1 des fournisseurs. À mettre à jour si
# un provider change sa grille. Le fallback "inconnu" retourne (0, 0)
# pour ne pas casser le log — un modèle non référencé coûte "0 USD"
# dans les stats et apparaît dans les warnings `cost.unknown_model`.
# ═══════════════════════════════════════════════════════════════════

_PRICING_USD_PER_1M: dict[tuple[str, str], tuple[float, float]] = {
    # Gemini 2.5
    ("gemini", "gemini-2.5-flash"): (0.075, 0.30),
    ("gemini", "gemini-2.5-pro"): (1.25, 5.00),
    # Gemini 1.5 (fallback legacy)
    ("gemini", "gemini-1.5-flash"): (0.075, 0.30),
    ("gemini", "gemini-1.5-pro"): (1.25, 5.00),
    # OpenAI GPT-4o family
    ("openai", "gpt-4o"): (2.50, 10.00),
    ("openai", "gpt-4o-mini"): (0.15, 0.60),
    ("openai", "gpt-4-turbo"): (10.00, 30.00),
    ("openai", "o1"): (15.00, 60.00),
    ("openai", "o1-mini"): (3.00, 12.00),
    # Anthropic Claude 4
    ("anthropic", "claude-opus-4-6"): (15.00, 75.00),
    ("anthropic", "claude-sonnet-4-6"): (3.00, 15.00),
    ("anthropic", "claude-haiku-4-5"): (0.80, 4.00),
    # Qwen 2.5
    ("qwen", "qwen2.5-72b-instruct"): (0.35, 1.40),
    ("qwen", "qwen2.5-32b-instruct"): (0.20, 0.80),
    ("qwen", "qwen2.5-14b-instruct"): (0.10, 0.40),
    ("qwen", "qwen2.5-7b-instruct"): (0.05, 0.20),
    ("qwen", "qwen-max"): (1.60, 6.40),
}


def estimate_cost_usd(provider: str, model: str, usage: ChatUsage | None) -> float:
    """Coût en USD d'un appel, estimé depuis la grille publique.

    Retourne 0 si `usage` est None (le provider n'a pas renvoyé la conso)
    ou si la paire (provider, model) n'est pas dans la table.
    """
    if usage is None:
        return 0.0
    prices = _PRICING_USD_PER_1M.get((provider, model))
    if prices is None:
        log.warning(
            "ai.cost.unknown_model",
            provider=provider,
            model=model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
        )
        return 0.0
    input_price, output_price = prices
    return (usage.prompt_tokens / 1_000_000 * input_price) + (
        usage.completion_tokens / 1_000_000 * output_price
    )


# ═══════════════════════════════════════════════════════════════════
# ACCUMULATEUR DE MÉTRIQUES
# ═══════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class StreamMetrics:
    """Collecteur de métriques pour un stream — instancié au démarrage,
    enrichi au fil des événements, émis en log à la fermeture."""

    user_id: str
    trace_id: str
    expert_id: str | None
    session_id: str | None = None

    # Renseigné après résolution de la chaîne
    provider: str = ""
    model: str = ""

    # Timing
    started_at: float = field(default_factory=time.monotonic)
    first_chunk_at: float | None = None
    completed_at: float | None = None

    # Comptage transport
    chunks_count: int = 0
    bytes_sent: int = 0

    # Tentatives provider (plusieurs si fallback)
    attempts: int = 0
    fallback_used: bool = False

    # Comptage tokens (renseigné si le provider le remonte)
    usage: ChatUsage | None = None
    cost_usd: float = 0.0

    # Issue
    outcome: str = "in_flight"          # in_flight | success | cancelled | failed
    failure_code: str | None = None     # LLM_UNAVAILABLE, CONTENT_FILTERED, etc.

    # ─── Méthodes d'enrichissement ───────────────────────────────────

    def mark_first_chunk(self) -> None:
        if self.first_chunk_at is None:
            self.first_chunk_at = time.monotonic()

    def record_chunk(self, size_bytes: int = 0) -> None:
        self.chunks_count += 1
        self.bytes_sent += size_bytes
        self.mark_first_chunk()

    def bind_provider(self, provider: str, model: str, *, is_fallback: bool) -> None:
        self.provider = provider
        self.model = model
        self.attempts += 1
        if is_fallback:
            self.fallback_used = True

    def finalize(self, *, outcome: str, failure_code: str | None = None) -> None:
        self.completed_at = time.monotonic()
        self.outcome = outcome
        self.failure_code = failure_code
        self.cost_usd = estimate_cost_usd(self.provider, self.model, self.usage)

    # ─── Émission du log ─────────────────────────────────────────────

    def emit(self) -> None:
        """Émet `ai.chat.completed` avec tous les champs. Idempotent :
        peut être appelé plusieurs fois sans conséquence (la trace est juste
        dupliquée ; utile si on veut logguer tôt en cas d'erreur)."""
        duration_ms = self._ms(self.started_at, self.completed_at or time.monotonic())
        ttfb_ms = self._ms(self.started_at, self.first_chunk_at) if self.first_chunk_at else None

        payload: dict = {
            "user_id": self.user_id,
            "trace_id": self.trace_id,
            "session_id": self.session_id,
            "expert_id": self.expert_id,
            "provider": self.provider or None,
            "model": self.model or None,
            "outcome": self.outcome,
            "failure_code": self.failure_code,
            "attempts": self.attempts,
            "fallback_used": self.fallback_used,
            "chunks_count": self.chunks_count,
            "bytes_sent": self.bytes_sent,
            "first_chunk_ms": ttfb_ms,
            "duration_ms": duration_ms,
            "cost_usd": round(self.cost_usd, 6),
        }
        if self.usage is not None:
            payload["prompt_tokens"] = self.usage.prompt_tokens
            payload["completion_tokens"] = self.usage.completion_tokens
            payload["total_tokens"] = self.usage.total_tokens

        log.info("ai.chat.completed", **payload)

    @staticmethod
    def _ms(start: float, end: float | None) -> int | None:
        if end is None:
            return None
        return max(0, int((end - start) * 1000))
