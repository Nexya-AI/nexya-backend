"""
Tests d'intégration — router `/chat/conversations`.

Les tests hittent directement le router FastAPI via `TestClient`, avec :
- `get_current_user` surchargé pour injecter un user factice sans JWT
- `get_db` surchargé pour fournir une session inerte (service monkeypatché)
- `ConversationService.*` monkeypatché en `AsyncMock` — on ne démarre pas
  Postgres, on vérifie que le routeur **câble proprement** le service,
  renvoie les bons statuts HTTP et la bonne forme `NexyaResponse`.

Portée couverte par Lot 3 :
- Happy-path sur les 6 endpoints (201/200/204 selon le cas)
- Isolation cross-user : service lève `ResourceNotFoundException` 404, le
  routeur propage sans fuite d'information (jamais 403).
- Curseur forgé : service lève `ValidationException` 422, le routeur renvoie
  un `NexyaResponse` d'erreur avec le code `VALIDATION_ERROR`.

Les tests de charge pagination (bump counters sous concurrence, curseur
ASC/DESC exact sur N pages) requièrent Postgres et arrivent en Phase 5.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from app.core.auth.guards import get_current_user
from app.core.database.postgres import get_db
from app.core.errors.exceptions import (
    ResourceNotFoundException,
    ValidationException,
)
from app.features.auth.models import User
from app.features.chat import service as chat_service_module
from app.features.chat.models import Conversation, Message
from app.features.chat.service import ConversationService
from app.main import app

# ══════════════════════════════════════════════════════════════
# Fixtures communes
# ══════════════════════════════════════════════════════════════

_FAKE_USER_ID = uuid.UUID("c4a2b9a6-0f01-4a0e-9f3f-0d1b8e3c5a77")


def _make_fake_user() -> User:
    """Fabrique un User en mémoire pour alimenter le guard surchargé.

    On ne peuple que les champs qu'un router CRUD consulte : `id`, `is_pro`.
    Les autres colonnes nullables/à défaut server-side restent à `None`
    puisque cet objet ne sera jamais flushé en DB.
    """
    user = MagicMock(spec=User)
    user.id = _FAKE_USER_ID
    user.is_pro = False
    return user


def _make_fake_conversation(
    *,
    conversation_id: uuid.UUID | None = None,
    title: str | None = "Brainstorm roadmap",
    expert_id: str = "general",
    is_archived: bool = False,
    is_favorite: bool = False,
    message_count: int = 0,
    last_message_at: datetime | None = None,
    deleted_at: datetime | None = None,
) -> Conversation:
    """Construit un ORM `Conversation` complet sans toucher la DB.

    Les colonnes à `server_default` ne sont pas peuplées par SQLAlchemy
    tant qu'on ne flush pas — on les renseigne explicitement pour que
    `ConversationResponse.model_validate` n'explose pas sur `None`.

    `deleted_at` (F2.0) : passer une date pour simuler un item de corbeille.
    """
    now = datetime(2026, 4, 21, 14, 30, 0, tzinfo=UTC)
    conv = Conversation(
        user_id=_FAKE_USER_ID,
        title=title,
        expert_id=expert_id,
    )
    conv.id = conversation_id or uuid.UUID("a1b2c3d4-0000-4000-8000-000000000001")
    conv.last_message_at = last_message_at
    conv.message_count = message_count
    conv.is_archived = is_archived
    conv.is_favorite = is_favorite
    conv.title_generated_at = None
    conv.deleted_at = deleted_at
    conv.created_at = now
    conv.updated_at = now
    return conv


def _make_fake_message(
    *,
    conversation_id: uuid.UUID,
    role: str = "user",
    content: str = "Bonjour",
    status_: str = "completed",
) -> Message:
    now = datetime(2026, 4, 21, 14, 35, 0, tzinfo=UTC)
    msg = Message(
        conversation_id=conversation_id,
        role=role,
        content=content,
        status=status_,
    )
    msg.id = uuid.uuid4()
    msg.provider = None
    msg.model = None
    msg.prompt_tokens = None
    msg.completion_tokens = None
    msg.total_tokens = None
    msg.cost_usd = Decimal("0.000000") if False else None
    msg.error_code = None
    msg.finished_at = None
    msg.deleted_at = None
    msg.created_at = now
    msg.updated_at = now
    return msg


@pytest.fixture
def client() -> TestClient:
    """Client HTTP avec guards + session surchargés.

    `get_current_user` renvoie systématiquement le user factice ; `get_db`
    renvoie un `MagicMock` (non consulté puisque le service est
    monkeypatché dans chaque test).
    """
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


# ══════════════════════════════════════════════════════════════
# 1. POST /chat/conversations — happy path
# ══════════════════════════════════════════════════════════════


def test_create_conversation_returns_201_with_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv = _make_fake_conversation(title="Roadmap Q2", expert_id="ingenierie")
    mock_create = AsyncMock(return_value=conv)
    monkeypatch.setattr(ConversationService, "create", mock_create)

    response = client.post(
        "/chat/conversations",
        json={"title": "Roadmap Q2", "expert_id": "ingenierie"},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["success"] is True
    assert body["data"]["id"] == str(conv.id)
    assert body["data"]["title"] == "Roadmap Q2"
    assert body["data"]["expert_id"] == "ingenierie"
    assert body["data"]["message_count"] == 0
    mock_create.assert_awaited_once()


def test_create_conversation_accepts_empty_body(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un `POST` avec `{}` doit fonctionner — titre et expert optionnels."""
    conv = _make_fake_conversation(title=None, expert_id="general")
    monkeypatch.setattr(ConversationService, "create", AsyncMock(return_value=conv))

    response = client.post("/chat/conversations", json={})

    assert response.status_code == 201
    assert response.json()["data"]["title"] is None
    assert response.json()["data"]["expert_id"] == "general"


