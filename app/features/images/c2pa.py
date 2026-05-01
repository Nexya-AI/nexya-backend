"""
NEXYA C2PA — Signature cryptographique des images générées par IA (Session E4.5).

Conformité AI Act UE 2024/1689 — applicable août 2026 — exige que tout
contenu généré par IA porte une indication détectable et techniquement
robuste qu'il a été créé par une IA. C2PA (Content Provenance and
Authenticity) est le standard de l'industrie (Adobe + Microsoft + Google
+ OpenAI + BBC + Sony + Nikon + Canon) pour répondre à cette obligation.

Différence avec E4 :
- **E4 watermark visuel** (oiseau bleu) — branding utilisateur, lisible
  à l'œil nu, mais facilement supprimé par crop.
- **E4.5 C2PA** — manifeste cryptographique invisible **embarqué dans
  les métadonnées du fichier** (PNG iTXt / JPEG APP11 / WEBP XMP),
  signé X.509, vérifiable via Content Credentials Adobe ou tout
  vérificateur C2PA. Couvre la conformité légale AI Act.

Pattern aligné Brevo/hCaptcha/FCM/Vision/Voice/Embeddings/Crisp :
- ABC `ManifestProvider` — contrat minimal `sign_image`.
- `RealC2PAProvider` — wrap `c2pa-python` (lib Adobe, wheel Rust c2pa-rs).
- `MockManifestProvider` — accumule les calls, retourne fake manifest_id
  sans toucher l'image. Permet dev sans clés X.509.
- Factory `get_manifest_provider()` mock-first auto si clés absentes.

**Fail-safe absolu** côté `RealC2PAProvider.sign_image` : sur exception
import / clés invalides / format non supporté / signature échouée,
retourne `C2PASignResult(image_bytes=original, applied=False, ...)`.
**Jamais** bloquer `/image/generate` pour une signature ratée — l'IA a
été payée, l'user reçoit son image (sans signature, traçé en metadata).

Prérequis Ivan pour activer le mode RÉEL en prod :
  Option A — Clés X.509 perso (NEXYA = sa propre Trust Anchor)
    openssl req -x509 -newkey rsa:4096 \\
      -keyout nexya_c2pa_private.pem \\
      -out nexya_c2pa_cert.pem \\
      -days 730 -nodes -subj "/CN=NEXYA AI/O=Nexyalabs"

  Option B — Adobe Content Credentials (CA reconnue mondialement)
    Compte gratuit https://contentcredentials.org → récupérer cert + key.

Puis :
    pip install c2pa-python>=0.6,<1
    C2PA_SIGNING_CERTIFICATE_PATH=/secrets/nexya_c2pa_cert.pem
    C2PA_SIGNING_KEY_PATH=/secrets/nexya_c2pa_private.pem
    C2PA_ENABLED=true
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import structlog

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# CONSTANTES
# ═══════════════════════════════════════════════════════════════════

C2PA_VERSION: Final[str] = "v1-2026-04"

# Formats supportés par c2pa-rs (wheels Rust). PDF/MP4 hors scope V1
# car `/image/generate` ne produit que des images. Étendre quand
# Nexya Studio exportera vidéo/PDF (Phase 7-8).
_SUPPORTED_MIMES: Final[frozenset[str]] = frozenset(
    {"image/png", "image/jpeg", "image/jpg", "image/webp"}
)


# ═══════════════════════════════════════════════════════════════════
# DATACLASSES
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class C2PASignRequest:
    """Métadonnées injectées dans le manifest C2PA.

    Le LLM provider/model + prompt apparaissent dans Content Credentials
    quand un user vérifie l'image sur https://contentcredentials.org/verify.
    Permet la traçabilité « cette image a été générée par NEXYA via X
    avec le prompt Y le ZZZ ».
    """

    prompt: str
    provider: str
    model: str
    generation_timestamp: datetime
    watermark_applied: bool = False
    watermark_version: str | None = None


@dataclass(frozen=True, slots=True)
class C2PASignResult:
    """Résultat de la tentative de signature.

    `applied=True` → l'image a été modifiée (manifest embarqué) et
    `image_bytes` contient la version signée.
    `applied=False` → fail-safe ou skip, `image_bytes` = bytes originaux,
    `skip_reason` documente pourquoi (pour metadata Library + audit).
    """

    image_bytes: bytes
    applied: bool
    manifest_id: str | None = None
    signed_at: datetime | None = None
    skip_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════════════
# ERREURS
# ═══════════════════════════════════════════════════════════════════


class C2PAError(Exception):
    """Erreur générique côté manifest provider."""


class C2PAConfigError(C2PAError):
    """Configuration invalide (clés manquantes, lib c2pa absente, etc.).

    Levée à l'instanciation du `RealC2PAProvider` — empêche le boot prod
    avec config cassée. La factory bascule alors sur `MockManifestProvider`
    + log error (le mode dégradé est traçable).
    """


class C2PASignError(C2PAError):
    """Signature échouée à l'exécution (format non supporté, OOM, etc.)."""


