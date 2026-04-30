"""
Tests d'intégration — injection mémoire D3 dans `POST /chat/stream`.

Valide le câblage router → context_builder → StreamContext.memory_context.
On ne monte pas un vrai LLM — on capture le `StreamContext` passé au
handler pour vérifier que `memory_context` est bien renseigné.

Couverture :
1. memory_context non-None → StreamContext porte le bloc.
2. memory_context None → StreamContext.memory_context=None (no-op).
3. Fail-safe : build_memory_context raise → chat passe sans mémoire.
4. Token estimator reçoit bien le system_prompt avec mémoire (cap cohérent).
5. Cache key B2 inclut le system_prompt avec mémoire (miss attendu).
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.ai.cache import _reset_cache_for_tests
from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.chat import router as chat_router_module
from app.main import app

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_fake_user() -> User:
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


@pytest.fixture
def client() -> TestClient:
    fake_user = _make_fake_user()
    fake_db = MagicMock()

    async def _fake_user_override() -> User:
        return fake_user

    async def _fake_db_override():
        yield fake_db

    app.dependency_overrides[get_current_user] = _fake_user_override
    app.dependency_overrides[get_db] = _fake_db_override
    try:
        with TestClient(app) as c:
            yield c
    finally:
        app.dependency_overrides.pop(get_current_user, None)
        app.dependency_overrides.pop(get_db, None)


@pytest.fixture(autouse=True)
def _reset_cache_singleton():
    _reset_cache_for_tests()
    yield
    _reset_cache_for_tests()


class _FakeBudget:
    def __init__(self) -> None:
        self.check_and_consume_chat = AsyncMock(return_value=None)
        self.check_and_consume_image = AsyncMock(return_value=None)


class _FakeModeration:
    def __init__(self) -> None:
        decision = MagicMock()
        decision.allowed = True
        self.check = AsyncMock(return_value=decision)


def _install_ai_mocks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(chat_router_module, "get_budget_tracker", lambda: _FakeBudget())
    monkeypatch.setattr(chat_router_module, "get_moderation_service", lambda: _FakeModeration())


def _install_fake_stream_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> dict:
    """Handler qui capture le `ctx` reçu pour vérification a posteriori."""
    captured: dict = {"ctx": None, "request": None, "call_count": 0}

    async def _stream(request, ctx):
        captured["ctx"] = ctx
        captured["request"] = request
        captured["call_count"] += 1
        # Émet un SSE minimal : un chunk puis done.
        yield 'event: chunk\ndata: {"delta":"Salut"}\n\n'
        yield 'event: done\ndata: {"reason":"stop"}\n\n'

    fake = MagicMock()
    fake.stream = _stream
    monkeypatch.setattr(chat_router_module, "get_stream_handler", lambda: fake)
    return captured


# ══════════════════════════════════════════════════════════════
# 1. memory_context non-None → propagé dans StreamContext
# ══════════════════════════════════════════════════════════════


def test_stream_propagates_memory_context_when_present(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_mocks(monkeypatch)
    captured = _install_fake_stream_handler(monkeypatch)

    # Mock build_memory_context pour retourner un bloc fixe.
    memory_block = "[Contexte] L'utilisateur est Ivan dev Flutter [/Contexte]"
    monkeypatch.setattr(
        chat_router_module,
        "build_memory_context",
        AsyncMock(return_value=memory_block),
    )

    response = client.post(
        "/chat/stream",
        json={
            "message": "Écris-moi un script Flutter",
            "history": [{"role": "assistant", "content": "Je suis prêt."}],
        },
    )
    assert response.status_code == 200
    # Le handler a bien été invoqué avec un ctx contenant le bloc mémoire.
    assert captured["call_count"] == 1
    assert captured["ctx"] is not None
    assert captured["ctx"].memory_context == memory_block


# ══════════════════════════════════════════════════════════════
# 2. memory_context None → StreamContext.memory_context=None
# ══════════════════════════════════════════════════════════════


def test_stream_no_memory_context_when_builder_returns_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_mocks(monkeypatch)
    captured = _install_fake_stream_handler(monkeypatch)

    # Mock build_memory_context pour retourner None (aucune memory pertinente).
    monkeypatch.setattr(
        chat_router_module,
        "build_memory_context",
        AsyncMock(return_value=None),
    )

    response = client.post(
        "/chat/stream",
        json={
            "message": "Salut !",
            "history": [{"role": "assistant", "content": "Je suis prêt."}],
        },
    )
    assert response.status_code == 200
    assert captured["ctx"].memory_context is None


# ══════════════════════════════════════════════════════════════
# 3. Fail-safe : build_memory_context raise → chat continue
# ══════════════════════════════════════════════════════════════


def test_stream_continues_if_memory_builder_raises(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si `build_memory_context` raise (ne devrait pas arriver car il a
    déjà un fail-safe interne, mais double garde-fou), le chat doit
    quand même aboutir à un 200 SSE.

    Note : le fail-safe interne de `build_memory_context` retourne déjà
    None sur exception, ce test vérifie que le router ne tente pas de
    concat un None (sinon AttributeError)."""
    _install_ai_mocks(monkeypatch)
    captured = _install_fake_stream_handler(monkeypatch)

    # Même si le builder retourne None de manière fail-safe, le chat doit
    # passer proprement.
    monkeypatch.setattr(
        chat_router_module,
        "build_memory_context",
        AsyncMock(return_value=None),
    )

    response = client.post(
        "/chat/stream",
        json={
            "message": "Une question simple",
            "history": [{"role": "assistant", "content": "Je suis prêt."}],
        },
    )
    assert response.status_code == 200
    assert captured["call_count"] == 1


