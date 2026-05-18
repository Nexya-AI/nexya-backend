"""
Tests unitaires — `GeminiEmbeddingsProvider` (Session G1).

Valide :
- Happy path batch natif.
- Split automatique au-delà du cap 100 items par appel SDK.
- Task type `RETRIEVAL_DOCUMENT` vs `RETRIEVAL_QUERY` effectivement transmis.
- Mapping d'erreurs SDK Google → hiérarchie `EmbeddingsError` NEXYA.
- Validations d'entrée (liste vide, texte > 2048 chars, modèle non supporté).
- Authentification refusée si `GEMINI_API_KEY` vide.

Le SDK `google-genai` n'est pas appelé réellement — on monkey-patche
`_embed_sub_batch` pour simuler les réponses.
"""

from __future__ import annotations

import pytest

from app.ai.embeddings import (
    EmbeddingsAuthError,
    EmbeddingsInvalidRequestError,
    EmbeddingsRateLimitError,
    EmbeddingsUnavailableError,
    GeminiEmbeddingsProvider,
)

# ══════════════════════════════════════════════════════════════
# Helpers : stubs de réponse SDK
# ══════════════════════════════════════════════════════════════


class _FakeEmbedding:
    """Reproduit `google.genai` `ContentEmbedding(values=list[float])`."""

    def __init__(self, values: list[float]) -> None:
        self.values = values


class _FakeResponse:
    def __init__(self, vectors: list[list[float]]) -> None:
        self.embeddings = [_FakeEmbedding(v) for v in vectors]


def _make_provider() -> GeminiEmbeddingsProvider:
    return GeminiEmbeddingsProvider(api_key="AIza-fake-test-key")


# ══════════════════════════════════════════════════════════════
# Happy path
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_embed_single_text_returns_768_dim(monkeypatch) -> None:
    provider = _make_provider()
    captured: dict = {}

    async def fake_sub_batch(*, client, model, texts, task_type):
        captured["texts"] = list(texts)
        captured["task_type"] = task_type
        captured["model"] = model
        return _FakeResponse([[0.1] * 768])

    monkeypatch.setattr(provider, "_embed_sub_batch", fake_sub_batch)
    # Court-circuite le client réel — `_embed_sub_batch` n'en a pas besoin.
    provider._client = object()  # type: ignore[assignment]

    response = await provider.embed(["bonjour"])

    assert len(response.vectors) == 1
    assert response.vectors[0].dim == 768
    assert response.vectors[0].model == "gemini-embedding-001"
    assert captured["task_type"] == "RETRIEVAL_DOCUMENT"  # défaut
    assert captured["texts"] == ["bonjour"]


@pytest.mark.asyncio
async def test_task_type_retrieval_query_forwarded(monkeypatch) -> None:
    """`build_expert_corpus_context` passera `RETRIEVAL_QUERY` — vérifié."""
    provider = _make_provider()
    captured: dict = {}

    async def fake_sub_batch(*, client, model, texts, task_type):
        captured["task_type"] = task_type
        return _FakeResponse([[0.0] * 768])

    monkeypatch.setattr(provider, "_embed_sub_batch", fake_sub_batch)
    provider._client = object()  # type: ignore[assignment]

    await provider.embed(["une question user"], task_type="RETRIEVAL_QUERY")
    assert captured["task_type"] == "RETRIEVAL_QUERY"


@pytest.mark.asyncio
async def test_batch_over_100_splits_into_multiple_sdk_calls(monkeypatch) -> None:
    """Le SDK Gemini n'accepte pas > 100 items — on doit splitter."""
    provider = _make_provider()
    calls: list[int] = []

    async def fake_sub_batch(*, client, model, texts, task_type):
        calls.append(len(texts))
        return _FakeResponse([[0.0] * 768 for _ in texts])

    monkeypatch.setattr(provider, "_embed_sub_batch", fake_sub_batch)
    provider._client = object()  # type: ignore[assignment]

    texts = [f"text #{i}" for i in range(250)]
    response = await provider.embed(texts)

    assert len(response.vectors) == 250
    # 100 + 100 + 50 = 3 appels SDK
    assert calls == [100, 100, 50]


