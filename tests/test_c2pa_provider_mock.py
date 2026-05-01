"""
Tests unitaires — `MockManifestProvider` + `RealC2PAProvider` config + factory.

Session E4.5 — Pas d'appel réel à la lib `c2pa-python` (mock-first).
On valide :
- Le contrat ABC `ManifestProvider`.
- Le comportement déterministe du Mock (n'altère pas l'image, accumule
  les calls, retourne fake manifest_id).
- Le rejet des formats non supportés (PDF/MP4/etc.).
- L'option `force_skip` pour tester le fail-safe côté caller.
- La factory `get_manifest_provider()` mock-first auto :
  - Kill-switch global `c2pa_enabled=False` → Mock.
  - Force mock via `c2pa_mock_enabled=True` → Mock.
  - Clés vides → Mock + log warning.
  - `RealC2PAProvider` __init__ raise `C2PAConfigError` si fichiers
    introuvables → fallback Mock + log error.
- `C2PASignResult` dataclass frozen + champs cohérents.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.features.images.c2pa import (
    C2PAConfigError,
    C2PASignRequest,
    C2PASignResult,
    ManifestProvider,
    MockManifestProvider,
    RealC2PAProvider,
    get_manifest_provider,
    reset_manifest_provider_for_tests,
)

# ══════════════════════════════════════════════════════════════
# Fixtures
# ══════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _reset_factory():
    """Reset le singleton factory entre chaque test."""
    reset_manifest_provider_for_tests()
    yield
    reset_manifest_provider_for_tests()


def _make_request() -> C2PASignRequest:
    return C2PASignRequest(
        prompt="un chat dans un panier",
        provider="gemini-imagen",
        model="imagen-3.0-generate-002",
        generation_timestamp=datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC),
        watermark_applied=True,
        watermark_version="v1-oiseau-bleu-2026-04",
    )


# ══════════════════════════════════════════════════════════════
# 1. C2PASignResult dataclass
# ══════════════════════════════════════════════════════════════


def test_c2pa_sign_result_is_frozen() -> None:
    res = C2PASignResult(image_bytes=b"x", applied=True, manifest_id="abc")
    with pytest.raises(Exception):  # FrozenInstanceError
        res.applied = False  # type: ignore[misc]


def test_c2pa_sign_result_default_metadata_is_empty_dict() -> None:
    res = C2PASignResult(image_bytes=b"x", applied=False)
    assert res.metadata == {}
    assert res.manifest_id is None
    assert res.signed_at is None
    assert res.skip_reason is None


# ══════════════════════════════════════════════════════════════
# 2. MockManifestProvider — happy path
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_provider_signs_png_returns_applied_true() -> None:
    provider = MockManifestProvider()
    image_bytes = b"\x89PNG fake png bytes"
    request = _make_request()

    result = await provider.sign_image(image_bytes, "image/png", request)

    assert result.applied is True
    assert result.manifest_id == "mock-c2pa-000001"
    assert result.image_bytes is image_bytes  # mock = bytes inchangés
    assert result.signed_at is not None
    assert result.signed_at.tzinfo is UTC
    assert result.skip_reason is None
    assert result.metadata["algorithm"] == "mock"
    assert result.metadata["creator"] == "NEXYA-Mock"


@pytest.mark.asyncio
async def test_mock_provider_increments_counter_per_call() -> None:
    provider = MockManifestProvider()
    request = _make_request()

    r1 = await provider.sign_image(b"img1", "image/jpeg", request)
    r2 = await provider.sign_image(b"img2", "image/png", request)
    r3 = await provider.sign_image(b"img3", "image/webp", request)

    assert r1.manifest_id == "mock-c2pa-000001"
    assert r2.manifest_id == "mock-c2pa-000002"
    assert r3.manifest_id == "mock-c2pa-000003"


@pytest.mark.asyncio
async def test_mock_provider_accumulates_calls_for_assertions() -> None:
    provider = MockManifestProvider()
    request = _make_request()

    await provider.sign_image(b"img1", "image/png", request)
    await provider.sign_image(b"img2", "image/jpeg", request)

    assert len(provider.calls) == 2
    assert provider.calls[0][0] == "image/png"
    assert provider.calls[0][1].provider == "gemini-imagen"
    assert provider.calls[1][0] == "image/jpeg"


# ══════════════════════════════════════════════════════════════
# 3. MockManifestProvider — skip cases
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_provider_force_skip_returns_applied_false() -> None:
    provider = MockManifestProvider(force_skip=True)
    request = _make_request()

    result = await provider.sign_image(b"img", "image/png", request)

    assert result.applied is False
    assert result.skip_reason == "mock_force_skip"
    assert result.image_bytes == b"img"
    assert result.manifest_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime_type",
    ["application/pdf", "video/mp4", "text/plain", "image/gif", ""],
)
async def test_mock_provider_skips_unsupported_format(mime_type: str) -> None:
    provider = MockManifestProvider()
    request = _make_request()

    result = await provider.sign_image(b"img", mime_type, request)

    assert result.applied is False
    assert result.skip_reason == "unsupported_format"
    assert result.image_bytes == b"img"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime_type",
    ["image/png", "image/jpeg", "image/jpg", "image/webp", "IMAGE/PNG"],
)
async def test_mock_provider_accepts_supported_formats_case_insensitive(
    mime_type: str,
) -> None:
    provider = MockManifestProvider()
    request = _make_request()

    result = await provider.sign_image(b"img", mime_type, request)

    assert result.applied is True


# ══════════════════════════════════════════════════════════════
# 4. RealC2PAProvider — config validation
# ══════════════════════════════════════════════════════════════


def test_real_provider_raises_on_missing_certificate(tmp_path) -> None:
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(b"fake key bytes")

    with pytest.raises(C2PAConfigError, match="certificate introuvable"):
        RealC2PAProvider(
            certificate_path=str(tmp_path / "missing_cert.pem"),
            key_path=str(key_path),
        )


def test_real_provider_raises_on_missing_key(tmp_path) -> None:
    cert_path = tmp_path / "cert.pem"
    cert_path.write_bytes(b"fake cert bytes")

    with pytest.raises(C2PAConfigError, match="private key introuvable"):
        RealC2PAProvider(
            certificate_path=str(cert_path),
            key_path=str(tmp_path / "missing_key.pem"),
        )


def test_real_provider_raises_when_c2pa_lib_not_installed(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`c2pa-python` n'est volontairement PAS installé V1 (mock-first).
    Le RealC2PAProvider doit refuser de s'instancier proprement avec
    un message explicite pointant vers la procédure d'install.
    """
    cert_path = tmp_path / "cert.pem"
    cert_path.write_bytes(b"fake cert bytes")
    key_path = tmp_path / "key.pem"
    key_path.write_bytes(b"fake key bytes")

    # Force ImportError sur `c2pa` même si la lib est installée par accident.
    import builtins

    real_import = builtins.__import__

    def _fake_import(name: str, *args, **kwargs):
        if name == "c2pa":
            raise ImportError("simulated absence of c2pa-python")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    with pytest.raises(C2PAConfigError, match="c2pa-python"):
        RealC2PAProvider(
            certificate_path=str(cert_path),
            key_path=str(key_path),
        )


