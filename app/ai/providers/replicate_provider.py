"""
NEXYA Couche IA — Provider Replicate (images) pour Flux 1.1 Pro.

Sert de **fallback** quand Imagen 4 (Vertex AI) refuse un prompt via
`ProviderContentFilteredError` — en particulier pour la génération de
personnalités publiques (politiciens, dirigeants, célébrités nommées)
que Google bloque au niveau infrastructure Trust & Safety.

Architecture cible (cf. `/image/generate` dans `app/main.py`) :
    try:
        images = await imagen.generate_images(request)
    except ProviderContentFilteredError:
        if replicate_enabled:
            images = await replicate.generate_images(request)
        else:
            raise

API Replicate utilisée (pattern « Run a model ») :
    POST https://api.replicate.com/v1/models/{owner}/{model}/predictions
    GET  https://api.replicate.com/v1/predictions/{id}   (polling)

Authentification : header `Authorization: Bearer r8_xxx` (token format
Replicate `r8_` + 32 chars hex). Token visible côté serveur uniquement,
jamais exposé au client Flutter.

Modèle par défaut : `black-forest-labs/flux-1.1-pro` (~$0.04/image,
qualité photo-réaliste premium 2025-2026, comparable à Gemini app).
Configurable via `settings.replicate_default_model` pour pouvoir basculer
sur `flux-schnell` (~$0.003/image, 15× moins cher) si Ivan veut
optimiser le coût en mode Africa-first.

Pattern de mapping erreurs :
- Token invalide / révoqué → `ProviderAuthError` (non-retryable)
- 429 rate limit → `ProviderRateLimitError` avec `retry_after`
- 5xx / timeout → `ProviderUnavailableError` (retryable)
- 400 input invalide → `ProviderInvalidRequestError` (non-retryable)
- Prediction `status='failed'` avec `error` contenant safety/policy
  → `ProviderContentFilteredError` (fallback exhausted)
"""

from __future__ import annotations

import asyncio
import base64
from typing import Any, Final

import httpx
import structlog

from app.config import settings

from .base import (
    GeneratedImage,
    ImageGenerationRequest,
    ImageProvider,
    ProviderAuthError,
    ProviderContentFilteredError,
    ProviderInvalidRequestError,
    ProviderRateLimitError,
    ProviderUnavailableError,
)

log = structlog.get_logger()


# Constants
_REPLICATE_API_BASE: Final[str] = "https://api.replicate.com/v1"
_DEFAULT_TIMEOUT_SECONDS: Final[float] = 60.0
_POLLING_MAX_WAIT_SECONDS: Final[float] = 90.0
_POLLING_INITIAL_DELAY: Final[float] = 1.5  # backoff exponentiel x1.5 jusqu'à 5s