# ═══════════════════════════════════════════════════════════════════
# ABC
# ═══════════════════════════════════════════════════════════════════


class ManifestProvider(ABC):
    """Contrat minimal d'un provider C2PA.

    `sign_image` ne lève JAMAIS d'exception côté `RealC2PAProvider` —
    le caller `/image/generate` reste fail-safe absolu (pattern aligné
    `apply_nexya_watermark` E4 et `LibraryService.create_from_bytes` C3).
    """

    name: str = ""

    @abstractmethod
    async def sign_image(
        self,
        image_bytes: bytes,
        mime_type: str,
        request: C2PASignRequest,
    ) -> C2PASignResult:
        """Signe une image avec un manifest C2PA.

        Retourne `C2PASignResult` qui indique succès/skip via `applied`.
        Fail-safe : aucune exception ne doit remonter au caller.
        """
        raise NotImplementedError


# ═══════════════════════════════════════════════════════════════════
# REAL — c2pa-python (Adobe wrap c2pa-rs)
# ═══════════════════════════════════════════════════════════════════


class RealC2PAProvider(ManifestProvider):
    """Provider C2PA réel via la lib `c2pa-python` (Adobe).

    L'import de `c2pa` est **lazy** — le module reste importable même
    sans la lib installée (mock-first). Le `RealC2PAProvider` n'est
    instancié par la factory que quand les clés sont fournies, et
    son __init__ tente l'import + valide les clés. Sinon
    `C2PAConfigError` → factory bascule sur Mock + log error.

    Algorithme de signature : ES256 (ECDSA P-256) par défaut, recommandé
    Adobe et le plus largement supporté par les vérificateurs C2PA.
    Configurable via `c2pa_signing_algorithm` pour ES384/PS256/Ed25519.
    """

    name: str = "c2pa-real"

    def __init__(
        self,
        *,
        certificate_path: str,
        key_path: str,
        creator_name: str = "NEXYA",
        signing_algorithm: str = "es256",
    ) -> None:
        # 1. Valider les chemins de fichiers (fail-fast, pas au runtime).
        cert = Path(certificate_path)
        key = Path(key_path)
        if not cert.is_file():
            raise C2PAConfigError(
                f"C2PA certificate introuvable: {certificate_path}. "
                "Voir docstring du module pour la procédure de génération."
            )
        if not key.is_file():
            raise C2PAConfigError(
                f"C2PA private key introuvable: {key_path}. "
                "Voir docstring du module pour la procédure de génération."
            )

        # 2. Import lazy de c2pa-python (lib Rust wheel ~10 MB).
        # Si la lib n'est pas installée, on raise — la factory bascule
        # sur Mock avec log error explicite.
        try:
            import c2pa  # type: ignore[import-not-found]  # noqa: F401
        except ImportError as exc:
            raise C2PAConfigError(
                "Lib `c2pa-python` non installée. Installer via "
                "`uv pip install c2pa-python>=0.6,<1` puis redémarrer."
            ) from exc

        self._certificate_path = str(cert)
        self._key_path = str(key)
        self._creator_name = creator_name
        self._signing_algorithm = signing_algorithm.lower()
        # Cache le contenu des clés (lecture une fois au boot, évite I/O
        # disque par requête + permet de monter les secrets en read-only).
        self._certificate_bytes = cert.read_bytes()
        self._key_bytes = key.read_bytes()

        log.info(
            "c2pa.real.initialized",
            certificate_path=self._certificate_path,
            key_path=self._key_path,
            algorithm=self._signing_algorithm,
            creator=self._creator_name,
        )

    async def sign_image(
        self,
        image_bytes: bytes,
        mime_type: str,
        request: C2PASignRequest,
    ) -> C2PASignResult:
        """Signe l'image avec un manifest C2PA — fail-safe absolu."""
        normalized_mime = (mime_type or "").lower()
        if normalized_mime not in _SUPPORTED_MIMES:
            log.info(
                "c2pa.real.skipped_unsupported_format",
                mime_type=mime_type,
                supported=sorted(_SUPPORTED_MIMES),
            )
            return C2PASignResult(
                image_bytes=image_bytes,
                applied=False,
                skip_reason="unsupported_format",
            )

        try:
            signed_bytes, manifest_id = await self._sign_via_c2pa_lib(
                image_bytes=image_bytes,
                mime_type=normalized_mime,
                request=request,
            )
            return C2PASignResult(
                image_bytes=signed_bytes,
                applied=True,
                manifest_id=manifest_id,
                signed_at=datetime.now(UTC),
                metadata={
                    "algorithm": self._signing_algorithm,
                    "creator": self._creator_name,
                    "c2pa_version": C2PA_VERSION,
                },
            )
        except Exception as exc:  # noqa: BLE001 — fail-safe absolu
            log.warning(
                "c2pa.real.sign_failed",
                error=str(exc),
                error_type=type(exc).__name__,
                mime_type=mime_type,
                provider=request.provider,
                model=request.model,
            )
            return C2PASignResult(
                image_bytes=image_bytes,
                applied=False,
                skip_reason="sign_error",
            )

    async def _sign_via_c2pa_lib(
        self,
        *,
        image_bytes: bytes,
        mime_type: str,
        request: C2PASignRequest,
    ) -> tuple[bytes, str]:
        """Appelle la lib c2pa-python pour produire les bytes signés.

        Construit un manifest avec :
        - `claim_generator` : NEXYA + version backend.
        - Assertion `c2pa.actions` : `c2pa.created` (l'image a été
          générée par IA, jamais éditée par humain).
        - Assertion `c2pa.training-mining` : `notAllowed` partout
          (NEXYA refuse explicitement que ses images servent à
          entraîner d'autres modèles — défense brand + RGPD).
        - Assertion custom `ai.nexya.generation` : provider + model +
          prompt + timestamp + watermark info (E4) pour traçabilité
          complète Content Credentials.
        """
        import c2pa  # type: ignore[import-not-found]

        from app.config import settings

        app_version = getattr(settings, "app_version", "dev") or "dev"

        manifest_definition: dict[str, Any] = {
            "claim_generator": f"NEXYA/{app_version}",
            "claim_generator_info": [
                {
                    "name": self._creator_name,
                    "version": app_version,
                }
            ],
            "title": "NEXYA AI-Generated Image",
            "format": mime_type,
            "assertions": [
                {
                    "label": "c2pa.actions",
                    "data": {
                        "actions": [
                            {
                                "action": "c2pa.created",
                                "softwareAgent": f"NEXYA/{app_version}",
                                "digitalSourceType": (
                                    "http://cv.iptc.org/newscodes/"
                                    "digitalsourcetype/trainedAlgorithmicMedia"
                                ),
                            }
                        ]
                    },
                },
                {
                    "label": "c2pa.training-mining",
                    "data": {
                        "entries": {
                            "c2pa.ai_generative_training": {"use": "notAllowed"},
                            "c2pa.ai_inference": {"use": "notAllowed"},
                            "c2pa.ai_training": {"use": "notAllowed"},
                            "c2pa.data_mining": {"use": "notAllowed"},
                        }
                    },
                },
                {
                    "label": "ai.nexya.generation",
                    "data": {
                        "provider": request.provider,
                        "model": request.model,
                        "generation_timestamp": (request.generation_timestamp.isoformat()),
                        "watermark_applied": request.watermark_applied,
                        "watermark_version": request.watermark_version,
                        "prompt_truncated": request.prompt[:500],
                    },
                },
            ],
        }

        # API c2pa-python 0.6+ : Builder + signer callback.
        # On reste générique sur l'API pour absorber les évolutions
        # mineures de la lib — getattr + duck-typing.
        builder_cls = getattr(c2pa, "Builder", None)
        if builder_cls is None:  # pragma: no cover — sanity fallback
            raise C2PASignError("c2pa.Builder API introuvable dans la lib installée.")

        builder = builder_cls(manifest_definition)

        # Signer local via clés X.509 chargées au boot.
        sign_method = getattr(builder, "sign", None) or getattr(builder, "sign_bytes", None)
        if sign_method is None:  # pragma: no cover
            raise C2PASignError("Builder.sign(_bytes) introuvable dans la lib.")

        signed_bytes: bytes = sign_method(  # type: ignore[operator]
            input_bytes=image_bytes,
            input_format=mime_type,
            certificate=self._certificate_bytes,
            private_key=self._key_bytes,
            algorithm=self._signing_algorithm,
        )

        # Génère un manifest_id traçable côté metadata Library.
        # c2pa lib peut retourner un id natif via `builder.manifest_label`,
        # sinon on en construit un déterministe à partir du timestamp.
        manifest_id = (
            getattr(builder, "manifest_label", None)
            or f"nexya-c2pa-{datetime.now(UTC).strftime('%Y%m%d%H%M%S%f')}"
        )

        return signed_bytes, str(manifest_id)