# ══════════════════════════════════════════════════════════════
# 5. Factory — mock-first auto
# ══════════════════════════════════════════════════════════════


def test_factory_returns_mock_when_killswitch_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "c2pa_enabled", False)
    monkeypatch.setattr(app_settings, "c2pa_signing_certificate_path", "/nope.pem")
    monkeypatch.setattr(app_settings, "c2pa_signing_key_path", "/nope.pem")

    provider = get_manifest_provider()

    assert isinstance(provider, MockManifestProvider)


def test_factory_returns_mock_when_force_mock_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "c2pa_enabled", True)
    monkeypatch.setattr(app_settings, "c2pa_mock_enabled", True)

    provider = get_manifest_provider()

    assert isinstance(provider, MockManifestProvider)


def test_factory_returns_mock_when_keys_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "c2pa_enabled", True)
    monkeypatch.setattr(app_settings, "c2pa_mock_enabled", False)
    monkeypatch.setattr(app_settings, "c2pa_signing_certificate_path", "")
    monkeypatch.setattr(app_settings, "c2pa_signing_key_path", "")

    provider = get_manifest_provider()

    assert isinstance(provider, MockManifestProvider)


def test_factory_singleton_returns_same_instance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "c2pa_enabled", False)

    p1 = get_manifest_provider()
    p2 = get_manifest_provider()

    assert p1 is p2


def test_factory_falls_back_to_mock_when_real_init_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cas du déploiement où on a posé `c2pa_enabled=true` + chemins de
    clés mais les fichiers ont été perdus (rotation ratée, secret mal
    monté, etc.). La factory log error + bascule sur Mock pour ne pas
    crasher le boot — la production safety guard côté config.py
    s'occupera de fail-fast en prod si nécessaire.
    """
    from app.config import settings as app_settings

    monkeypatch.setattr(app_settings, "c2pa_enabled", True)
    monkeypatch.setattr(app_settings, "c2pa_mock_enabled", False)
    monkeypatch.setattr(app_settings, "c2pa_signing_certificate_path", "/nonexistent/cert.pem")
    monkeypatch.setattr(app_settings, "c2pa_signing_key_path", "/nonexistent/key.pem")

    provider = get_manifest_provider()

    assert isinstance(provider, MockManifestProvider)


# ══════════════════════════════════════════════════════════════
# 6. ABC contract
# ══════════════════════════════════════════════════════════════


def test_manifest_provider_is_abstract() -> None:
    with pytest.raises(TypeError):
        ManifestProvider()  # type: ignore[abstract]


def test_mock_provider_inherits_from_abc() -> None:
    provider = MockManifestProvider()
    assert isinstance(provider, ManifestProvider)
    assert provider.name == "c2pa-mock"


def test_real_provider_class_inherits_from_abc() -> None:
    # Vérifie l'héritage sans instancier (qui exigerait des fichiers).
    assert issubclass(RealC2PAProvider, ManifestProvider)
    assert RealC2PAProvider.name == "c2pa-real"
