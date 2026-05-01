"""
Tests N2 — `app.ai.router.LlmRouter` : résolution expert → provider/model/config.

Couvre :
1. Constructeur — refus chat_providers vide, copie défensive des dicts.
2. `resolve(expert_id)` — happy path, fallback general, expert image-only,
   chaîne entièrement non-viable → RouterError.
3. `build_chain(expert_id)` — primaire + fallbacks viables uniquement.
4. `resolve_image(expert_id)` — happy path Studio, expert sans image provider.
5. Filtrage chaîne : provider non enregistré → skip silencieux,
   modèle hors `supported_models` → garde mais log warning.
6. Introspection — has_chat/image_provider, *_names triés.
7. `build_default_router()` — mock-first quand toutes les clés sont vides,
   identité usurpée par les Mock providers.

Aucune dépendance DB, aucun appel LLM. Tests sur un router instancié à
la main avec MockChatProvider.
"""

from __future__ import annotations

import pytest

from app.ai.experts import EXPERT_REGISTRY, ExpertConfig
from app.ai.providers import (
    ChatProvider,
    GeminiImageProvider,
    ImageProvider,
    MockChatProvider,
)
from app.ai.providers.base import GeneratedImage, ImageGenerationRequest
from app.ai.router import (
    ChatResolution,
    ImageResolution,
    LlmRouter,
    RouterError,
    build_default_router,
)

# ══════════════════════════════════════════════════════════════
# Helpers — fabrique de Mocks usurpant l'identité des providers réels
# ══════════════════════════════════════════════════════════════


def _gemini_mock() -> MockChatProvider:
    return MockChatProvider(
        name="gemini",
        default_model="gemini-2.5-flash",
        supported_models=frozenset({"gemini-2.5-flash", "gemini-2.5-pro"}),
        max_context_tokens=1_048_576,
    )


def _openai_mock() -> MockChatProvider:
    return MockChatProvider(
        name="openai",
        default_model="gpt-4o",
        supported_models=frozenset({"gpt-4o", "gpt-4o-mini"}),
        max_context_tokens=128_000,
    )


def _openrouter_mock() -> MockChatProvider:
    return MockChatProvider(
        name="openrouter",
        default_model="anthropic/claude-3.5-sonnet",
        supported_models=frozenset(
            {
                "anthropic/claude-3.5-sonnet",
                "meta-llama/llama-3.1-70b-instruct",
            }
        ),
        max_context_tokens=128_000,
    )


class _FakeImageProvider(ImageProvider):
    name = "gemini-imagen"
    default_model = "imagen-3.0-generate-002"
    supported_models = frozenset({"imagen-3.0-generate-002"})

    async def generate_images(self, request: ImageGenerationRequest) -> list[GeneratedImage]:
        return []


# ══════════════════════════════════════════════════════════════
# 1. Constructeur
# ══════════════════════════════════════════════════════════════


def test_router_init_refuses_empty_chat_providers() -> None:
    with pytest.raises(RouterError):
        LlmRouter(chat_providers={})


def test_router_init_with_only_chat_providers_no_image() -> None:
    router = LlmRouter(chat_providers={"gemini": _gemini_mock()})
    assert router.has_chat_provider("gemini") is True
    assert router.image_provider_names() == []


def test_router_init_copies_chat_providers_dict_defensively() -> None:
    """Un caller qui mute le dict après construction ne doit PAS pouvoir
    affecter le routage."""
    src: dict[str, ChatProvider] = {"gemini": _gemini_mock()}
    router = LlmRouter(chat_providers=src)
    src.pop("gemini")  # mutation après coup
    assert router.has_chat_provider("gemini") is True


def test_router_init_copies_image_providers_dict_defensively() -> None:
    src_img: dict[str, ImageProvider] = {"gemini-imagen": _FakeImageProvider()}
    router = LlmRouter(chat_providers={"gemini": _gemini_mock()}, image_providers=src_img)
    src_img.pop("gemini-imagen")
    assert router.has_image_provider("gemini-imagen") is True


# ══════════════════════════════════════════════════════════════
# 2. resolve()
# ══════════════════════════════════════════════════════════════


def test_resolve_known_expert_returns_primary_provider_and_model() -> None:
    router = LlmRouter(chat_providers={"gemini": _gemini_mock()})
    res = router.resolve("computer")
    assert isinstance(res, ChatResolution)
    assert res.provider.name == "gemini"
    assert res.model == "gemini-2.5-flash"
    assert res.config.expert_id == "computer"


