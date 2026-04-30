"""
Tests — Session A3 : Captcha + anti-abus + sanitizer + quotas device + audit.

Couverture :

  Sanitizer (core/security/sanitizer.py)
  ──────────────────────────────────────
   1. clean_text supprime les null bytes
   2. clean_text normalise en NFC (équivalence `é` précomposé / composé)
   3. clean_text retire les caractères invisibles (zero-width, RLO)
   4. clean_text respecte max_length + collapse_whitespace
   5. clean_email strip + lowercase
   6. is_safe_identifier rejette control / ponctuation / longueur

  Captcha (core/security/captcha/*)
  ─────────────────────────────────
   7. MockCaptchaVerifier accepte "mock-success"
   8. MockCaptchaVerifier rejette "mock-fail"
   9. MockCaptchaVerifier default_success=False rejette l'inconnu
  10. MockCaptchaVerifier enregistre les `calls`
  11. Factory renvoie Mock si hcaptcha_secret_key vide
  12. Factory renvoie Mock si hcaptcha_enabled=False même avec clé

  Device quota (features/auth/device_quotas.py)
  ────────────────────────────────────────────
  13. normalize_device_id renvoie sentinelle si None / vide / malformé / trop long
  14. normalize_device_id préserve un UUID valide
  15. check_and_consume_device_quota commit + retourne count quand quota OK
  16. check_and_consume_device_quota lève DeviceQuotaExceededException au dépassement

  Audit log (features/auth/auth_events.py)
  ───────────────────────────────────────
  17. log_auth_event insère + commit avec payload complet
  18. log_auth_event tronque le user_agent à 256 chars avant insert
  19. log_auth_event fail-safe : SQLAlchemyError swallowed (warning log only)

  Pipeline register A3 (features/auth/service.py + router.py)
  ──────────────────────────────────────────────────────────
  20. Captcha refusé → CaptchaInvalidException + audit `captcha_failed`
  21. Captcha transport error → fail-open (register continue)
  22. Device quota épuisé → DeviceQuotaExceededException + audit `device_quota_exceeded`
  23. Router /auth/register propage client_ip + user_agent + X-Device-Id au service

Discipline : aucun Redis réel, aucune DB. Monkeypatch + AsyncMock + MagicMock.
"""

from __future__ import annotations

import uuid
from datetime import date
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.core.errors.exceptions import (
    CaptchaInvalidException,
    DeviceQuotaExceededException,
)
from app.core.security.captcha import (
    CaptchaVerifier,
    CaptchaVerifyException,
    MockCaptchaVerifier,
)
from app.core.security.captcha.factory import (
    get_captcha_verifier,
    reset_captcha_verifier_for_tests,
)
from app.core.security.captcha.mock import TOKEN_FAIL, TOKEN_SUCCESS
from app.core.security.sanitizer import (
    clean_email,
    clean_text,
    is_safe_identifier,
)
from app.features.auth import auth_events
from app.features.auth.device_quotas import (
    UNKNOWN_DEVICE_SENTINEL,
    check_and_consume_device_quota,
    normalize_device_id,
)

# ══════════════════════════════════════════════════════════════
# 1. SANITIZER
# ══════════════════════════════════════════════════════════════


def test_clean_text_strips_null_bytes() -> None:
    assert clean_text("He\x00llo") == "Hello"
    assert clean_text("\x00\x00") == ""


def test_clean_text_normalizes_nfc() -> None:
    """`é` composé (e + U+0301) doit devenir `é` précomposé (U+00E9)."""
    decomposed = "E\u0301le\u0301a"  # Éléa décomposé (5 codepoints)
    composed = "\u00c9l\u00e9a"  # Éléa précomposé (4 codepoints)
    assert clean_text(decomposed) == composed
    assert len(clean_text(decomposed)) == 4


def test_clean_text_removes_invisible_chars() -> None:
    """Zero-width space, RLO override, BOM doivent disparaître."""
    dirty = "Hello\u200b\u202eWorld\ufeff"
    assert clean_text(dirty) == "HelloWorld"


def test_clean_text_max_length_and_collapse_whitespace() -> None:
    assert clean_text("John    Doe", collapse_whitespace=True) == "John Doe"
    assert clean_text("AAAAAAAAAA", max_length=5) == "AAAAA"
    # Collapse OFF : les retours à la ligne sont conservés
    assert clean_text("a\n\nb", collapse_whitespace=False) == "a\n\nb"
    # strip() aux bords, même sans collapse
    assert clean_text("  x  ") == "x"
    # None reste None
    assert clean_text(None) is None


