"""
Tests unitaires — `app/core/storage/virus_scanner.py` (Session E3).

MockVirusScanner détecte la signature EICAR industry-standard.
Factory retourne Mock par défaut (clamav_host vide), NoOp si virus_scan
désactivé, ClamAV stub sinon (qui raise NotImplementedError).
"""

from __future__ import annotations

import pytest

from app.core.storage.virus_scanner import (
    ClamAVScanner,
    MockVirusScanner,
    NoOpVirusScanner,
    get_virus_scanner,
    reset_virus_scanner,
)

# ══════════════════════════════════════════════════════════════
# 1. MockVirusScanner — détection EICAR
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_mock_scanner_detects_eicar() -> None:
    scanner = MockVirusScanner()
    # La signature EICAR est reconstruite à partir des parts pour éviter
    # que ce fichier source lui-même soit scanné comme malveillant.
    eicar = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    result = await scanner.scan(eicar, filename="test.com")
    assert result.status == "suspicious"
    assert result.signature == "EICAR-TEST-SIGNATURE"
    assert result.scanner == "mock"


@pytest.mark.asyncio
async def test_mock_scanner_accepts_clean() -> None:
    scanner = MockVirusScanner()
    result = await scanner.scan(b"Hello world, just a text file", filename="a.txt")
    assert result.status == "clean"
    assert result.signature is None


# ══════════════════════════════════════════════════════════════
# 2. NoOpScanner — toujours clean, même EICAR
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_noop_scanner_always_clean() -> None:
    scanner = NoOpVirusScanner()
    eicar = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
    result = await scanner.scan(eicar)
    # NoOp ne scanne pas → clean pour tout.
    assert result.status == "clean"


# ══════════════════════════════════════════════════════════════
# 3. ClamAV stub — raise NotImplementedError
# ══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_clamav_scanner_stub_raises_not_implemented() -> None:
    scanner = ClamAVScanner(host="localhost", port=3310)
    with pytest.raises(NotImplementedError):
        await scanner.scan(b"data")


# ══════════════════════════════════════════════════════════════
# 4. Factory — résolution selon settings
# ══════════════════════════════════════════════════════════════


def test_factory_returns_mock_when_clamav_host_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "virus_scan_enabled", True, raising=False)
    monkeypatch.setattr(settings, "clamav_host", "", raising=False)
    reset_virus_scanner()
    scanner = get_virus_scanner()
    assert isinstance(scanner, MockVirusScanner)


def test_factory_returns_noop_when_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "virus_scan_enabled", False, raising=False)
    monkeypatch.setattr(settings, "clamav_host", "", raising=False)
    reset_virus_scanner()
    scanner = get_virus_scanner()
    assert isinstance(scanner, NoOpVirusScanner)


def test_factory_returns_clamav_when_host_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "virus_scan_enabled", True, raising=False)
    monkeypatch.setattr(settings, "clamav_host", "av.internal", raising=False)
    monkeypatch.setattr(settings, "clamav_port", 3310, raising=False)
    reset_virus_scanner()
    scanner = get_virus_scanner()
    assert isinstance(scanner, ClamAVScanner)
