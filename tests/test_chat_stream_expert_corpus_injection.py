"""
Tests unitaires — hook `build_expert_corpus_context` dans `/chat/stream` (G1).

Vérifie :
- `config.corpus_enabled=True` (expert `language`) → `build_expert_corpus_context`
  est appelé et le bloc est propagé à `StreamContext.expert_corpus_context`.
- Expert `general` (`corpus_enabled=False`) → helper NON appelé, ctx reste None.
- L'ordre `memory → corpus → system` est bien reflété dans le
  `system_prompt_for_check` transmis au token estimator.
- Fail-safe : si le helper raise, le chat continue (None propagé).

On monkey-patche `build_expert_corpus_context` directement pour isoler
le test du vrai provider + DB.
"""

from __future__ import annotations

import pytest

from app.ai.experts import get_expert_config


@pytest.mark.xfail(
    reason=(
        "G1 cleanup 2026-04-24 : scope « Expert Langues via Tatoeba » abandonné "
        "produit, `corpus_enabled=False` posé sur l'expert `language` dans "
        "experts.py. Test maintenu en xfail pour réactivation V2 quand G2 Cuisine "
        "ou G4 Ingénierie ré-activeront un expert avec corpus_enabled=True."
    ),
    strict=True,
)
@pytest.mark.asyncio
async def test_corpus_enabled_expert_triggers_builder(monkeypatch) -> None:
    """Expert `language` (corpus_enabled=True) → helper appelé."""
    from app.features.chat import router as chat_router

    calls: list[dict] = []

    async def fake_builder(*, expert_slug, query, db, **kwargs):
        calls.append({"expert_slug": expert_slug, "query": query})
        return "[INJECTED_CORPUS]"

    monkeypatch.setattr(chat_router, "build_expert_corpus_context", fake_builder)

    cfg = get_expert_config("language")
    assert cfg.corpus_enabled is True

    # Simule le check côté router : `if config.corpus_enabled`
    expert_corpus_context: str | None = None
    if cfg.corpus_enabled:
        expert_corpus_context = await chat_router.build_expert_corpus_context(
            expert_slug=cfg.expert_id,
            query="traduis en espagnol",
            db=object(),
        )
    assert expert_corpus_context == "[INJECTED_CORPUS]"
    assert len(calls) == 1
    assert calls[0]["expert_slug"] == "language"


@pytest.mark.asyncio
async def test_corpus_disabled_expert_skips_builder(monkeypatch) -> None:
    """Expert `general` (corpus_enabled=False) → helper JAMAIS appelé."""
    from app.features.chat import router as chat_router

    calls: list = []

    async def fake_builder(*, expert_slug, query, db, **kwargs):
        calls.append(expert_slug)
        return "[SHOULD_NOT_BE_CALLED]"

    monkeypatch.setattr(chat_router, "build_expert_corpus_context", fake_builder)

    cfg = get_expert_config("general")
    assert cfg.corpus_enabled is False

    expert_corpus_context: str | None = None
    if cfg.corpus_enabled:
        expert_corpus_context = await chat_router.build_expert_corpus_context(
            expert_slug=cfg.expert_id,
            query="hello",
            db=object(),
        )
    assert expert_corpus_context is None
    assert calls == []


def test_concat_order_memory_corpus_system_for_check() -> None:
    """Ordre de concat `memory → corpus → system` réplique `_stream_link`."""
    memory_context = "[MEM]"
    expert_corpus_context = "[CORP]"
    system_prompt = "You are NEXYA Language Expert."

    parts = [memory_context, expert_corpus_context, system_prompt]
    result = "\n\n".join(p for p in parts if p)

    assert result.index("[MEM]") < result.index("[CORP]")
    assert result.index("[CORP]") < result.index("You are NEXYA")


def test_concat_skips_none_segments() -> None:
    parts = [None, "[CORP]", None, "system"]
    result = "\n\n".join(p for p in parts if p)
    assert result == "[CORP]\n\nsystem"


@pytest.mark.asyncio
async def test_builder_exception_does_not_propagate(monkeypatch) -> None:
    """Le helper `build_expert_corpus_context` a sa propre fail-safe (retourne None).

    Ce test documente le contrat : on s'attend à ce que le helper ne
    LAISSE JAMAIS une exception remonter jusqu'au router. On vérifie le
    comportement documenté en simulant le helper qui catche et renvoie None.
    """
    from app.features.experts.context_builder import build_expert_corpus_context

    class _ExplodingProvider:
        name = "boom"
        default_model = "boom"
        dim = 768

        async def embed(self, *args, **kwargs):
            raise RuntimeError("provider died")

    from app.config import settings

    monkeypatch.setattr(settings, "expert_corpus_enabled", True, raising=False)

    result = await build_expert_corpus_context(
        expert_slug="language",
        query="hello",
        db=object(),
        provider=_ExplodingProvider(),
    )
    assert result is None


@pytest.mark.xfail(
    reason=(
        "G1 cleanup 2026-04-24 : `corpus_enabled=False` posé sur expert `language` "
        "(scope Tatoeba abandonné). Voir CLAUDE.md §15 entrée 2026-04-24 — invariant "
        "G1 inversé. Test gardé en xfail strict pour signal de ré-activation V2."
    ),
    strict=True,
)
def test_language_expert_config_corpus_enabled() -> None:
    """Invariant G1 : l'expert `language` a bien `corpus_enabled=True`."""
    cfg = get_expert_config("language")
    assert cfg.corpus_enabled is True
    assert cfg.expert_id == "language"


def test_other_experts_corpus_disabled() -> None:
    """Experts hors `cooking` (G2 actif) : `corpus_enabled=False` tant que la
    session d'activation dédiée (G4/G6/G7) n'a pas eu lieu."""
    for slug in ("general", "computer", "science", "finance"):
        cfg = get_expert_config(slug)
        assert cfg.corpus_enabled is False, (
            f"Expert '{slug}' ne doit pas avoir corpus_enabled=True avant "
            f"la session d'activation dédiée."
        )


def test_cooking_expert_corpus_enabled_post_g2() -> None:
    """Invariant G2 : l'expert `cooking` a `corpus_enabled=True` + bascule Pro."""
    cfg = get_expert_config("cooking")
    assert cfg.corpus_enabled is True
    assert cfg.expert_id == "cooking"
    assert cfg.primary_model == "gemini-2.5-pro", (
        "G2 ancre le RAG sur Pro pour traçabilité des recettes camerounaises."
    )
    assert cfg.tier == "pro"
    assert cfg.max_tokens == 4096