# ══════════════════════════════════════════════════════════════
# 2. GET /chat/conversations — liste paginée
# ══════════════════════════════════════════════════════════════


def test_list_conversations_returns_page_with_cursor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv1 = _make_fake_conversation(conversation_id=uuid.uuid4(), title="Conv 1")
    conv2 = _make_fake_conversation(conversation_id=uuid.uuid4(), title="Conv 2")
    page = chat_service_module.ConversationsPageOrm(
        items=[conv1, conv2],
        next_cursor="opaque-cursor-abc",
    )
    monkeypatch.setattr(ConversationService, "list_for_user", AsyncMock(return_value=page))

    response = client.get("/chat/conversations?limit=20")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert len(body["data"]["items"]) == 2
    assert body["data"]["next_cursor"] == "opaque-cursor-abc"
    assert body["data"]["items"][0]["title"] == "Conv 1"


def test_list_conversations_rejects_limit_above_50(client: TestClient) -> None:
    """Le plafond `le=50` est une défense contre l'abus — Pydantic rejette
    avant d'atteindre le service (422)."""
    response = client.get("/chat/conversations?limit=500")
    assert response.status_code == 422


def test_list_conversations_forwards_filters_to_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`is_archived=true` et `is_favorite=true` atteignent bien le service."""
    empty = chat_service_module.ConversationsPageOrm(items=[], next_cursor=None)
    mock = AsyncMock(return_value=empty)
    monkeypatch.setattr(ConversationService, "list_for_user", mock)

    response = client.get("/chat/conversations?is_archived=true&is_favorite=true&limit=10")

    assert response.status_code == 200
    kwargs = mock.await_args.kwargs
    assert kwargs["is_archived"] is True
    assert kwargs["is_favorite"] is True
    assert kwargs["limit"] == 10


def test_list_conversations_returns_422_on_malformed_cursor(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Un curseur forgé lève `ValidationException` côté service → 422 propre,
    jamais 500."""
    monkeypatch.setattr(
        ConversationService,
        "list_for_user",
        AsyncMock(side_effect=ValidationException("Curseur de pagination invalide.")),
    )

    response = client.get("/chat/conversations?cursor=not-base64!")

    assert response.status_code == 422
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "VALIDATION_ERROR"


# ══════════════════════════════════════════════════════════════
# 3. GET /chat/conversations/{id} — détail + isolation
# ══════════════════════════════════════════════════════════════


