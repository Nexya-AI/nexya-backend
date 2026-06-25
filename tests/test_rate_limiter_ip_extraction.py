"""Tests unitaires `_get_client_ip` — extraction IP spoof-safe derriere Caddy (v1.2.4).

Regression-guard du fix 2026-06-25 : derriere EXACTEMENT un proxy Caddy, l'entree
la plus a DROITE de `X-Forwarded-For` est l'IP du pair verifie par Caddy (non
falsifiable) ; l'entree de gauche est fournie par le client et NE DOIT PAS servir
au rate limiting (sinon un attaquant la falsifie a chaque requete pour obtenir une
cle Redis differente et contourner toutes les limites IP). On valide `parts[-1]`.
"""

from __future__ import annotations

from app.core.security.rate_limiter import _get_client_ip


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    """Stand-in minimal : `_get_client_ip` ne touche que `.headers.get` + `.client.host`."""

    def __init__(self, xff: str | None, client_host: str | None = "10.0.0.9") -> None:
        self.headers: dict[str, str] = {} if xff is None else {"x-forwarded-for": xff}
        self.client = _FakeClient(client_host) if client_host is not None else None


def test_rightmost_xff_used_when_client_spoofs_leftmost():
    # Caddy AJOUTE l'IP du pair (10.0.0.5) a droite ; 1.2.3.4 est forge par le client.
    assert _get_client_ip(_FakeRequest("1.2.3.4, 10.0.0.5")) == "10.0.0.5"


def test_single_xff_entry_returned():
    assert _get_client_ip(_FakeRequest("203.0.113.42")) == "203.0.113.42"


def test_multiple_spoofed_entries_take_caddy_appended_rightmost():
    assert _get_client_ip(_FakeRequest("9.9.9.9, 8.8.8.8, 172.18.0.7")) == "172.18.0.7"


def test_whitespace_and_empty_segments_ignored():
    assert _get_client_ip(_FakeRequest("  1.2.3.4 ,  , 10.0.0.5  ")) == "10.0.0.5"


def test_fallback_to_client_host_when_no_xff():
    assert _get_client_ip(_FakeRequest(None, client_host="198.51.100.7")) == "198.51.100.7"


def test_fallback_unknown_when_no_xff_and_no_client():
    assert _get_client_ip(_FakeRequest(None, client_host=None)) == "unknown"