def test_resolve_none_falls_back_to_general() -> None:
    router = LlmRouter(chat_providers={"gemini": _gemini_mock()})
    res = router.resolve(None)
    assert res.config.expert_id == "general"


def test_resolve_unknown_expert_falls_back_to_general() -> None:
    router = LlmRouter(chat_providers={"gemini": _gemini_mock()})
    res = router.resolve("unknown_xyz_123")
    assert res.config.expert_id == "general"


def test_resolve_studio_expert_raises_router_error() -> None:
    """Studio est image-only : son `primary_provider="gemini-imagen"`
    n'est PAS dans `chat_providers`. La chaîne entière est non-viable."""
    router = LlmRouter(chat_providers={"gemini": _gemini_mock()})
    with pytest.raises(RouterError):
        router.resolve("studio")


def test_resolve_with_only_unviable_chain_raises_router_error() -> None:
    """Un router qui n'a aucun des providers de la chaîne d'un expert
    doit lever RouterError (configuration serveur cassée)."""
    # Pour `science`, full_chain = (gemini, gemini, openrouter). Si on a
    # uniquement openai enregistré, aucun candidat n'est viable.
    router = LlmRouter(chat_providers={"openai": _openai_mock()})
    with pytest.raises(RouterError):
        router.resolve("science")


# ══════════════════════════════════════════════════════════════
# 3. build_chain()
# ══════════════════════════════════════════════════════════════


def test_build_chain_returns_primary_then_fallbacks() -> None:
    router = LlmRouter(
        chat_providers={
            "gemini": _gemini_mock(),
            "openrouter": _openrouter_mock(),
        }
    )
    # general : primary=(gemini, flash) + fallbacks=(gemini pro, openrouter sonnet)
    chain = router.build_chain("general")
    assert len(chain) == 3
    assert chain[0].model == "gemini-2.5-flash"
    assert chain[1].model == "gemini-2.5-pro"
    assert chain[2].provider.name == "openrouter"


def test_build_chain_skips_unregistered_provider_silently() -> None:
    """OpenRouter dans la chaîne general mais non enregistré → la chaîne
    saute l'entrée mais les autres restent viables."""
    router = LlmRouter(chat_providers={"gemini": _gemini_mock()})
    chain = router.build_chain("general")
    # Seules les 2 entrées Gemini doivent rester (flash + pro)
    assert len(chain) == 2
    assert all(res.provider.name == "gemini" for res in chain)


def test_build_chain_keeps_model_not_in_supported_set_with_warning() -> None:
    """Un modèle absent de `supported_models` est conservé dans la chaîne
    (le provider réel pourrait l'accepter), juste un warning est loggé."""
    # Mock gemini avec uniquement "flash" supporté (pas "pro")
    limited_gemini = MockChatProvider(
        name="gemini",
        default_model="gemini-2.5-flash",
        supported_models=frozenset({"gemini-2.5-flash"}),  # pro absent
        max_context_tokens=1_048_576,
    )
    router = LlmRouter(chat_providers={"gemini": limited_gemini})
    chain = router.build_chain("computer")  # primary flash + fallback pro
    # Les 2 entrées sont conservées (warning sur pro mais pas filtré)
    assert len(chain) == 2


def test_build_chain_for_unknown_expert_uses_general() -> None:
    router = LlmRouter(chat_providers={"gemini": _gemini_mock()})
    chain = router.build_chain("nonexistent")
    assert len(chain) >= 1
    # Tous les ChatResolution portent la config de 'general'
    assert all(res.config.expert_id == "general" for res in chain)


# ══════════════════════════════════════════════════════════════
# 4. resolve_image()
# ══════════════════════════════════════════════════════════════


def test_resolve_image_studio_happy_path() -> None:
    img_provider = _FakeImageProvider()
    router = LlmRouter(
        chat_providers={"gemini": _gemini_mock()},
        image_providers={"gemini-imagen": img_provider},
    )
    res = router.resolve_image("studio")
    assert isinstance(res, ImageResolution)
    assert res.provider is img_provider
    assert res.model == "imagen-3.0-generate-002"
    assert res.config.expert_id == "studio"


def test_resolve_image_without_provider_raises() -> None:
    """Un router qui n'a pas le provider image enregistré doit lever
    `RouterError` (jamais retourner None)."""
    router = LlmRouter(chat_providers={"gemini": _gemini_mock()})
    with pytest.raises(RouterError):
        router.resolve_image("studio")


