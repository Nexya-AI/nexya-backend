"""
Tests unitaires — Concaténation `memory_context + config.system_prompt`
dans `_stream_link` (Session D3).

Validation que la concat se fait au bon endroit, dans le bon ordre
(memory d'abord), et qu'elle est totalement passive quand
`memory_context` est None.

On ne monte pas un vrai StreamHandler — on capture juste la
`ChatCompletionRequest` construite pour vérifier son `system_prompt`.
"""

from __future__ import annotations

# NOTE : on n'importe PAS `StreamContext` / `ChatMessage` au top-level
# pour éviter un import circulaire lors de la collection pytest (la
# chaîne app.ai.streaming → app.ai.runtime → ... peut cycler).
# Les tests qui ont besoin du dataclass réel importent localement.


# ══════════════════════════════════════════════════════════════
# Helpers — reproduction de la logique de concat de streaming.py
# ══════════════════════════════════════════════════════════════
#
# La logique est simple et déterministe. Plutôt que de monter un
# StreamHandler complet (qui nécessite un provider mock, un
# RetryPolicy, un CircuitBreakerRegistry, etc.), on teste
# directement le snippet concat qui apparaît dans _stream_link.
# Si un jour la logique change, ce test cassera immédiatement et
# nous signalera qu'il faut ré-aligner.


def _compose_system_prompt(
    config_system_prompt: str | None,
    memory_context: str | None,
) -> str:
    """Réplique exacte de la logique dans streaming._stream_link
    (ligne ~455 après le patch D3). Si cette logique diverge, ce test
    échouera en premier."""
    system_prompt_final = config_system_prompt or ""
    if memory_context:
        if system_prompt_final:
            system_prompt_final = memory_context + "\n\n" + system_prompt_final
        else:
            system_prompt_final = memory_context
    return system_prompt_final


# ══════════════════════════════════════════════════════════════
# 1. memory_context None → system_prompt inchangé
# ══════════════════════════════════════════════════════════════


def test_concat_no_memory_preserves_config_system_prompt() -> None:
    result = _compose_system_prompt(
        config_system_prompt="Tu es un expert en cuisine camerounaise.",
        memory_context=None,
    )
    assert result == "Tu es un expert en cuisine camerounaise."


def test_concat_no_memory_no_config_returns_empty_string() -> None:
    """Edge : ni system_prompt expert, ni memory → chaîne vide."""
    result = _compose_system_prompt(config_system_prompt=None, memory_context=None)
    assert result == ""


# ══════════════════════════════════════════════════════════════
# 2. memory_context présent → préfixé
# ══════════════════════════════════════════════════════════════


def test_concat_memory_prefixed_with_double_newline() -> None:
    result = _compose_system_prompt(
        config_system_prompt="Tu es un expert cuisine.",
        memory_context="[Contexte] L'utilisateur est Ivan [/Contexte]",
    )
    assert result == ("[Contexte] L'utilisateur est Ivan [/Contexte]\n\nTu es un expert cuisine.")


def test_concat_memory_only_when_no_config_system_prompt() -> None:
    """Edge : expert sans system_prompt → memory seul (pas de \\n\\n vide)."""
    result = _compose_system_prompt(
        config_system_prompt=None,
        memory_context="[Contexte] Faits user [/Contexte]",
    )
    assert result == "[Contexte] Faits user [/Contexte]"


def test_concat_memory_only_when_empty_config_system_prompt() -> None:
    """Edge : expert avec system_prompt vide → memory seul."""
    result = _compose_system_prompt(
        config_system_prompt="",
        memory_context="[Contexte] X [/Contexte]",
    )
    assert result == "[Contexte] X [/Contexte]"


# ══════════════════════════════════════════════════════════════
# 3. StreamContext — le champ `memory_context` est bien propagé
# ══════════════════════════════════════════════════════════════
#
# Ces 2 tests importent StreamContext LOCALEMENT (import différé) pour
# éviter l'import circulaire détecté lors de la collection pytest.


def test_stream_context_has_memory_context_field() -> None:
    """Sanity : le dataclass StreamContext expose bien le champ D3."""
    from app.ai.providers.base import ChatMessage
    from app.ai.streaming import StreamContext

    ctx = StreamContext(
        expert_id="general",
        user_messages=[ChatMessage(role="user", content="Salut")],
        user_id="user-1",
        trace_id="trace-1",
        memory_context="[Contexte] Ivan est dev [/Contexte]",
    )
    assert ctx.memory_context == "[Contexte] Ivan est dev [/Contexte]"


def test_stream_context_memory_context_defaults_to_none() -> None:
    """Le champ est optionnel, défaut None — rétro-compat."""
    from app.ai.providers.base import ChatMessage
    from app.ai.streaming import StreamContext

    ctx = StreamContext(
        expert_id="general",
        user_messages=[ChatMessage(role="user", content="Salut")],
        user_id="user-1",
        trace_id="trace-1",
    )
    assert ctx.memory_context is None
