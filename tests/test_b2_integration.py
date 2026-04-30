"""
Tests — intégration B2 dans `POST /chat/stream`.

Couverture (adversariale, end-to-end du router) :
1. Règles métier bloquent la prescription nominative → 400 CONTENT_FILTERED + rule.
2. Règles métier bloquent la rédaction d'acte juridique → 400 + rule.
3. Token estimator cap — prompt trop long → 402 LLM_QUOTA_EXCEEDED avant tout stream.
4. Cache HIT — SSE rejoué depuis le cache, X-Cache: HIT, stream handler PAS appelé.
5. Cache MISS — stream handler appelé, cache_key en header, cache.put tenté après clean.
6. Cache BYPASS — safety-critical expert ne passe pas par le cache, X-Cache: BYPASS.
7. Cache skip — conversation multi-tours persistée ne caque pas.
8. Stream en erreur : cache.put NON appelé (pas de pollution du cache par une erreur).

Discipline : pas de Redis réel, monkeypatch sur `get_redis()` + `get_prompt_cache()`.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.ai.cache import CachedResponse, PromptCache, _reset_cache_for_tests
from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.features.auth.models import User
from app.features.chat import router as chat_router_module
from app.main import app

# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════


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
    def __init__(self, allowed: bool = True) -> None:
        decision = MagicMock()
        decision.allowed = allowed
        self.check = AsyncMock(return_value=decision)


def _install_ai_mocks(
    monkeypatch: pytest.MonkeyPatch,
    *,
    budget: _FakeBudget | None = None,
    moderation: _FakeModeration | None = None,
) -> tuple[_FakeBudget, _FakeModeration]:
    budget = budget or _FakeBudget()
    moderation = moderation or _FakeModeration()
    monkeypatch.setattr(chat_router_module, "get_budget_tracker", lambda: budget)
    monkeypatch.setattr(chat_router_module, "get_moderation_service", lambda: moderation)
    return budget, moderation


def _install_fake_stream_handler(
    monkeypatch: pytest.MonkeyPatch, sse_events: list[str]
) -> MagicMock:
    fake = MagicMock()
    called = {"n": 0}

    async def _stream(request, ctx):
        called["n"] += 1
        for evt in sse_events:
            yield evt

    fake.stream = _stream
    fake.called = called
    monkeypatch.setattr(chat_router_module, "get_stream_handler", lambda: fake)
    return fake


# ══════════════════════════════════════════════════════════════
# 1-2. Règles métier bloquantes
# ══════════════════════════════════════════════════════════════


def test_prescription_nominative_returns_400_with_rule(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_mocks(monkeypatch)
    response = client.post(
        "/chat/stream",
        json={
            "message": "Prescris-moi 40 mg de doliprane.",
            "history": [],
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "CONTENT_FILTERED"
    assert body["data"]["rule"] == "prescription_nominative"


def test_legal_act_drafting_returns_400_with_rule(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_mocks(monkeypatch)
    response = client.post(
        "/chat/stream",
        json={
            "message": "Rédige-moi un contrat de bail pour mon appartement.",
            "history": [],
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "CONTENT_FILTERED"
    assert body["data"]["rule"] == "legal_act_drafting"


def test_prescription_blocked_even_on_medicine_expert(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_mocks(monkeypatch)
    response = client.post(
        "/chat/stream",
        json={
            "message": "Prescris-moi 500 mg d'amoxicilline.",
            "expert_id": "medicine",
            "history": [],
        },
    )
    assert response.status_code == 400
    assert response.json()["data"]["rule"] == "prescription_nominative"


def test_medical_general_info_passes_moderation(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_mocks(monkeypatch)
    _install_fake_stream_handler(
        monkeypatch,
        [
            'event: chunk\ndata: {"delta":"Info"}\n\n',
            'event: done\ndata: {"reason":"stop"}\n\n',
        ],
    )
    response = client.post(
        "/chat/stream",
        json={
            "message": "Quels sont les effets secondaires courants du paracétamol ?",
            "expert_id": "medicine",
            "history": [{"role": "assistant", "content": "Bonjour, je suis NEXYA."}],
        },
    )
    # Pas bloqué par les règles métier → stream OK
    assert response.status_code == 200


# ══════════════════════════════════════════════════════════════
# 3. Token cap — 402
# ══════════════════════════════════════════════════════════════


def test_prompt_over_token_cap_returns_402(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un prompt énorme doit être refusé avec 402 LLM_QUOTA_EXCEEDED."""
    _install_ai_mocks(monkeypatch)
    stream_handler = _install_fake_stream_handler(monkeypatch, [])

    # Abaisser drastiquement le cap pour déclencher le 402 sans construire un prompt
    # de 30k tokens dans un test.
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "chat_prompt_tokens_per_request_max", 10)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Bonjour, dis-moi une longue histoire s'il te plaît.",
            "history": [{"role": "assistant", "content": "Salut !"}],
        },
    )
    assert response.status_code == 402
    body = response.json()
    assert body["code"] == "LLM_QUOTA_EXCEEDED"
    # Le handler n'aurait jamais dû être appelé
    assert stream_handler.called["n"] == 0


