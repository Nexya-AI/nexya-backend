"""
NEXYA Couche IA — CircuitBreaker par (provider, modèle).

Quand un provider enchaîne N échecs consécutifs (timeout, 5xx, connexion
refusée), continuer à l'appeler est contre-productif : on perd du temps,
on ajoute à la charge d'un service déjà en galère, et on dégrade l'UX
pour tous les users. Le CircuitBreaker coupe automatiquement ces appels
pendant une période de cooldown, puis teste la reprise.

État machine par clé `(provider, model)` :
    CLOSED → (N échecs consécutifs) → OPEN
    OPEN   → (cooldown écoulé)     → HALF_OPEN
    HALF_OPEN → (1 succès)          → CLOSED
    HALF_OPEN → (1 échec)           → OPEN (réinitialise le cooldown)

Le `LlmRouter` peut skip une entrée `open` de la chaîne et passer au
fallback. C'est la défense en profondeur qui fait qu'une panne de
Gemini ne coupe pas NEXYA si OpenAI répond.

État stocké en mémoire process — suffisant pour un API mono-worker,
et le mode HALF_OPEN permet de se remettre rapidement si l'état
se désynchronise entre workers. Pour une vraie synchronisation
multi-worker, passer sur Redis dans une version ultérieure.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from threading import RLock

import structlog

from app.ai.providers import ProviderError

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# ÉTATS
# ═══════════════════════════════════════════════════════════════════


class CircuitState(StrEnum):
    CLOSED = "closed"  # Tout va bien, les appels passent
    OPEN = "open"  # Coupure active, les appels sont rejetés
    HALF_OPEN = "half_open"  # Test de reprise : un seul appel autorisé


# ═══════════════════════════════════════════════════════════════════
# EXCEPTION SPÉCIFIQUE
# ═══════════════════════════════════════════════════════════════════


class CircuitOpenError(ProviderError):
    """Levée quand on tente d'appeler un breaker OPEN.

    Typée `retryable=False` parce que la couche `retry.py` ne doit PAS
    retenter — c'est au `LlmRouter` de skip vers le fallback suivant.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str | None,
        reopen_in_seconds: float,
    ) -> None:
        super().__init__(
            f"Circuit breaker ouvert pour {provider}/{model} — "
            f"réouverture dans {reopen_in_seconds:.1f}s.",
            provider=provider,
            model=model,
            retryable=False,
            status_code=503,
        )
        self.reopen_in_seconds = reopen_in_seconds


# ═══════════════════════════════════════════════════════════════════
# CONFIGURATION
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class BreakerConfig:
    """Paramètres d'un circuit.

    - `failure_threshold=5` : cinq échecs consécutifs ouvrent le circuit.
      En-dessous, on tolère les erreurs isolées (flakiness réseau).
    - `cooldown_seconds=30` : après ouverture, on attend 30s avant de
      laisser une requête sonder la reprise.
    - `half_open_max_trials=1` : un seul essai en half-open — si ça passe,
      on referme ; sinon on reprolonge de `cooldown_seconds`.
    """

    failure_threshold: int = 5
    cooldown_seconds: float = 30.0
    half_open_max_trials: int = 1


DEFAULT_CONFIG = BreakerConfig()


# ═══════════════════════════════════════════════════════════════════
# CIRCUIT PAR CLÉ
# ═══════════════════════════════════════════════════════════════════


@dataclass(slots=True)
class _CircuitStats:
    state: CircuitState = CircuitState.CLOSED
    consecutive_failures: int = 0
    opened_at: float = 0.0
    half_open_trials_in_flight: int = 0
    last_error: str = ""
    # Métadonnées pour introspection (logs / monitoring futur)
    opens_count: int = 0
    total_failures: int = 0
    total_successes: int = 0
    _lock: RLock = field(default_factory=RLock, repr=False)


# ═══════════════════════════════════════════════════════════════════
# REGISTRY — manager thread-safe
# ═══════════════════════════════════════════════════════════════════