def test_get_conversation_returns_detail(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    conv = _make_fake_conversation(title="À lire")
    monkeypatch.setattr(ConversationService, "get_by_id", AsyncMock(return_value=conv))

    response = client.get(f"/chat/conversations/{conv.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["id"] == str(conv.id)
    assert body["data"]["title"] == "À lire"


def test_get_conversation_returns_404_for_non_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Isolation IDOR : mismatch user_id → 404 (jamais 403), pas de fuite."""
    monkeypatch.setattr(
        ConversationService,
        "get_by_id",
        AsyncMock(side_effect=ResourceNotFoundException("Conversation")),
    )

    other_user_conv = uuid.uuid4()
    response = client.get(f"/chat/conversations/{other_user_conv}")

    assert response.status_code == 404
    body = response.json()
    assert body["success"] is False
    assert body["code"] == "RESOURCE_NOT_FOUND"


def test_get_conversation_rejects_malformed_uuid(client: TestClient) -> None:
    """Pydantic rejette un UUID mal formé avant d'atteindre le service (422)."""
    response = client.get("/chat/conversations/not-a-uuid")
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# 4. PATCH /chat/conversations/{id} — update + isolation
# ══════════════════════════════════════════════════════════════


def test_update_conversation_applies_partial_fields(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    updated = _make_fake_conversation(title="Nouveau titre", is_favorite=True)
    monkeypatch.setattr(ConversationService, "update", AsyncMock(return_value=updated))

    response = client.patch(
        f"/chat/conversations/{updated.id}",
        json={"title": "Nouveau titre", "is_favorite": True},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data"]["title"] == "Nouveau titre"
    assert body["data"]["is_favorite"] is True


def test_update_conversation_rejects_empty_title(client: TestClient) -> None:
    """Le validator `title_not_only_whitespace` rejette en 422."""
    response = client.patch(
        f"/chat/conversations/{uuid.uuid4()}",
        json={"title": "   "},
    )
    assert response.status_code == 422


def test_update_conversation_returns_404_for_non_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ConversationService,
        "update",
        AsyncMock(side_effect=ResourceNotFoundException("Conversation")),
    )

    response = client.patch(
        f"/chat/conversations/{uuid.uuid4()}",
        json={"is_archived": True},
    )

    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


# ══════════════════════════════════════════════════════════════
# 5. DELETE /chat/conversations/{id} — soft-delete + isolation
# ══════════════════════════════════════════════════════════════


def test_delete_conversation_returns_204_on_success(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock_delete = AsyncMock(return_value=None)
    monkeypatch.setattr(ConversationService, "soft_delete", mock_delete)

    conv_id = uuid.uuid4()
    response = client.delete(f"/chat/conversations/{conv_id}")

    assert response.status_code == 204
    assert response.content == b""
    mock_delete.assert_awaited_once()


def test_delete_conversation_returns_404_for_non_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        ConversationService,
        "soft_delete",
        AsyncMock(side_effect=ResourceNotFoundException("Conversation")),
    )

    response = client.delete(f"/chat/conversations/{uuid.uuid4()}")

    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


# ══════════════════════════════════════════════════════════════
# 6. GET /chat/conversations/{id}/messages — pagination + isolation
# ══════════════════════════════════════════════════════════════


def test_list_messages_returns_page(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    conv_id = uuid.uuid4()
    messages = [
        _make_fake_message(conversation_id=conv_id, role="user", content="Hello"),
        _make_fake_message(conversation_id=conv_id, role="assistant", content="Salut !"),
    ]
    page = chat_service_module.MessagesPageOrm(items=messages, next_cursor="next-messages-cursor")
    monkeypatch.setattr(ConversationService, "list_messages", AsyncMock(return_value=page))

    response = client.get(f"/chat/conversations/{conv_id}/messages?limit=20")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert len(body["data"]["items"]) == 2
    assert body["data"]["items"][0]["role"] == "user"
    assert body["data"]["items"][1]["role"] == "assistant"
    assert body["data"]["next_cursor"] == "next-messages-cursor"


def test_list_messages_returns_404_for_non_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Owner check effectué dans le service : non-propriétaire → 404."""
    monkeypatch.setattr(
        ConversationService,
        "list_messages",
        AsyncMock(side_effect=ResourceNotFoundException("Conversation")),
    )

    response = client.get(f"/chat/conversations/{uuid.uuid4()}/messages")

    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


# ══════════════════════════════════════════════════════════════
# 7. F2.0 — Filtre `expert_id` sur la liste active
# ══════════════════════════════════════════════════════════════


def test_list_conversations_forwards_expert_id_filter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`?expert_id=cooking` doit atteindre le service, tel quel."""
    empty = chat_service_module.ConversationsPageOrm(items=[], next_cursor=None)
    mock = AsyncMock(return_value=empty)
    monkeypatch.setattr(ConversationService, "list_for_user", mock)

    response = client.get("/chat/conversations?expert_id=cooking")

    assert response.status_code == 200
    kwargs = mock.await_args.kwargs
    assert kwargs["expert_id"] == "cooking"


def test_list_conversations_without_expert_id_passes_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sans `expert_id` → le service est appelé avec `expert_id=None`
    (pas de filtre → toutes les conversations, tous experts confondus)."""
    empty = chat_service_module.ConversationsPageOrm(items=[], next_cursor=None)
    mock = AsyncMock(return_value=empty)
    monkeypatch.setattr(ConversationService, "list_for_user", mock)

    response = client.get("/chat/conversations")

    assert response.status_code == 200
    assert mock.await_args.kwargs["expert_id"] is None


# ══════════════════════════════════════════════════════════════
# 8. F2.0 — GET /chat/conversations/trash
# ══════════════════════════════════════════════════════════════


def test_list_trash_conversations_happy_path(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """La route corbeille renvoie la page avec `deleted_at` peuplé."""
    deleted_at = datetime(2026, 4, 20, 10, 0, 0, tzinfo=UTC)
    conv = _make_fake_conversation(
        conversation_id=uuid.uuid4(),
        title="Vieille conv",
        deleted_at=deleted_at,
    )
    page = chat_service_module.ConversationsPageOrm(items=[conv], next_cursor="trash-cursor-xyz")
    mock = AsyncMock(return_value=page)
    monkeypatch.setattr(ConversationService, "list_trash_for_user", mock)

    response = client.get("/chat/conversations/trash?limit=20")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert len(body["data"]["items"]) == 1
    item = body["data"]["items"][0]
    assert item["title"] == "Vieille conv"
    assert item["deleted_at"] is not None  # peuplé ici, contrairement à la liste active
    assert body["data"]["next_cursor"] == "trash-cursor-xyz"
    mock.assert_awaited_once()


def test_list_trash_conversations_forwards_expert_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    empty = chat_service_module.ConversationsPageOrm(items=[], next_cursor=None)
    mock = AsyncMock(return_value=empty)
    monkeypatch.setattr(ConversationService, "list_trash_for_user", mock)

    response = client.get("/chat/conversations/trash?expert_id=science")

    assert response.status_code == 200
    assert mock.await_args.kwargs["expert_id"] == "science"


def test_list_trash_route_takes_precedence_over_uuid_route(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Garde anti-régression : si quelqu'un déplace `/trash` après `/{id}`,
    FastAPI tente de parser `"trash"` comme UUID et renvoie 422. Ce test
    verrouille la précédence."""
    empty = chat_service_module.ConversationsPageOrm(items=[], next_cursor=None)
    mock_trash = AsyncMock(return_value=empty)
    monkeypatch.setattr(ConversationService, "list_trash_for_user", mock_trash)
    # Si la route /{id} était matchée à la place, le service détail serait
    # appelé et retournerait 404 (ou 422 Pydantic). On vérifie que NON :
    mock_detail = AsyncMock()
    monkeypatch.setattr(ConversationService, "get_by_id", mock_detail)

    response = client.get("/chat/conversations/trash")

    assert response.status_code == 200
    mock_trash.assert_awaited_once()
    mock_detail.assert_not_awaited()


# ══════════════════════════════════════════════════════════════
# 9. F2.0 — POST /chat/conversations/{id}/restore
# ══════════════════════════════════════════════════════════════


def test_restore_conversation_returns_200_with_payload(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    restored = _make_fake_conversation(
        conversation_id=uuid.uuid4(),
        title="Reviens parmi nous",
        deleted_at=None,  # restauré → deleted_at effacé
    )
    mock = AsyncMock(return_value=restored)
    monkeypatch.setattr(ConversationService, "restore", mock)

    response = client.post(f"/chat/conversations/{restored.id}/restore")

    assert response.status_code == 200
    body = response.json()
    assert body["success"] is True
    assert body["data"]["id"] == str(restored.id)
    assert body["data"]["deleted_at"] is None
    mock.assert_awaited_once()


def test_restore_conversation_returns_404_when_not_in_trash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Service lève `ResourceNotFoundException` si la conv n'est pas dans la
    corbeille du user courant (non-propriétaire OU non supprimée) → 404
    IDOR-safe."""
    monkeypatch.setattr(
        ConversationService,
        "restore",
        AsyncMock(side_effect=ResourceNotFoundException("Conversation")),
    )

    response = client.post(f"/chat/conversations/{uuid.uuid4()}/restore")

    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


def test_restore_conversation_rejects_malformed_uuid(client: TestClient) -> None:
    response = client.post("/chat/conversations/not-a-uuid/restore")
    assert response.status_code == 422


# ══════════════════════════════════════════════════════════════
# 10. F2.0 — DELETE /chat/conversations/{id}/permanent
# ══════════════════════════════════════════════════════════════


def test_permanent_delete_conversation_returns_204(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock = AsyncMock(return_value=None)
    monkeypatch.setattr(ConversationService, "permanent_delete", mock)

    conv_id = uuid.uuid4()
    response = client.delete(f"/chat/conversations/{conv_id}/permanent")

    assert response.status_code == 204
    assert response.content == b""
    mock.assert_awaited_once()


def test_permanent_delete_conversation_returns_404_when_not_in_trash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Purge refusée si la conv est active ou n'appartient pas au user."""
    monkeypatch.setattr(
        ConversationService,
        "permanent_delete",
        AsyncMock(side_effect=ResourceNotFoundException("Conversation")),
    )

    response = client.delete(f"/chat/conversations/{uuid.uuid4()}/permanent")

    assert response.status_code == 404
    assert response.json()["code"] == "RESOURCE_NOT_FOUND"


# ══════════════════════════════════════════════════════════════
# 11. F2.0 — ConversationResponse expose `deleted_at`
# ══════════════════════════════════════════════════════════════


def test_conversation_response_exposes_deleted_at_field(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le champ `deleted_at` est dans le contrat `ConversationResponse` —
    `null` par défaut sur les endpoints actifs, peuplé sur trash/restore.
    Le Flutter lit ce champ pour afficher la date de suppression dans
    l'écran TrashScreen."""
    conv = _make_fake_conversation(title="Conv vivante")
    monkeypatch.setattr(ConversationService, "get_by_id", AsyncMock(return_value=conv))

    response = client.get(f"/chat/conversations/{conv.id}")

    assert response.status_code == 200
    data = response.json()["data"]
    assert "deleted_at" in data
    assert data["deleted_at"] is None


# ══════════════════════════════════════════════════════════════
# 12. C1 — GET /chat/conversations?q=… (FTS titre + messages)
# ══════════════════════════════════════════════════════════════


def test_list_conversations_forwards_q_to_service(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Le query param `q` arrive dans les kwargs du service tel quel."""
    empty = chat_service_module.ConversationsPageOrm(items=[], next_cursor=None)
    mock = AsyncMock(return_value=empty)
    monkeypatch.setattr(ConversationService, "list_for_user", mock)

    response = client.get("/chat/conversations?q=migration%20postgres")

    assert response.status_code == 200
    kwargs = mock.await_args.kwargs
    assert kwargs["q"] == "migration postgres"


def test_list_conversations_q_default_is_none(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sans `q=` dans l'URL, le service reçoit `q=None` (pas de recherche)."""
    empty = chat_service_module.ConversationsPageOrm(items=[], next_cursor=None)
    mock = AsyncMock(return_value=empty)
    monkeypatch.setattr(ConversationService, "list_for_user", mock)

    response = client.get("/chat/conversations")

    assert response.status_code == 200
    assert mock.await_args.kwargs["q"] is None


def test_list_conversations_rejects_empty_q(client: TestClient) -> None:
    """`q=` vide → 422 (Pydantic Query min_length=1). Protège d'un
    ILIKE `%%` qui matcherait tout et saturerait le serveur."""
    response = client.get("/chat/conversations?q=")
    assert response.status_code == 422


def test_list_conversations_rejects_q_longer_than_200_chars(
    client: TestClient,
) -> None:
    """`q` au-delà de 200 chars → 422. Cap défensif : une query FTS raisonnable
    tient largement en 200 chars, au-delà c'est un abus."""
    long_q = "a" * 201
    response = client.get(f"/chat/conversations?q={long_q}")
    assert response.status_code == 422


def test_list_conversations_q_and_expert_id_combine(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`q` et `expert_id` sont additifs — les deux atteignent le service.
    Le Flutter « écran Expert X avec recherche » utilise cette combinaison."""
    empty = chat_service_module.ConversationsPageOrm(items=[], next_cursor=None)
    mock = AsyncMock(return_value=empty)
    monkeypatch.setattr(ConversationService, "list_for_user", mock)

    response = client.get("/chat/conversations?q=docker&expert_id=ingenierie")

    assert response.status_code == 200
    kwargs = mock.await_args.kwargs
    assert kwargs["q"] == "docker"
    assert kwargs["expert_id"] == "ingenierie"