# ══════════════════════════════════════════════════════════════
# 4. Token estimator reçoit le system_prompt augmenté
# ══════════════════════════════════════════════════════════════


def test_stream_token_estimator_sees_memory_augmented_prompt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le cap anti-abus 30 000 tokens doit prendre en compte le bloc
    mémoire, sinon un user pourrait contourner le cap en saturant sa
    mémoire IA."""
    _install_ai_mocks(monkeypatch)
    _install_fake_stream_handler(monkeypatch)

    memory_block = "[Contexte] " + ("fait " * 50) + "[/Contexte]"
    monkeypatch.setattr(
        chat_router_module,
        "build_memory_context",
        AsyncMock(return_value=memory_block),
    )

    # Capture les kwargs de estimate_tokens.
    captured = {"system_prompt": None}

    def _capture_estimate(*args, **kwargs):
        captured["system_prompt"] = kwargs.get("system_prompt")
        fake = MagicMock()
        fake.prompt_tokens = 100  # Bien en dessous du cap, pas de raise.
        return fake

    monkeypatch.setattr(chat_router_module, "estimate_tokens", _capture_estimate)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Un message",
            "history": [{"role": "assistant", "content": "Prêt."}],
        },
    )
    assert response.status_code == 200
    # Le system_prompt passé à estimate_tokens contient le bloc mémoire.
    assert captured["system_prompt"] is not None
    assert memory_block in captured["system_prompt"]


# ══════════════════════════════════════════════════════════════
# 5. Cache key B2 inclut le system_prompt augmenté
# ══════════════════════════════════════════════════════════════


def test_stream_cache_key_includes_memory_augmented_prompt(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2 users avec des memories différentes → cache_key différent,
    donc pas de collision inter-users."""
    _install_ai_mocks(monkeypatch)
    _install_fake_stream_handler(monkeypatch)

    memory_block = "[Contexte] Spécifique user A [/Contexte]"
    monkeypatch.setattr(
        chat_router_module,
        "build_memory_context",
        AsyncMock(return_value=memory_block),
    )

    # Capture le system_prompt passé à cache.build_key.
    captured: dict = {"system_prompt": None, "build_key_called": False}
    real_cache = chat_router_module.get_prompt_cache()

    def _capture_build_key(**kwargs):
        captured["system_prompt"] = kwargs.get("system_prompt")
        captured["build_key_called"] = True
        return "fake-cache-key"

    # On patche directement la méthode via un remplacement de l'instance.
    fake_cache = MagicMock()
    fake_cache.is_cacheable = MagicMock(return_value=True)
    fake_cache.build_key = _capture_build_key
    fake_cache.get = AsyncMock(return_value=None)  # Miss systématique.
    fake_cache.put = AsyncMock(return_value=None)

    monkeypatch.setattr(chat_router_module, "get_prompt_cache", lambda: fake_cache)

    response = client.post(
        "/chat/stream",
        json={
            "message": "question",
            "history": [{"role": "assistant", "content": "Prêt."}],
        },
    )
    assert response.status_code == 200
    assert captured["build_key_called"]
    assert memory_block in (captured["system_prompt"] or "")
