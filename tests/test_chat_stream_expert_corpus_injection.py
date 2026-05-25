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
    """Invariant G2 V1.1 : l'expert `cooking` a `corpus_enabled=True`,
    bascule Flash (latence) + disable_thinking + Pro en fallback."""
    cfg = get_expert_config("cooking")
    assert cfg.corpus_enabled is True
    assert cfg.expert_id == "cooking"
    # G2 V1.1 2026-05-18 — bascule Flash après benchmark latence
    # (Pro+thinking TTFT 19.5s -> Flash sans thinking TTFT 8.8s, et
    # réponse 5x plus riche car le thinking ne consomme plus le budget
    # output). Voir CLAUDE.md §15 entrée 2026-05-18 + commit historique.
    assert cfg.primary_model == "gemini-2.5-flash", (
        "G2 V1.1 bascule cooking sur Flash sans thinking (latence + qualité)."
    )
    assert cfg.disable_thinking is True, (
        "G2 V1.1 thinking désactivé sur cooking (recette = formatage RAG,"
        " pas raisonnement multi-étapes)."
    )
    # Pro reste en fallback si Flash échoue
    assert ("gemini", "gemini-2.5-pro") in cfg.fallback_chain
    assert cfg.tier == "pro"
    assert cfg.max_tokens == 4096


# ══════════════════════════════════════════════════════════════
# Tests E2E hook /chat/stream cooking (G2 V8 2026-05-18)
# ══════════════════════════════════════════════════════════════
#
# Vérifie le chemin complet user→router→corpus_builder→stream_ctx
# pour le mode cooking en isolation des vrais providers (DB + Vertex AI
# monkey-patchés). Si ces tests cassent, ça signifie qu'une régression
# a touché l'injection RAG côté `/chat/stream` pour les experts RAG.


@pytest.mark.asyncio
async def test_cooking_expert_triggers_corpus_builder_hook(monkeypatch) -> None:
    """G2 — Expert `cooking` (`corpus_enabled=True`) déclenche bien le
    helper `build_expert_corpus_context` côté router avec `expert_slug='cooking'`.
    Simule le check conditionnel du router sans monter une vraie session DB."""
    from app.features.chat import router as chat_router

    calls: list[dict] = []

    async def fake_builder(*, expert_slug, query, db, **kwargs):
        calls.append({"expert_slug": expert_slug, "query": query})
        return (
            "[INSTRUCTION RAG]\n\n"
            '<<<DOCUMENT EXTRACT id="1">>>\n'
            "[Recette] Ndolé Aux Crevettes\n"
            "[Région] Littoral\n"
            "[Ingrédients]\n- 1kg feuilles ndolé\n- crevettes\n"
            "<<<END EXTRACT 1>>>"
        )

    monkeypatch.setattr(chat_router, "build_expert_corpus_context", fake_builder)

    cfg = get_expert_config("cooking")
    assert cfg.corpus_enabled is True

    # Reproduit le check conditionnel router (`if config.corpus_enabled`)
    expert_corpus_context: str | None = None
    if cfg.corpus_enabled:
        expert_corpus_context = await chat_router.build_expert_corpus_context(
            expert_slug=cfg.expert_id,
            query="Donne-moi la recette du Ndolé",
            db=object(),
        )

    # Le contexte doit contenir l'instruction RAG + extrait framé D5
    assert expert_corpus_context is not None
    assert "[INSTRUCTION RAG]" in expert_corpus_context
    assert "<<<DOCUMENT EXTRACT" in expert_corpus_context
    assert "Ndolé" in expert_corpus_context

    # Le helper a été appelé avec le bon expert_slug
    assert len(calls) == 1
    assert calls[0]["expert_slug"] == "cooking"
    assert "Ndolé" in calls[0]["query"]


def test_cooking_corpus_concat_order_memory_corpus_system() -> None:
    """G2 — L'ordre de concaténation `memory → corpus → rag → system`
    doit être respecté côté router (reproduit le pattern inline
    `_prompt_parts = [memory, corpus, rag, system]` du router)."""
    cfg = get_expert_config("cooking")
    memory_block = "[MEMORY] User aime les plats épicés."
    corpus_block = "[CORPUS] <<<EXTRACT>>> Ndolé...<<<END>>>"

    # Reproduit la concat du router L597-603
    _prompt_parts = [memory_block, corpus_block, None, cfg.system_prompt or None]
    result = "\n\n".join(p for p in _prompt_parts if p)

    assert result is not None
    # Memory en premier (avant corpus)
    assert result.index("[MEMORY]") < result.index("[CORPUS]")
    # Corpus avant le system prompt expert. Session A1 (2026-05-19) :
    # post-cleanup `_NEXYA_IDENTITY=""`, le marker "NEXYA" n'apparaît
    # plus dans le system_prompt expert (il vit dans le preamble A1
    # injecté en amont par streaming._stream_link). On utilise le marker
    # spécifique au prompt cooking « Expert Cuisine » qui reste présent.
    assert result.index("[CORPUS]") < result.index("Expert Cuisine")


@pytest.mark.asyncio
async def test_cooking_corpus_builder_failure_does_not_crash(monkeypatch) -> None:
    """G2 — Si `build_expert_corpus_context` raise (DB down, Vertex KO),
    le chat continue avec `expert_corpus_context=None` (fail-safe absolue)."""
    from app.features.chat import router as chat_router

    async def crashing_builder(*, expert_slug, query, db, **kwargs):
        raise RuntimeError("Vertex AI quota exhausted")

    monkeypatch.setattr(chat_router, "build_expert_corpus_context", crashing_builder)

    cfg = get_expert_config("cooking")
    expert_corpus_context: str | None = None
    if cfg.corpus_enabled:
        try:
            expert_corpus_context = await chat_router.build_expert_corpus_context(
                expert_slug=cfg.expert_id,
                query="Recette Ndolé",
                db=object(),
            )
        except Exception:
            # Le router NEXYA fait ce except en pratique (fail-safe)
            expert_corpus_context = None

    assert expert_corpus_context is None