class ReplicateImageProvider(ImageProvider):
    """Adaptateur Replicate Flux 1.1 Pro pour la génération d'images.

    Sert de fallback à `GeminiImageProvider` quand Imagen bloque un prompt
    via Trust & Safety (célébrités nommées, etc.). Replicate / Flux Pro a
    des filtres plus permissifs sur les personnes (configurable via
    `safety_tolerance`).
    """

    name = "replicate-flux"
    # Le model par défaut est lu dynamiquement depuis settings au moment
    # de l'init pour permettre le swap `flux-1.1-pro` → `flux-schnell`
    # via env var sans redéploiement code.
    default_model = "black-forest-labs/flux-1.1-pro"
    supported_models = frozenset({
        "black-forest-labs/flux-1.1-pro",
        "black-forest-labs/flux-1.1-pro-ultra",
        "black-forest-labs/flux-schnell",
        "black-forest-labs/flux-dev",
    })
    max_images_per_call = 4

    def __init__(
        self,
        api_token: str | None = None,
        default_model: str | None = None,
    ) -> None:
        """Init avec override optionnel des settings.

        Args:
            api_token: token Replicate `r8_xxx`. Si None, lit
                `settings.replicate_api_token`. Si vide → fail-fast côté
                `_get_token()` au premier appel (anti config silencieuse).
            default_model: modèle par défaut (`black-forest-labs/flux-1.1-pro`
                par défaut). Override pratique pour les tests + le swap
                cost-smart (flux-schnell, flux-dev).
        """
        self._api_token_override = api_token
        if default_model is not None:
            self.default_model = default_model

    def _get_token(self) -> str:
        """Lit le token Replicate avec fail-fast si vide.

        Le caller (`/image/generate`) ne devrait jamais déclencher ce
        provider sans avoir vérifié `settings.replicate_enabled=True ET
        replicate_api_token != ""`. Si on arrive ici sans token, c'est
        un bug de wiring → `ProviderAuthError` claire pour le debug.
        """
        token = self._api_token_override or settings.replicate_api_token
        if not token:
            raise ProviderAuthError(
                "REPLICATE_API_TOKEN non configuré côté serveur.",
                provider=self.name,
                model=self.default_model,
            )
        return token

    async def generate_images(
        self,
        request: ImageGenerationRequest,
    ) -> list[GeneratedImage]:
        """Génère N images via Replicate Flux 1.1 Pro.

        Pipeline :
            1. POST création de la prediction avec input formaté
            2. Polling status `succeeded`/`failed`/`canceled`
            3. Récupération des URLs d'output
            4. Téléchargement parallèle des bytes (gather)
            5. Encode base64 + retour `list[GeneratedImage]`

        Flux Pro ne supporte qu'1 image par prediction. Pour `count > 1`,
        on lance N predictions en parallèle via `asyncio.gather`.

        Args:
            request: requête neutre `ImageGenerationRequest` avec prompt,
                count (1-4), aspect_ratio.

        Returns:
            `list[GeneratedImage]` — exactement `request.count` images
            (jamais moins, sauf si Replicate refuse via safety filter
            → `ProviderContentFilteredError`).

        Raises:
            ProviderContentFilteredError: Replicate aussi a refusé (safety
                filter encore plus permissif que Flux Pro défaut).
            ProviderAuthError: token Replicate invalide.
            ProviderRateLimitError: quota Replicate dépassé (rare).
            ProviderUnavailableError: API down ou timeout.
            ProviderInvalidRequestError: input mal formé (bug NEXYA).
        """
        count = max(1, min(request.count, self.max_images_per_call))
        token = self._get_token()
        model = self.default_model

        # Flux Pro = 1 image par prediction → on lance N appels en parallèle.
        # asyncio.gather avec return_exceptions=False : si UNE prediction
        # échoue, on propage l'erreur (les autres sont annulées).
        tasks = [
            self._generate_one_image(token, model, request, image_index=i)
            for i in range(count)
        ]

        try:
            results = await asyncio.gather(*tasks)
        except (
            ProviderContentFilteredError,
            ProviderAuthError,
            ProviderRateLimitError,
            ProviderUnavailableError,
            ProviderInvalidRequestError,
        ):
            # Re-raise les erreurs typées du provider sans wrapping.
            raise
        except Exception as exc:
            log.error(
                "replicate.generate.unexpected_error",
                error=str(exc),
                error_type=type(exc).__name__,
            )
            raise ProviderUnavailableError(
                f"Erreur inattendue Replicate: {exc!s}",
                provider=self.name,
                model=model,
            ) from exc

        return results

    async def _generate_one_image(
        self,
        token: str,
        model: str,
        request: ImageGenerationRequest,
        image_index: int,
    ) -> GeneratedImage:
        """Génère UNE seule image via Replicate (Flux Pro = 1 img/prediction).

        Pattern « Run a model » officiel Replicate :
            1. POST /v1/models/{owner}/{model}/predictions → 201 + prediction_id
            2. Polling GET /v1/predictions/{prediction_id} jusqu'à succeeded
            3. GET output URL → image bytes
            4. base64 encode → GeneratedImage
        """
        # Construit l'input Flux Pro depuis la requête neutre.
        # Champs Flux Pro :
        # - prompt: str
        # - aspect_ratio: "1:1"|"16:9"|"3:2"|"2:3"|"4:5"|"5:4"|"9:16"|"3:4"|"4:3"
        # - output_format: "webp"|"png"|"jpg"
        # - safety_tolerance: 1-6 (6 = le plus permissif, idéal fallback)
        # - prompt_upsampling: bool (False par défaut pour respecter le prompt)
        input_payload: dict[str, Any] = {
            "prompt": request.prompt,
            "aspect_ratio": request.aspect_ratio or "1:1",
            "output_format": "jpg",
            # safety_tolerance=6 = max permissif (le but du fallback est
            # précisément de débloquer ce que Imagen refuse). Configurable
            # via settings si besoin d'un mode plus strict.
            "safety_tolerance": settings.replicate_safety_tolerance,
            "prompt_upsampling": False,
        }

        if request.negative_prompt:
            # Flux Pro n'a pas de `negative_prompt` natif mais accepte
            # une inclusion dans le prompt sous forme « not X, not Y ».
            # On le laisse au caller car la sémantique diffère trop
            # entre les providers — pour l'instant on ignore.
            pass

        # 1. POST création prediction
        owner, model_name = model.split("/", 1) if "/" in model else (model, "")
        url = f"{_REPLICATE_API_BASE}/models/{owner}/{model_name}/predictions"
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            # `Prefer: wait` demande à Replicate de garder la connexion
            # ouverte jusqu'à 60s en attente du résultat — économise
            # plusieurs round-trips polling pour les modèles rapides
            # comme Flux Schnell (1-2s). Flux Pro (~8-15s) bénéficie
            # aussi mais peut nécessiter du polling complémentaire.
            "Prefer": "wait=60",
        }
        body = {"input": input_payload}

        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS) as client:
            try:
                response = await client.post(url, headers=headers, json=body)
            except httpx.TimeoutException as exc:
                raise ProviderUnavailableError(
                    f"Timeout Replicate POST predictions: {exc!s}",
                    provider=self.name,
                    model=model,
                ) from exc
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(
                    f"Erreur réseau Replicate: {exc!s}",
                    provider=self.name,
                    model=model,
                ) from exc

            # Mapping des codes HTTP NON-2xx vers exceptions typées.
            if response.status_code == 401 or response.status_code == 403:
                raise ProviderAuthError(
                    f"Replicate auth refusée (HTTP {response.status_code}). "
                    "Vérifie REPLICATE_API_TOKEN côté serveur.",
                    provider=self.name,
                    model=model,
                )
            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                retry_seconds = float(retry_after) if retry_after else None
                raise ProviderRateLimitError(
                    "Replicate rate limit atteint (HTTP 429).",
                    provider=self.name,
                    model=model,
                    retry_after_seconds=retry_seconds,
                )
            if response.status_code == 400:
                # Input mal formé (bug NEXYA, pas un échec utilisateur).
                raise ProviderInvalidRequestError(
                    f"Replicate input invalide: {response.text}",
                    provider=self.name,
                    model=model,
                )
            if response.status_code >= 500:
                raise ProviderUnavailableError(
                    f"Replicate erreur serveur (HTTP {response.status_code}).",
                    provider=self.name,
                    model=model,
                    status_code=response.status_code,
                )
            if response.status_code not in (200, 201):
                raise ProviderUnavailableError(
                    f"Replicate réponse inattendue (HTTP {response.status_code}): "
                    f"{response.text[:200]}",
                    provider=self.name,
                    model=model,
                    status_code=response.status_code,
                )

            prediction = response.json()

        # 2. Polling jusqu'au status final (si Prefer: wait n'a pas suffi).
        prediction = await self._poll_prediction(
            token=token,
            prediction=prediction,
            model=model,
        )

        # 3. Vérifie le status final.
        final_status = prediction.get("status")
        if final_status == "failed":
            error_msg = prediction.get("error", "Unknown error")
            # Heuristique : si l'erreur mentionne safety / policy / nsfw,
            # on lève ContentFilteredError pour que le caller sache que
            # le fallback Replicate a aussi refusé (vraie limite ultime).
            error_lower = str(error_msg).lower()
            if any(
                kw in error_lower
                for kw in ("safety", "policy", "nsfw", "content", "moderation")
            ):
                raise ProviderContentFilteredError(
                    f"Replicate a aussi refusé via safety filter: {error_msg}",
                    provider=self.name,
                    model=model,
                )
            raise ProviderUnavailableError(
                f"Replicate prediction failed: {error_msg}",
                provider=self.name,
                model=model,
            )

        if final_status in ("canceled", "starting", "processing"):
            # Polling a timeout sans succès → on déclare le timeout comme
            # une erreur Unavailable (l'image n'est pas générée).
            raise ProviderUnavailableError(
                f"Replicate prediction non terminée après "
                f"{_POLLING_MAX_WAIT_SECONDS}s (status={final_status}).",
                provider=self.name,
                model=model,
            )

        if final_status != "succeeded":
            raise ProviderUnavailableError(
                f"Replicate status inattendu: {final_status}",
                provider=self.name,
                model=model,
            )

        # 4. Récupère l'URL de l'image (Flux Pro retourne soit string soit list).
        output = prediction.get("output")
        image_url: str | None = None
        if isinstance(output, str):
            image_url = output
        elif isinstance(output, list) and output:
            first = output[0]
            if isinstance(first, str):
                image_url = first

        if not image_url:
            raise ProviderUnavailableError(
                f"Replicate output absent ou format inattendu: {output!r}",
                provider=self.name,
                model=model,
            )

        # 5. Download bytes + base64 encode.
        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS) as client:
            try:
                img_response = await client.get(image_url)
                img_response.raise_for_status()
            except httpx.HTTPError as exc:
                raise ProviderUnavailableError(
                    f"Impossible de télécharger l'image Replicate: {exc!s}",
                    provider=self.name,
                    model=model,
                ) from exc

            image_bytes = img_response.content

        base64_data = base64.b64encode(image_bytes).decode("utf-8")

        log.info(
            "replicate.generate.success",
            model=model,
            image_index=image_index,
            bytes=len(image_bytes),
        )

        return GeneratedImage(
            base64_data=base64_data,
            mime_type="image/jpeg",
        )

    async def _poll_prediction(
        self,
        token: str,
        prediction: dict[str, Any],
        model: str,
    ) -> dict[str, Any]:
        """Poll Replicate jusqu'à status terminal (succeeded/failed/canceled).

        Backoff exponentiel x1.5 plafonné à 5s. Timeout global
        `_POLLING_MAX_WAIT_SECONDS` (90s).
        """
        status = prediction.get("status")
        if status in ("succeeded", "failed", "canceled"):
            return prediction

        prediction_id = prediction.get("id")
        if not prediction_id:
            raise ProviderUnavailableError(
                "Replicate prediction sans id — réponse malformée.",
                provider=self.name,
                model=model,
            )

        get_url = prediction.get("urls", {}).get("get") or (
            f"{_REPLICATE_API_BASE}/predictions/{prediction_id}"
        )
        headers = {"Authorization": f"Bearer {token}"}

        elapsed = 0.0
        delay = _POLLING_INITIAL_DELAY

        async with httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT_SECONDS) as client:
            while elapsed < _POLLING_MAX_WAIT_SECONDS:
                await asyncio.sleep(delay)
                elapsed += delay

                try:
                    response = await client.get(get_url, headers=headers)
                except httpx.HTTPError as exc:
                    # Network blip pendant le polling — on continue jusqu'au
                    # timeout global plutôt que d'abandonner immédiatement.
                    log.warning(
                        "replicate.poll.network_error",
                        error=str(exc),
                        elapsed=elapsed,
                    )
                    delay = min(delay * 1.5, 5.0)
                    continue

                if response.status_code != 200:
                    log.warning(
                        "replicate.poll.non_200",
                        status_code=response.status_code,
                        elapsed=elapsed,
                    )
                    delay = min(delay * 1.5, 5.0)
                    continue

                prediction = response.json()
                status = prediction.get("status")
                if status in ("succeeded", "failed", "canceled"):
                    return prediction

                # Status encore en `starting` ou `processing` → backoff
                delay = min(delay * 1.5, 5.0)

        # Timeout global sans status terminal → on retourne le dernier
        # état connu (qui sera mappé en Unavailable par le caller).
        return prediction
