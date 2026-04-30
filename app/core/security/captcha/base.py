"""
Interface abstraite pour la vérification d'un captcha.

Un seul contrat — plusieurs implémentations : hCaptcha en prod, Mock en
dev/test (accepte les tokens `mock-success`, rejette `mock-fail`).
Le service métier (ex. `/auth/register`) ne connaît jamais le provider
concret, il appelle uniquement `verify(token, remote_ip=...)`.

Pourquoi abstraire plutôt que parler directement à hCaptcha :
- **Tests** : on n'émet pas de requête HTTP à chaque test d'inscription.
- **Dev local** : pas de clé hCaptcha requise pour démarrer la stack.
- **Fail-open contrôlé** : si hCaptcha est down en prod, on a un point
  unique pour décider (loguer + laisser passer, ou bloquer).
- **Pluggable** : demain on peut passer à Turnstile (Cloudflare) ou
  reCAPTCHA v3 sans toucher à `AuthService`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass(slots=True, frozen=True)
class CaptchaResult:
    """Résultat d'une vérification captcha — neutre vis-à-vis du provider.

    Attributes:
        success:      True si le token est valide et émis pour le bon site.
        score:        Score de confiance [0.0, 1.0] si le provider l'expose
                      (hCaptcha Enterprise, reCAPTCHA v3). `None` sinon.
        error_codes:  Codes d'erreur bruts retournés par le provider.
                      Utiles pour le log, pas exposés au client.
    """

    success: bool
    score: float | None = None
    error_codes: tuple[str, ...] = field(default_factory=tuple)


class CaptchaVerifier(ABC):
    """Contrat commun — `verify()` est la seule méthode obligatoire."""

    @abstractmethod
    async def verify(
        self,
        token: str,
        *,
        remote_ip: str | None = None,
    ) -> CaptchaResult:
        """Vérifie un token captcha soumis par le client.

        Args:
            token:      Valeur opaque soumise par le frontend (widget
                        hCaptcha côté Flutter/web).
            remote_ip:  IP du client extraite du header `X-Forwarded-For`
                        ou `request.client.host`. hCaptcha recommande de
                        la passer — on a une deuxième vérif côté provider.

        Returns:
            CaptchaResult.success = True si le token est légitime, False
            sinon. N'émet JAMAIS d'exception sur un token invalide —
            un `success=False` est une donnée normale.

        Raises:
            CaptchaVerifyException: uniquement sur erreur transport
                (hCaptcha injoignable, réponse malformée). Le service
                métier décide alors de fail-open ou fail-close selon
                sa politique.
        """

    async def close(self) -> None:  # pragma: no cover — surchargé par HCaptcha
        """Libère les ressources (client HTTP). No-op par défaut."""
        return None


class CaptchaVerifyException(Exception):
    """Échec transport côté provider captcha — hCaptcha injoignable,
    timeout, réponse malformée. **Jamais** levée pour un token invalide
    (ça c'est `CaptchaResult(success=False)`).
    """