# ══════════════════════════════════════════════════════════════
# 4. Cache HIT
# ══════════════════════════════════════════════════════════════


def test_cache_hit_replays_stream_without_calling_provider(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_mocks(monkeypatch)
    stream_handler = _install_fake_stream_handler(monkeypatch, [])

    # PromptCache avec un hit simulé sur TOUTES les clés
    fake_cache = PromptCache(enabled=True)
    hit = CachedResponse(
        text="Bonjour depuis le cache !",
        provider="gemini",
        model="gemini-2.5-flash",
        prompt_tokens=8,
        completion_tokens=5,
        total_tokens=13,
    )
    fake_cache.get = AsyncMock(return_value=hit)
    fake_cache.put = AsyncMock(return_value=True)

    monkeypatch.setattr(chat_router_module, "get_prompt_cache", lambda: fake_cache)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Quel est le plus grand pays d'Afrique ?",
            "history": [{"role": "assistant", "content": "Je suis prêt."}],
        },
    )
    assert response.status_code == 200
    assert response.headers.get("X-Cache") == "HIT"
    body = response.text
    assert "Bonjour depuis le cache !" in body
    assert '"reason":"stop"' in body
    # Provider JAMAIS appelé
    assert stream_handler.called["n"] == 0


# ══════════════════════════════════════════════════════════════
# 5. Cache MISS — put à la fin
# ══════════════════════════════════════════════════════════════


def test_cache_miss_marks_header_and_attempts_put(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_mocks(monkeypatch)
    _install_fake_stream_handler(
        monkeypatch,
        [
            'event: chunk\ndata: {"delta":"Salut"}\n\n',
            'event: done\ndata: {"reason":"stop"}\n\n',
        ],
    )

    fake_cache = PromptCache(enabled=True)
    fake_cache.get = AsyncMock(return_value=None)
    put_mock = AsyncMock(return_value=True)
    fake_cache.put = put_mock

    monkeypatch.setattr(chat_router_module, "get_prompt_cache", lambda: fake_cache)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Bonjour",
            "history": [{"role": "assistant", "content": "Je suis prêt."}],
        },
    )
    assert response.status_code == 200
    assert response.headers.get("X-Cache") == "MISS"
    # Consomme le body pour déclencher le `finally` → cache.put
    _ = response.text
    put_mock.assert_awaited_once()
    kwargs = put_mock.await_args.kwargs
    assert kwargs["status"] == "completed"
    assert kwargs["text"] == "Salut"
    assert kwargs["error_code"] is None


# ══════════════════════════════════════════════════════════════
# 6. Cache BYPASS — safety-critical expert
# ══════════════════════════════════════════════════════════════