# ══════════════════════════════════════════════════════════════
# Validations d'entrée
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_empty_list_raises_invalid_request() -> None:
    provider = _make_provider()
    with pytest.raises(EmbeddingsInvalidRequestError):
        await provider.embed([])


@pytest.mark.asyncio
async def test_text_over_2048_chars_raises_invalid_request() -> None:
    provider = _make_provider()
    with pytest.raises(EmbeddingsInvalidRequestError):
        await provider.embed(["a" * 2049])


@pytest.mark.asyncio
async def test_unsupported_model_override_raises() -> None:
    provider = _make_provider()
    with pytest.raises(EmbeddingsInvalidRequestError):
        await provider.embed(["x"], model="text-embedding-ada-002")


# ══════════════════════════════════════════════════════════════
# Authentification
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_embed_without_api_key_raises_auth_error(monkeypatch) -> None:
    """Factory passe le Mock quand clé vide, mais si on instancie directement → auth error.

    G2 V8 2026-05-18 : on isole explicitement le mode AI Studio en forçant
    `gemini_use_vertex=False` car certains environnements (poste Ivan post-G2)
    ont `GEMINI_USE_VERTEX=true` dans le `.env`, ce qui fait que le provider
    tente d'utiliser Vertex AI + ADC à la place de la clé — l'auth Vertex
    aboutit et le test ne raise plus. Le test mesure le comportement AI Studio,
    on doit donc le forcer.
    """
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "gemini_use_vertex", False)
    provider = GeminiEmbeddingsProvider(api_key="")
    with pytest.raises(EmbeddingsAuthError):
        await provider.embed(["x"])


# ══════════════════════════════════════════════════════════════
# Mapping d'erreurs SDK
# ══════════════════════════════════════════════════════════════


class _FakeGoogleError(Exception):
    """Simule une exception `google.genai` avec `code` HTTP."""

    def __init__(self, msg: str, *, code: int) -> None:
        super().__init__(msg)
        self.code = code


@pytest.mark.asyncio
async def test_sdk_429_mapped_to_rate_limit_error(monkeypatch) -> None:
    provider = _make_provider()

    async def fake_sub_batch(*, client, model, texts, task_type):
        raise _FakeGoogleError("quota", code=429)

    monkeypatch.setattr(provider, "_embed_sub_batch", fake_sub_batch)
    provider._client = object()  # type: ignore[assignment]

    with pytest.raises(EmbeddingsRateLimitError):
        await provider.embed(["x"])


@pytest.mark.asyncio
async def test_sdk_401_mapped_to_auth_error(monkeypatch) -> None:
    provider = _make_provider()

    async def fake_sub_batch(*, client, model, texts, task_type):
        raise _FakeGoogleError("bad key", code=401)

    monkeypatch.setattr(provider, "_embed_sub_batch", fake_sub_batch)
    provider._client = object()  # type: ignore[assignment]

    with pytest.raises(EmbeddingsAuthError):
        await provider.embed(["x"])


@pytest.mark.asyncio
async def test_sdk_400_mapped_to_invalid_request(monkeypatch) -> None:
    provider = _make_provider()

    async def fake_sub_batch(*, client, model, texts, task_type):
        raise _FakeGoogleError("bad arg", code=400)

    monkeypatch.setattr(provider, "_embed_sub_batch", fake_sub_batch)
    provider._client = object()  # type: ignore[assignment]

    with pytest.raises(EmbeddingsInvalidRequestError):
        await provider.embed(["x"])


@pytest.mark.asyncio
async def test_sdk_503_mapped_to_unavailable(monkeypatch) -> None:
    provider = _make_provider()

    async def fake_sub_batch(*, client, model, texts, task_type):
        raise _FakeGoogleError("down", code=503)

    monkeypatch.setattr(provider, "_embed_sub_batch", fake_sub_batch)
    provider._client = object()  # type: ignore[assignment]

    with pytest.raises(EmbeddingsUnavailableError):
        await provider.embed(["x"])


# ══════════════════════════════════════════════════════════════
# Propriétés
# ══════════════════════════════════════════════════════════════


def test_provider_metadata_properties() -> None:
    provider = _make_provider()
    assert provider.name == "gemini"
    assert provider.default_model == "gemini-embedding-001"
    assert provider.dim == 768
    assert "gemini-embedding-001" in provider.supported_models
