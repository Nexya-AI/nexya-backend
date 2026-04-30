"""
Implémentation hCaptcha de `CaptchaVerifier`.

POST application/x-www-form-urlencoded vers https://hcaptcha.com/siteverify
avec les champs `secret` (clé privée) + `response` (token soumis) +
optionnel `remoteip`. Doc : https://docs.hcaptcha.com/#verify-the-user-response

Choix techniques :
- **httpx.AsyncClient réutilisé** (connection pool) — une seule instance
  par process via la factory, pas un nouveau client par requête.
- **Timeout 5 s** — on ne peut pas attendre 30 s pour inscrire un user ;
  si hCaptcha lag, on lève `CaptchaVerifyException` et le service métier
  décide (voir `fail_open` dans AuthService.register).
- **Aucun log du token** — un token hCaptcha est à usage unique (120 s
  côté hCaptcha) mais c'est quand même une valeur sensible temporairement.
- **Pas de retry** — hCaptcha rejette les tokens soumis 2 fois, un retry
  sur timeout ferait échouer le 2ᵉ appel systématiquement. Si ça échoue,
  ça échoue.
"""

from __future__ import annotations

import httpx
import structlog

from app.core.security.captcha.base import (
    CaptchaResult,
    CaptchaVerifier,
    CaptchaVerifyException,
)

log = structlog.get_logger()

_HCAPTCHA_VERIFY_URL = "https://hcaptcha.com/siteverify"
_DEFAULT_TIMEOUT_SECONDS = 5.0


class HCaptchaVerifier(CaptchaVerifier):
    """Client HTTP async vers l'API siteverify de hCaptcha."""

    def __init__(
        self,
        *,
        secret_key: str,
        site_key: str | None = None,
        timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS,
    ) -> None:
        if not secret_key:
            raise ValueError("HCaptchaVerifier requiert une secret_key non vide")
        self._secret_key = secret_key
        # `site_key` est optionnel côté verify (hCaptcha ne l'exige pas),
        # mais on le stocke pour les logs et les checks croisés.
        self._site_key = site_key
        self._client = httpx.AsyncClient(
            timeout=timeout_seconds,
            headers={
                "accept": "application/json",
                "content-type": "application/x-www-form-urlencoded",
            },
        )

    async def verify(
        self,
        token: str,
        *,
        remote_ip: str | None = None,
    ) -> CaptchaResult:
        # Un token vide ne peut jamais être valide — économise un appel réseau
        # et évite de révéler à hCaptcha qu'on appelle avec un token vide
        # (certains providers loguent ça comme un signal anormal).
        if not token:
            return CaptchaResult(success=False, error_codes=("missing-input-response",))

        data: dict[str, str] = {"secret": self._secret_key, "response": token}
        if remote_ip:
            data["remoteip"] = remote_ip
        if self._site_key:
            data["sitekey"] = self._site_key

        try:
            response = await self._client.post(_HCAPTCHA_VERIFY_URL, data=data)
        except httpx.HTTPError as exc:
            log.error("captcha.hcaptcha.transport_error", error=str(exc))
            raise CaptchaVerifyException("hCaptcha transport error") from exc

        if response.status_code >= 400:
            log.error(
                "captcha.hcaptcha.api_error",
                status=response.status_code,
                body=response.text[:500],
            )
            raise CaptchaVerifyException(f"hCaptcha API returned {response.status_code}")

        try:
            payload = response.json()
        except ValueError as exc:
            log.error("captcha.hcaptcha.malformed_response", body=response.text[:500])
            raise CaptchaVerifyException("hCaptcha malformed response") from exc

        success = bool(payload.get("success", False))
        error_codes = tuple(payload.get("error-codes", []) or [])
        score_raw = payload.get("score")
        score = float(score_raw) if isinstance(score_raw, (int, float)) else None

        if success:
            log.info(
                "captcha.hcaptcha.verified",
                score=score,
                hostname=payload.get("hostname"),
            )
        else:
            # On loggue les error_codes (ex. "timeout-or-duplicate", "invalid-input-response")
            # pour détecter une mauvaise intégration côté front. Pas de token loggué.
            log.warning(
                "captcha.hcaptcha.rejected",
                error_codes=error_codes,
            )

        return CaptchaResult(success=success, score=score, error_codes=error_codes)

    async def close(self) -> None:
        await self._client.aclose()