def test_clean_email_lowercase_and_strip() -> None:
    assert clean_email("  Ivan.Ngassa@NEXYA.AI  ") == "ivan.ngassa@nexya.ai"
    assert clean_email("user\x00@example.com") == "user@example.com"


def test_is_safe_identifier_accepts_uuid_and_rejects_specials() -> None:
    assert is_safe_identifier("a1b2c3d4-e5f6-7890-abcd-1234567890ef")
    assert is_safe_identifier("device_ABC-123")
    assert not is_safe_identifier("")  # vide
    assert not is_safe_identifier("contains space")
    assert not is_safe_identifier("has\nnewline")
    assert not is_safe_identifier("semi;colon")
    assert not is_safe_identifier("x" * 129)  # dépasse max_length défaut


# ══════════════════════════════════════════════════════════════
# 2. CAPTCHA
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_captcha_accepts_success_token() -> None:
    verifier = MockCaptchaVerifier()
    result = await verifier.verify(TOKEN_SUCCESS, remote_ip="10.0.0.1")
    assert result.success is True
    assert result.score == 0.9
    assert result.error_codes == ()


@pytest.mark.asyncio
async def test_mock_captcha_rejects_fail_token() -> None:
    verifier = MockCaptchaVerifier()
    result = await verifier.verify(TOKEN_FAIL)
    assert result.success is False
    assert "mock-rejected" in result.error_codes


@pytest.mark.asyncio
async def test_mock_captcha_default_success_false_rejects_unknown() -> None:
    """Tests qui veulent simuler la prod (captcha strict) instancient
    MockCaptchaVerifier(default_success=False)."""
    verifier = MockCaptchaVerifier(default_success=False)
    result = await verifier.verify("random-token")
    assert result.success is False
    assert result.error_codes == ("mock-rejected-default",)


@pytest.mark.asyncio
async def test_mock_captcha_records_calls() -> None:
    verifier = MockCaptchaVerifier()
    await verifier.verify("tok1", remote_ip="1.2.3.4")
    await verifier.verify("tok2")
    assert verifier.calls == [("tok1", "1.2.3.4"), ("tok2", None)]


def test_factory_returns_mock_when_secret_key_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.security.captcha import factory as captcha_factory

    reset_captcha_verifier_for_tests()
    monkeypatch.setattr(captcha_factory.settings, "hcaptcha_secret_key", "", raising=False)
    monkeypatch.setattr(captcha_factory.settings, "hcaptcha_enabled", True, raising=False)

    verifier = get_captcha_verifier()
    assert isinstance(verifier, MockCaptchaVerifier)

    reset_captcha_verifier_for_tests()


def test_factory_returns_mock_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.security.captcha import factory as captcha_factory

    reset_captcha_verifier_for_tests()
    monkeypatch.setattr(captcha_factory.settings, "hcaptcha_secret_key", "fake-key", raising=False)
    monkeypatch.setattr(captcha_factory.settings, "hcaptcha_enabled", False, raising=False)

    verifier = get_captcha_verifier()
    assert isinstance(verifier, MockCaptchaVerifier)

    reset_captcha_verifier_for_tests()


# ══════════════════════════════════════════════════════════════
# 3. DEVICE QUOTA
# ══════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "   ",
        "contains space",
        "has\nnewline",
        "a" * 200,  # trop long
    ],
)
def test_normalize_device_id_returns_sentinel_on_invalid(raw: str | None) -> None:
    assert normalize_device_id(raw) == UNKNOWN_DEVICE_SENTINEL


def test_normalize_device_id_preserves_safe_uuid() -> None:
    uuid_str = "a1b2c3d4-e5f6-7890-abcd-1234567890ef"
    assert normalize_device_id(uuid_str) == uuid_str
    # Case-sensitive : on ne touche pas à la casse
    assert normalize_device_id("ABC_123") == "ABC_123"


