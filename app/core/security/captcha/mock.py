"""
MockCaptchaVerifier — implémentation dev/test de `CaptchaVerifier`.

Règles :
- Token exactement `mock-success` → `CaptchaResult(success=True, score=0.9)`
- Token exactement `mock-fail`     → `CaptchaResult(success=False,
                                        error_codes=("mock-rejected",))`
- Tout autre token en environnement de dev → succès par défaut (sinon
  impossible de tester l'inscription via curl sans poster un vrai token).

**Désactivable pour un test donné** : dans les tests, on peut instancier
`MockCaptchaVerifier(default_success=False)` pour forcer tous les tokens
non reconnus à échouer — utile pour un test « l'endpoint rejette bien
un captcha invalide ».

Toutes les vérifications sont enregistrées dans `self.calls` pour les
assertions de tests (`assert mock_verifier.calls == [("tok", "1.2.3.4")]`).
"""

from __future__ import annotations

import structlog

from app.core.security.captcha.base import CaptchaResult, CaptchaVerifier

log = structlog.get_logger()

TOKEN_SUCCESS = "mock-success"
TOKEN_FAIL = "mock-fail"


class MockCaptchaVerifier(CaptchaVerifier):
    """Faux CaptchaVerifier — aucun appel réseau."""

    def __init__(self, *, default_success: bool = True) -> None:
        self._default_success = default_success
        self.calls: list[tuple[str, str | None]] = []

    async def verify(
        self,
        token: str,
        *,
        remote_ip: str | None = None,
    ) -> CaptchaResult:
        self.calls.append((token, remote_ip))

        if token == TOKEN_SUCCESS:
            log.debug("captcha.mock.accepted", token_preview="mock-success")
            return CaptchaResult(success=True, score=0.9)
        if token == TOKEN_FAIL:
            log.debug("captcha.mock.rejected", token_preview="mock-fail")
            return CaptchaResult(
                success=False,
                error_codes=("mock-rejected",),
            )

        if self._default_success:
            log.debug("captcha.mock.accepted_default", remote_ip=remote_ip)
            return CaptchaResult(success=True, score=0.9)
        log.debug("captcha.mock.rejected_default", remote_ip=remote_ip)
        return CaptchaResult(
            success=False,
            error_codes=("mock-rejected-default",),
        )