# ═══════════════════════════════════════════════════════════════════
# MOCK — accumule les appels pour tests + dev sans clés X.509
# ═══════════════════════════════════════════════════════════════════


class MockManifestProvider(ManifestProvider):
    """Provider mock — log un fake manifest_id et accumule les calls.

    Permet :
    1. Dev local sans clés X.509.
    2. Tests pytest sans dep `c2pa-python`.
    3. CI sans secrets crypto.

    `force_skip=True` simule un format non supporté (retourne `applied=False`)
    pour tester le fail-safe côté caller (`/image/generate`).

    **N'altère JAMAIS l'image** — retourne les bytes originaux. Le flag
    `applied=True` permet quand même de tester le flow Library (metadata
    `has_c2pa=True` injectée pour traçabilité bout-en-bout).
    """

    name: str = "c2pa-mock"  # même family que real — caller indistinguable

    def __init__(self, *, force_skip: bool = False) -> None:
        self.force_skip = force_skip
        self.calls: list[tuple[str, C2PASignRequest]] = []
        self._counter = 0

    async def sign_image(
        self,
        image_bytes: bytes,
        mime_type: str,
        request: C2PASignRequest,
    ) -> C2PASignResult:
        self.calls.append((mime_type, request))
        if self.force_skip:
            log.info("c2pa.mock.force_skip", mime_type=mime_type)
            return C2PASignResult(
                image_bytes=image_bytes,
                applied=False,
                skip_reason="mock_force_skip",
            )
        if (mime_type or "").lower() not in _SUPPORTED_MIMES:
            return C2PASignResult(
                image_bytes=image_bytes,
                applied=False,
                skip_reason="unsupported_format",
            )
        self._counter += 1
        fake_id = f"mock-c2pa-{self._counter:06d}"
        log.info(
            "c2pa.mock.signed",
            manifest_id=fake_id,
            calls=len(self.calls),
            provider=request.provider,
            model=request.model,
        )
        return C2PASignResult(
            image_bytes=image_bytes,  # mock = bytes inchangés
            applied=True,
            manifest_id=fake_id,
            signed_at=datetime.now(UTC),
            metadata={
                "algorithm": "mock",
                "creator": "NEXYA-Mock",
                "c2pa_version": C2PA_VERSION,
            },
        )


