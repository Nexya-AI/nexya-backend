"""
Captcha — vérification serveur-side des challenges humains à l'inscription.

Module exposé :
- `CaptchaVerifier` : ABC, unique contrat public.
- `CaptchaResult`   : dataclass — success + score optionnel + raison d'échec.
- `get_captcha_verifier()` / `close_captcha_verifier()` : factory singleton.

Le choix de l'implémentation (Hcaptcha réel vs Mock dev/test) est
délégué à la factory selon `settings.hcaptcha_secret_key`.
"""

from __future__ import annotations

from app.core.security.captcha.base import (
    CaptchaResult,
    CaptchaVerifier,
    CaptchaVerifyException,
)
from app.core.security.captcha.factory import (
    close_captcha_verifier,
    get_captcha_verifier,
    reset_captcha_verifier_for_tests,
)
from app.core.security.captcha.hcaptcha import HCaptchaVerifier
from app.core.security.captcha.mock import MockCaptchaVerifier

__all__ = [
    "CaptchaResult",
    "CaptchaVerifier",
    "CaptchaVerifyException",
    "HCaptchaVerifier",
    "MockCaptchaVerifier",
    "close_captcha_verifier",
    "get_captcha_verifier",
    "reset_captcha_verifier_for_tests",
]