def test_resolve_image_for_chat_expert_falls_through_general_image() -> None:
    """Un expert NON image (computer) qui a `primary_provider='gemini'`
    n'aura pas d'image provider — `resolve_image` lève RouterError."""
    router = LlmRouter(
        chat_providers={"gemini": _gemini_mock()},
        image_providers={"gemini-imagen": _FakeImageProvider()},
    )
    with pytest.raises(RouterError):
        router.resolve_image("computer")


# ══════════════════════════════════════════════════════════════
# 5. Introspection
# ══════════════════════════════════════════════════════════════


def test_has_chat_provider_returns_true_for_registered() -> None:
    router = LlmRouter(chat_providers={"gemini": _gemini_mock()})
    assert router.has_chat_provider("gemini") is True
    assert router.has_chat_provider("nonexistent") is False


def test_has_image_provider_returns_false_when_empty() -> None:
    router = LlmRouter(chat_providers={"gemini": _gemini_mock()})
    assert router.has_image_provider("gemini-imagen") is False


def test_chat_provider_names_returns_sorted_list() -> None:
    router = LlmRouter(
        chat_providers={
            "openai": _openai_mock(),
            "gemini": _gemini_mock(),
            "openrouter": _openrouter_mock(),
        }
    )
    assert router.chat_provider_names() == ["gemini", "openai", "openrouter"]


def test_image_provider_names_returns_sorted_list() -> None:
    router = LlmRouter(
        chat_providers={"gemini": _gemini_mock()},
        image_providers={"gemini-imagen": _FakeImageProvider()},
    )
    assert router.image_provider_names() == ["gemini-imagen"]


# ══════════════════════════════════════════════════════════════
# 6. build_default_router() — factory mock-first
# ══════════════════════════════════════════════════════════════


def test_build_default_router_uses_mocks_when_keys_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Toutes les clés API vides → tous les providers sont des MockChatProvider
    usurpant l'identité (name="gemini", "openai", etc.)."""
    from app.ai import router as router_module

    # Force les clés à vide
    monkeypatch.setattr(router_module.settings, "gemini_api_key", "")
    monkeypatch.setattr(router_module.settings, "openai_api_key", "")
    monkeypatch.setattr(router_module.settings, "anthropic_api_key", "")
    monkeypatch.setattr(router_module.settings, "qwen_api_key", "")
    monkeypatch.setattr(router_module.settings, "openrouter_api_key", "")

    router = build_default_router()

    # Les 5 providers chat doivent être enregistrés
    for name in ("gemini", "openai", "anthropic", "qwen", "openrouter"):
        assert router.has_chat_provider(name)

    # Tous doivent être des Mock (clés vides)
    for name in ("gemini", "openai", "anthropic", "qwen", "openrouter"):
        # Accède au dict interne via l'attribut privé
        provider = router._chat[name]  # noqa: SLF001
        assert isinstance(provider, MockChatProvider)
        assert provider.name == name  # identité usurpée

    # Image provider absent quand gemini_api_key vide
    assert router.image_provider_names() == []


def test_build_default_router_image_provider_present_when_gemini_key_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.ai import router as router_module

    monkeypatch.setattr(router_module.settings, "gemini_api_key", "fake-key")
    monkeypatch.setattr(router_module.settings, "openai_api_key", "")
    monkeypatch.setattr(router_module.settings, "anthropic_api_key", "")
    monkeypatch.setattr(router_module.settings, "qwen_api_key", "")
    monkeypatch.setattr(router_module.settings, "openrouter_api_key", "")

    router = build_default_router()
    assert router.has_image_provider("gemini-imagen")
    assert isinstance(router._image["gemini-imagen"], GeminiImageProvider)  # noqa: SLF001


# ══════════════════════════════════════════════════════════════
# 7. ChatResolution / ImageResolution sont frozen dataclasses
# ══════════════════════════════════════════════════════════════


def test_chat_resolution_is_frozen() -> None:
    cfg: ExpertConfig = EXPERT_REGISTRY["general"]
    res = ChatResolution(provider=_gemini_mock(), model="x", config=cfg)
    with pytest.raises((AttributeError, Exception)):
        res.model = "hacked"  # type: ignore[misc]


def test_image_resolution_is_frozen() -> None:
    cfg: ExpertConfig = EXPERT_REGISTRY["studio"]
    res = ImageResolution(provider=_FakeImageProvider(), model="x", config=cfg)
    with pytest.raises((AttributeError, Exception)):
        res.model = "hacked"  # type: ignore[misc]
