"""
Factory — retourne le bon `CaptchaVerifier` selon la config.

Règle de sélection :
- `settings.hcaptcha_enabled == False` → `MockCaptchaVerifier`
  (même en prod par toggle, utile pour un incident hCaptcha où on
  préfère ouvrir plutôt que bloquer les inscriptions).
- `settings.hcaptcha_secret_key` vide → `MockCaptchaVerifier` +
  warning unique (dev par défaut, tests, staging).
- Sinon → `HCaptchaVerifier` avec la clé de prod.

Singleton process-wide pour réutiliser le pool HTTP httpx. La factory
expose aussi `reset_captcha_verifier_for_tests()` pour que chaque test
puisse installer sa propre instance mock (avec des règles custom).
"""

from __future__ import annotations

import structlog

from app.config import settings
from app.core.security.captcha.base import CaptchaVerifier
from app.core.security.captcha.hcaptcha import HCaptchaVerifier
from app.core.security.captcha.mock import MockCaptchaVerifier

log = structlog.get_logger()

_captcha_verifier_singleton: CaptchaVerifier | None = None


def get_captcha_verifier() -> CaptchaVerifier:
    global _captcha_verifier_singleton
    if _captcha_verifier_singleton is not None:
        return _captcha_verifier_singleton

    if not settings.hcaptcha_enabled:
        _captcha_verifier_singleton = MockCaptchaVerifier()
        log.warning(
            "captcha.verifier.initialized",
            provider="mock",
            reason="HCAPTCHA_ENABLED=false — captcha désactivé par toggle",
        )
        return _captcha_verifier_singleton

    if settings.hcaptcha_secret_key:
        _captcha_verifier_singleton = HCaptchaVerifier(
            secret_key=settings.hcaptcha_secret_key,
            site_key=settings.hcaptcha_site_key or None,
        )
        log.info("captcha.verifier.initialized", provider="hcaptcha")
    else:
        _captcha_verifier_singleton = MockCaptchaVerifier()
        log.warning(
            "captcha.verifier.initialized",
            provider="mock",
            reason="HCAPTCHA_SECRET_KEY vide — captcha accepte 'mock-success'",
        )
    return _captcha_verifier_singleton


async def close_captcha_verifier() -> None:
    """À appeler dans le lifespan shutdown."""
    global _captcha_verifier_singleton
    if _captcha_verifier_singleton is not None:
        await _captcha_verifier_singleton.close()
        _captcha_verifier_singleton = None


def reset_captcha_verifier_for_tests() -> None:
    """Tests only — force un nouveau singleton au prochain `get_captcha_verifier()`.

    Typiquement utilisé par une fixture pytest qui veut injecter son
    propre `MockCaptchaVerifier(default_success=False)` pour tester
    le chemin « captcha refusé ».
    """
    global _captcha_verifier_singleton
    _captcha_verifier_singleton = None


def set_captcha_verifier_for_tests(verifier: CaptchaVerifier) -> None:
    """Tests only — installe directement une instance custom.

    Évite le hack consistant à patcher `_captcha_verifier_singleton` à la main.
    """
    global _captcha_verifier_singleton
    _captcha_verifier_singleton = verifier