@pytest.mark.asyncio
async def test_check_and_consume_device_quota_ok_returns_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UPSERT retourne count=1 → sous la limite (3) → pas d'exception."""
    fake_db = MagicMock()
    result_mock = MagicMock()
    result_mock.scalar_one = MagicMock(return_value=1)
    fake_db.execute = AsyncMock(return_value=result_mock)
    fake_db.commit = AsyncMock()

    # Limite = 3 par défaut (settings.device_registration_daily_limit)
    count = await check_and_consume_device_quota("device-abc", fake_db, ip="1.2.3.4", daily_limit=3)

    assert count == 1
    # Le service DOIT commit — sinon un rollback ultérieur effacerait
    # l'incrément et laisserait l'attaquant retenter à l'infini.
    assert fake_db.commit.await_count == 1
    # Paramètres passés à l'UPSERT
    call = fake_db.execute.await_args
    params = call.args[1]
    assert params["device_id"] == "device-abc"
    assert params["ip"] == "1.2.3.4"
    assert isinstance(params["day"], date)


@pytest.mark.asyncio
async def test_check_and_consume_device_quota_exceeded_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UPSERT retourne count=4, limite=3 → DeviceQuotaExceededException."""
    fake_db = MagicMock()
    result_mock = MagicMock()
    result_mock.scalar_one = MagicMock(return_value=4)
    fake_db.execute = AsyncMock(return_value=result_mock)
    fake_db.commit = AsyncMock()

    with pytest.raises(DeviceQuotaExceededException):
        await check_and_consume_device_quota("spammer-device", fake_db, ip="9.9.9.9", daily_limit=3)

    # Le commit DOIT avoir eu lieu AVANT l'exception — on veut que le
    # compteur reste incrémenté même en cas de dépassement.
    assert fake_db.commit.await_count == 1


# ══════════════════════════════════════════════════════════════
# 4. AUTH EVENTS
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_log_auth_event_inserts_and_commits() -> None:
    fake_db = MagicMock()
    fake_db.add = MagicMock()
    fake_db.flush = AsyncMock()
    fake_db.commit = AsyncMock()

    user_id = uuid.uuid4()
    await auth_events.log_auth_event(
        fake_db,
        event_type="login_success",
        user_id=user_id,
        ip="8.8.8.8",
        user_agent="Mozilla/5.0",
        device_id="device-42",
        metadata={"email_hash": "abc123"},
    )

    # Un AuthEvent a été ajouté à la session
    fake_db.add.assert_called_once()
    inserted = fake_db.add.call_args.args[0]
    assert inserted.user_id == user_id
    assert inserted.event_type == "login_success"
    assert inserted.ip == "8.8.8.8"
    assert inserted.user_agent == "Mozilla/5.0"
    assert inserted.device_id == "device-42"
    assert inserted.metadata_json == {"email_hash": "abc123"}
    # Flush + commit ont été appelés
    assert fake_db.flush.await_count == 1
    assert fake_db.commit.await_count == 1


@pytest.mark.asyncio
async def test_log_auth_event_truncates_long_user_agent() -> None:
    fake_db = MagicMock()
    fake_db.add = MagicMock()
    fake_db.flush = AsyncMock()
    fake_db.commit = AsyncMock()

    long_ua = "A" * 1000
    await auth_events.log_auth_event(
        fake_db,
        event_type="register_failed",
        ip="1.2.3.4",
        user_agent=long_ua,
    )

    inserted = fake_db.add.call_args.args[0]
    # Le CHECK DB limite à 256 — Python tronque en amont.
    assert len(inserted.user_agent) == 256


@pytest.mark.asyncio
async def test_log_auth_event_fail_safe_on_sqlalchemy_error() -> None:
    """Un échec d'audit ne doit JAMAIS remonter côté caller — sinon
    une auth légitime échouerait à cause d'un incident d'audit."""
    fake_db = MagicMock()
    fake_db.add = MagicMock()
    fake_db.flush = AsyncMock(side_effect=SQLAlchemyError("db down"))
    fake_db.commit = AsyncMock()

    # Aucune exception ne doit remonter
    await auth_events.log_auth_event(
        fake_db,
        event_type="login_failed",
        ip="1.2.3.4",
    )


# ══════════════════════════════════════════════════════════════
# 5. PIPELINE REGISTER A3 — service
# ══════════════════════════════════════════════════════════════


@pytest.fixture
def fake_register_context(monkeypatch: pytest.MonkeyPatch):
    """Fixture — neutralise toutes les dépendances lourdes du register.

    Retourne un dict avec des handles pour inspecter / remplacer au cas par cas :
      - `audit_calls` : liste des appels à log_auth_event (event_type, user_id, metadata)
      - `set_captcha(verifier)` : force le verifier retourné par la factory
      - `set_quota_result(count_or_exc)` : forcé une valeur / une exc du quota
      - `existing_user` : MagicMock User à renvoyer comme doublon (ou None)
    """
    from app.features.auth import service as auth_service_mod

    # Capture des logs d'audit
    audit_calls: list[dict] = []

    async def _fake_log_auth_event(db, **kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(auth_service_mod, "log_auth_event", _fake_log_auth_event)

    # Remplacer le captcha verifier — défaut success
    captcha_ref = {"v": MockCaptchaVerifier(default_success=True)}

    def _fake_get_captcha():
        return captcha_ref["v"]

    monkeypatch.setattr(auth_service_mod, "get_captcha_verifier", _fake_get_captcha)

    # Device quota — défaut OK
    quota_ref = {"value": 1}

    async def _fake_check_quota(device_id, db, *, ip=None, daily_limit=None):
        v = quota_ref["value"]
        if isinstance(v, Exception):
            raise v
        return v

    monkeypatch.setattr(auth_service_mod, "check_and_consume_device_quota", _fake_check_quota)

    # DB — SELECT unicité renvoie None par défaut (pas de doublon)
    existing_ref: dict = {"user": None}
    fake_db = MagicMock()
    exec_result = MagicMock()
    exec_result.scalar_one_or_none = MagicMock(side_effect=lambda: existing_ref["user"])
    fake_db.execute = AsyncMock(return_value=exec_result)
    fake_db.add = MagicMock()
    fake_db.flush = AsyncMock(
        side_effect=lambda: (
            setattr(fake_db._last_user, "id", uuid.uuid4())
            if getattr(fake_db, "_last_user", None)
            else None
        )
    )

    # Capture du User créé pour lui attribuer un id au flush
    original_add = fake_db.add

    def _capture_add(entity):
        fake_db._last_user = entity
        original_add(entity)

    fake_db.add = _capture_add

    # Tokens — neutralisés pour garder le test unitaire
    async def _fake_create_refresh(user_id, db):
        return "fake-refresh-token"

    def _fake_create_access(user_id, plan):
        return "fake-access-token"

    monkeypatch.setattr(auth_service_mod, "create_refresh_token", _fake_create_refresh)
    monkeypatch.setattr(auth_service_mod, "create_access_token", _fake_create_access)

    return {
        "db": fake_db,
        "audit_calls": audit_calls,
        "captcha_ref": captcha_ref,
        "quota_ref": quota_ref,
        "existing_ref": existing_ref,
    }


@pytest.mark.asyncio
async def test_register_captcha_rejected_raises_and_audits(
    fake_register_context,
) -> None:
    """Captcha refusé → 400 + audit `captcha_failed` (pas d'INSERT user)."""
    from app.features.auth import service as auth_service_mod
    from app.features.auth.schemas import RegisterRequest

    ctx = fake_register_context
    ctx["captcha_ref"]["v"] = MockCaptchaVerifier(default_success=False)

    body = RegisterRequest(
        email="bot@nexya.ai",
        password="StrongPass123",
        captcha_token="not-mock-success",
    )

    with pytest.raises(CaptchaInvalidException):
        await auth_service_mod.register(
            body,
            ctx["db"],
            client_ip="1.2.3.4",
            user_agent="evil-bot/1.0",
            device_id_raw="device-bot",
        )

    audit_types = [c["event_type"] for c in ctx["audit_calls"]]
    assert "captcha_failed" in audit_types
    # Pas d'INSERT user
    assert ctx["db"].flush.await_count == 0


@pytest.mark.asyncio
async def test_register_captcha_transport_error_is_fail_open(
    fake_register_context,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hCaptcha down → le register continue (fail-open)."""
    from app.features.auth import service as auth_service_mod
    from app.features.auth.schemas import RegisterRequest

    ctx = fake_register_context

    class _BrokenVerifier(CaptchaVerifier):
        async def verify(self, token, *, remote_ip=None):
            raise CaptchaVerifyException("hcaptcha 500")

    ctx["captcha_ref"]["v"] = _BrokenVerifier()

    body = RegisterRequest(
        email="user@nexya.ai",
        password="StrongPass123",
        captcha_token="anything",
    )

    # Ne doit PAS lever — fail-open
    tokens = await auth_service_mod.register(
        body,
        ctx["db"],
        client_ip="1.2.3.4",
        user_agent="ua",
        device_id_raw="device-ok",
    )

    assert tokens.access_token == "fake-access-token"
    # L'audit final `register_success` doit bien avoir été émis malgré l'incident captcha
    audit_types = [c["event_type"] for c in ctx["audit_calls"]]
    assert "register_success" in audit_types
    assert "captcha_failed" not in audit_types


@pytest.mark.asyncio
async def test_register_device_quota_exceeded_raises_and_audits(
    fake_register_context,
) -> None:
    """Quota device dépassé → 429 + audit `device_quota_exceeded`."""
    from app.features.auth import service as auth_service_mod
    from app.features.auth.schemas import RegisterRequest

    ctx = fake_register_context
    ctx["quota_ref"]["value"] = DeviceQuotaExceededException()

    body = RegisterRequest(
        email="spam@nexya.ai",
        password="StrongPass123",
        captcha_token=TOKEN_SUCCESS,
    )

    with pytest.raises(DeviceQuotaExceededException):
        await auth_service_mod.register(
            body,
            ctx["db"],
            client_ip="1.2.3.4",
            user_agent="ua",
            device_id_raw="spammer-device",
        )

    audit_types = [c["event_type"] for c in ctx["audit_calls"]]
    assert "device_quota_exceeded" in audit_types
    # Pas d'INSERT user
    assert ctx["db"].flush.await_count == 0


@pytest.mark.asyncio
async def test_register_happy_path_emits_register_success_audit(
    fake_register_context,
) -> None:
    """Happy-path : captcha OK, quota OK, email libre → audit register_success."""
    from app.features.auth import service as auth_service_mod
    from app.features.auth.schemas import RegisterRequest

    ctx = fake_register_context

    body = RegisterRequest(
        email="new-user@nexya.ai",
        password="StrongPass123",
        username="new_user",
        captcha_token=TOKEN_SUCCESS,
    )

    tokens = await auth_service_mod.register(
        body,
        ctx["db"],
        client_ip="1.2.3.4",
        user_agent="Mozilla/5.0",
        device_id_raw="device-legit",
    )

    assert tokens.access_token == "fake-access-token"
    assert tokens.refresh_token == "fake-refresh-token"

    audit_types = [c["event_type"] for c in ctx["audit_calls"]]
    assert audit_types == ["register_success"]
    # L'audit embarque le contexte forensic
    final = ctx["audit_calls"][0]
    assert final["ip"] == "1.2.3.4"
    assert final["user_agent"] == "Mozilla/5.0"
    assert final["device_id"] == "device-legit"


# ══════════════════════════════════════════════════════════════
# 6. PIPELINE REGISTER A3 — router (forward contexte forensic)
# ══════════════════════════════════════════════════════════════


def test_router_register_forwards_ip_ua_and_device_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Le router doit extraire X-Forwarded-For, User-Agent, X-Device-Id
    et les passer en kwargs au service.register()."""
    from fastapi.testclient import TestClient

    from app.core.database.postgres import get_db
    from app.features.auth import router as auth_router_mod
    from app.features.auth import service as auth_service_mod
    from app.features.auth.schemas import TokenResponse
    from app.main import app

    # Capture les kwargs passés au service
    captured: dict = {}

    async def _fake_register(body, db, **kwargs):
        captured.update(kwargs)
        return TokenResponse(
            access_token="acc",
            refresh_token="ref",
            expires_in=900,
        )

    monkeypatch.setattr(auth_service_mod, "register", _fake_register)

    # Neutralise les 2 couches de rate limit register
    async def _noop(request) -> None:
        return None

    monkeypatch.setattr(auth_router_mod, "rate_limit_register", _noop)
    monkeypatch.setattr(auth_router_mod, "rate_limit_register_daily_ip", _noop)

    app.dependency_overrides[get_db] = lambda: MagicMock()

    try:
        with TestClient(app) as client:
            response = client.post(
                "/auth/register",
                json={
                    "email": "new@nexya.ai",
                    "password": "StrongPass123",
                    "captcha_token": "mock-success",
                },
                headers={
                    "X-Forwarded-For": "203.0.113.42, 10.0.0.1",
                    "User-Agent": "Mozilla/5.0 (Linux) MyApp/2.0",
                    "X-Device-Id": "device-legit-abc",
                },
            )
        assert response.status_code == 200
        # `X-Forwarded-For` est parsé → première IP seulement
        assert captured["client_ip"] == "203.0.113.42"
        assert captured["user_agent"] == "Mozilla/5.0 (Linux) MyApp/2.0"
        assert captured["device_id_raw"] == "device-legit-abc"
    finally:
        app.dependency_overrides.pop(get_db, None)