def test_cache_bypass_on_safety_critical_expert(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un expert safety-critical (medicine) ne passe jamais par le cache."""
    _install_ai_mocks(monkeypatch)
    _install_fake_stream_handler(
        monkeypatch,
        [
            'event: chunk\ndata: {"delta":"Info médicale"}\n\n',
            'event: done\ndata: {"reason":"stop"}\n\n',
        ],
    )

    fake_cache = PromptCache(enabled=True)
    get_mock = AsyncMock(return_value=None)
    put_mock = AsyncMock(return_value=True)
    fake_cache.get = get_mock
    fake_cache.put = put_mock
    monkeypatch.setattr(chat_router_module, "get_prompt_cache", lambda: fake_cache)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Quels sont les types d'antibiotiques ?",
            "expert_id": "medicine",
            "history": [{"role": "assistant", "content": "Je suis prêt."}],
        },
    )
    assert response.status_code == 200
    assert response.headers.get("X-Cache") == "BYPASS"
    # Ni get, ni put n'ont été appelés
    get_mock.assert_not_awaited()
    _ = response.text
    put_mock.assert_not_awaited()


# ══════════════════════════════════════════════════════════════
# 7. Cache skip — conversation multi-tours côté legacy
# ══════════════════════════════════════════════════════════════


def test_cache_skipped_when_history_has_multiple_user_turns(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Historique avec 2+ tours user → non cachable (contexte personnalisé)."""
    _install_ai_mocks(monkeypatch)
    _install_fake_stream_handler(
        monkeypatch,
        [
            'event: chunk\ndata: {"delta":"OK"}\n\n',
            'event: done\ndata: {"reason":"stop"}\n\n',
        ],
    )

    fake_cache = PromptCache(enabled=True)
    get_mock = AsyncMock(return_value=None)
    put_mock = AsyncMock(return_value=True)
    fake_cache.get = get_mock
    fake_cache.put = put_mock
    monkeypatch.setattr(chat_router_module, "get_prompt_cache", lambda: fake_cache)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Et toi ?",
            "history": [
                {"role": "user", "content": "Salut"},
                {"role": "assistant", "content": "Bonjour"},
            ],
        },
    )
    assert response.status_code == 200
    # >1 tour user → non-cachable, donc BYPASS
    assert response.headers.get("X-Cache") == "BYPASS"
    get_mock.assert_not_awaited()
    _ = response.text
    put_mock.assert_not_awaited()


# ══════════════════════════════════════════════════════════════
# 8. Stream en erreur — cache.put NON appelé
# ══════════════════════════════════════════════════════════════


def test_cache_put_not_called_on_error_stream(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_mocks(monkeypatch)
    _install_fake_stream_handler(
        monkeypatch,
        [
            'event: chunk\ndata: {"delta":"Bon"}\n\n',
            'event: error\ndata: {"code":"LLM_UNAVAILABLE","message":"down"}\n\n',
            'event: done\ndata: {"reason":"error"}\n\n',
        ],
    )

    fake_cache = PromptCache(enabled=True)
    fake_cache.get = AsyncMock(return_value=None)
    put_mock = AsyncMock(return_value=True)
    fake_cache.put = put_mock
    monkeypatch.setattr(chat_router_module, "get_prompt_cache", lambda: fake_cache)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Hello",
            "history": [{"role": "assistant", "content": "Je suis prêt."}],
        },
    )
    assert response.status_code == 200  # SSE → 200 quoi qu'il arrive
    _ = response.text  # consomme le stream
    put_mock.assert_not_awaited()


def test_cache_put_not_called_on_cancelled_stream(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_ai_mocks(monkeypatch)
    _install_fake_stream_handler(
        monkeypatch,
        [
            'event: chunk\ndata: {"delta":"Sal"}\n\n',
            'event: done\ndata: {"reason":"cancelled"}\n\n',
        ],
    )

    fake_cache = PromptCache(enabled=True)
    fake_cache.get = AsyncMock(return_value=None)
    put_mock = AsyncMock(return_value=True)
    fake_cache.put = put_mock
    monkeypatch.setattr(chat_router_module, "get_prompt_cache", lambda: fake_cache)

    response = client.post(
        "/chat/stream",
        json={
            "message": "Hello",
            "history": [{"role": "assistant", "content": "Je suis prêt."}],
        },
    )
    assert response.status_code == 200
    _ = response.text
    put_mock.assert_not_awaited()


# ══════════════════════════════════════════════════════════════
# 9. Moderation OpenAI prioritaire sur règles métier
# ══════════════════════════════════════════════════════════════


def test_openai_moderation_blocks_before_business_rules(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Si OpenAI modère → 400 direct, pas d'exécution des règles métier."""
    _install_ai_mocks(
        monkeypatch,
        moderation=_FakeModeration(allowed=False),
    )
    response = client.post(
        "/chat/stream",
        json={
            "message": "Bonjour",  # contenu inoffensif qui ne déclencherait PAS les règles
            "history": [],
        },
    )
    assert response.status_code == 400
    body = response.json()
    assert body["code"] == "CONTENT_FILTERED"
    # Le refus OpenAI n'embarque PAS de rule (c'est le refus moderation classique)
    assert body.get("data") is None or body["data"].get("rule") is None