# ═══════════════════════════════════════════════════════════════════
# FACTORY — singleton lazy
# ═══════════════════════════════════════════════════════════════════


_PROVIDER: ManifestProvider | None = None


def get_manifest_provider() -> ManifestProvider:
    """Retourne le singleton ManifestProvider.

    Décision mock-first :
    - `c2pa_enabled=False` → MockManifestProvider (kill-switch).
    - `c2pa_mock_enabled=True` → MockManifestProvider forcé (CI/tests).
    - `c2pa_signing_certificate_path` ou `c2pa_signing_key_path` vides
      → MockManifestProvider + log warning (mode dégradé visible).
    - `RealC2PAProvider` raise `C2PAConfigError` au __init__
      (clés introuvables / lib c2pa absente) → fallback Mock + log error.
    - Sinon RealC2PAProvider.

    Production safety : `_enforce_production_safety` dans `app/config.py`
    fail-fast au boot si `is_production AND c2pa_enabled AND clés vides`,
    pour empêcher un déploiement prod « pseudo-conforme AI Act » qui
    signerait silencieusement en mock.
    """
    global _PROVIDER
    if _PROVIDER is not None:
        return _PROVIDER

    from app.config import settings

    # Kill-switch global.
    if not getattr(settings, "c2pa_enabled", True):
        log.info("c2pa.factory.disabled_by_killswitch")
        _PROVIDER = MockManifestProvider()
        return _PROVIDER

    # Force mock (CI / dev local).
    if getattr(settings, "c2pa_mock_enabled", False):
        log.info("c2pa.factory.forced_mock_via_setting")
        _PROVIDER = MockManifestProvider()
        return _PROVIDER

    cert_path = getattr(settings, "c2pa_signing_certificate_path", "")
    key_path = getattr(settings, "c2pa_signing_key_path", "")

    if not cert_path or not key_path:
        log.warning(
            "c2pa.factory.mock_first_no_keys",
            cert_path=cert_path or "<empty>",
            key_path=key_path or "<empty>",
            hint=(
                "Fournir C2PA_SIGNING_CERTIFICATE_PATH + C2PA_SIGNING_KEY_PATH "
                "puis `pip install c2pa-python` pour activer la signature réelle."
            ),
        )
        _PROVIDER = MockManifestProvider()
        return _PROVIDER

    try:
        _PROVIDER = RealC2PAProvider(
            certificate_path=cert_path,
            key_path=key_path,
            creator_name=getattr(settings, "c2pa_creator_name", "NEXYA"),
            signing_algorithm=getattr(settings, "c2pa_signing_algorithm", "es256"),
        )
        return _PROVIDER
    except C2PAConfigError as exc:
        log.error(
            "c2pa.factory.real_init_failed_fallback_mock",
            error=str(exc),
        )
        _PROVIDER = MockManifestProvider()
        return _PROVIDER


def reset_manifest_provider_for_tests() -> None:
    """Reset du singleton — usage tests uniquement."""
    global _PROVIDER
    _PROVIDER = None