class CircuitBreakerRegistry:
    """Factory centrale des circuits. Thread-safe.

    Utilisation type (orchestrée par la brique 6 / QueryEngine) :

        registry = get_breaker_registry()
        registry.before_call("gemini", "gemini-2.5-flash")
        try:
            ... stream_chat ...
            registry.record_success("gemini", "gemini-2.5-flash")
        except ProviderError as exc:
            registry.record_failure("gemini", "gemini-2.5-flash", exc)
            raise
    """

    def __init__(self, config: BreakerConfig = DEFAULT_CONFIG) -> None:
        self._config = config
        self._registry_lock = RLock()
        self._circuits: dict[tuple[str, str], _CircuitStats] = {}

    # ─── API publique ────────────────────────────────────────────────

    def before_call(self, provider: str, model: str) -> None:
        """À appeler AVANT d'invoquer le provider.

        Lève `CircuitOpenError` si le circuit est OPEN et que le cooldown
        n'est pas écoulé. Transitionne en HALF_OPEN sinon, et autorise
        une tentative.
        """
        stats = self._get_or_create(provider, model)
        with stats._lock:
            now = time.monotonic()
            if stats.state is CircuitState.OPEN:
                elapsed = now - stats.opened_at
                if elapsed < self._config.cooldown_seconds:
                    raise CircuitOpenError(
                        provider=provider,
                        model=model,
                        reopen_in_seconds=self._config.cooldown_seconds - elapsed,
                    )
                # Cooldown écoulé → transition half-open
                stats.state = CircuitState.HALF_OPEN
                stats.half_open_trials_in_flight = 0
                log.info(
                    "ai.circuit.half_open",
                    provider=provider,
                    model=model,
                    after_seconds=round(elapsed, 2),
                )

            if stats.state is CircuitState.HALF_OPEN:
                if stats.half_open_trials_in_flight >= self._config.half_open_max_trials:
                    # Un essai est déjà en vol — rejet pour ne pas flooder
                    raise CircuitOpenError(
                        provider=provider,
                        model=model,
                        reopen_in_seconds=1.0,
                    )
                stats.half_open_trials_in_flight += 1

    def record_success(self, provider: str, model: str) -> None:
        stats = self._get_or_create(provider, model)
        with stats._lock:
            stats.total_successes += 1
            stats.consecutive_failures = 0
            previous = stats.state
            stats.state = CircuitState.CLOSED
            stats.half_open_trials_in_flight = 0
            if previous is not CircuitState.CLOSED:
                log.info(
                    "ai.circuit.closed",
                    provider=provider,
                    model=model,
                    from_state=previous.value,
                )

    def record_failure(
        self,
        provider: str,
        model: str,
        error: Exception,
    ) -> None:
        """Enregistre un échec. Si c'est une ProviderError non-retryable
        (auth, content_filter, invalid_request), on n'ouvre PAS le circuit :
        c'est un bug côté NEXYA, pas une panne du provider.
        """
        if isinstance(error, ProviderError) and not error.retryable:
            return

        stats = self._get_or_create(provider, model)
        with stats._lock:
            stats.total_failures += 1
            stats.last_error = f"{type(error).__name__}: {error}"

            if stats.state is CircuitState.HALF_OPEN:
                stats.half_open_trials_in_flight = max(0, stats.half_open_trials_in_flight - 1)
                stats.state = CircuitState.OPEN
                stats.opened_at = time.monotonic()
                stats.opens_count += 1
                log.warning(
                    "ai.circuit.reopened_after_trial",
                    provider=provider,
                    model=model,
                    error=stats.last_error,
                )
                return

            stats.consecutive_failures += 1
            if (
                stats.state is CircuitState.CLOSED
                and stats.consecutive_failures >= self._config.failure_threshold
            ):
                stats.state = CircuitState.OPEN
                stats.opened_at = time.monotonic()
                stats.opens_count += 1
                log.warning(
                    "ai.circuit.opened",
                    provider=provider,
                    model=model,
                    consecutive_failures=stats.consecutive_failures,
                    cooldown_seconds=self._config.cooldown_seconds,
                    error=stats.last_error,
                )

    def state(self, provider: str, model: str) -> CircuitState:
        stats = self._get_or_create(provider, model)
        with stats._lock:
            return stats.state

    def is_open(self, provider: str, model: str) -> bool:
        """Non-bloquant : retourne True si la prochaine `before_call` lèverait.
        Utilisable par le LlmRouter pour filtrer préventivement la chaîne.
        """
        stats = self._get_or_create(provider, model)
        with stats._lock:
            if stats.state is not CircuitState.OPEN:
                return False
            return (time.monotonic() - stats.opened_at) < self._config.cooldown_seconds

    def reset(self, provider: str | None = None, model: str | None = None) -> None:
        """Réinitialise un circuit (ou tous). Utile pour les tests ou
        pour un endpoint admin de remise à zéro manuelle."""
        with self._registry_lock:
            if provider is None:
                self._circuits.clear()
                return
            if model is None:
                keys_to_drop = [k for k in self._circuits if k[0] == provider]
            else:
                keys_to_drop = [(provider, model)]
            for key in keys_to_drop:
                self._circuits.pop(key, None)

    # ─── Internes ────────────────────────────────────────────────────

    def _get_or_create(self, provider: str, model: str) -> _CircuitStats:
        key = (provider, model)
        with self._registry_lock:
            stats = self._circuits.get(key)
            if stats is None:
                stats = _CircuitStats()
                self._circuits[key] = stats
            return stats


# ═══════════════════════════════════════════════════════════════════
# SINGLETON
# ═══════════════════════════════════════════════════════════════════

_registry: CircuitBreakerRegistry | None = None


def get_breaker_registry() -> CircuitBreakerRegistry:
    global _registry
    if _registry is None:
        _registry = CircuitBreakerRegistry()
    return _registry
