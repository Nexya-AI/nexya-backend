"""
MockVisionProvider — impl déterministe sans réseau pour dev/test/CI.

- `analyze_images()` retourne un texte synthétique basé sur le SHA-256
  des images + prompt — déterministe, testable sans clé API.
- Supporte les 2 tiers (flash + pro) pour couvrir tous les tests
  downstream sans besoin de mock séparé par tier.
- `cost_usd = 0.0` partout.

Activé automatiquement par `get_vision_provider()` quand :
- `settings.gemini_api_key` est vide.
- OU `settings.vision_mock_enabled=True` (force mock en CI).
"""

from __future__ import annotations

import hashlib
from typing import Final

import structlog

from app.ai.vision.base import (
    ImageInput,
    VisionInvalidRequestError,
    VisionProvider,
    VisionResult,
    VisionTier,
)

log = structlog.get_logger()


class MockVisionProvider(VisionProvider):
    """Mock déterministe — texte synthétique basé sur SHA des inputs."""

    name: Final[str] = "mock"
    supports_tiers: Final[set[VisionTier]] = {"flash", "pro"}

    async def analyze_images(
        self,
        images: list[ImageInput],
        prompt: str,
        *,
        tier: VisionTier = "flash",
        system_prompt: str | None = None,
        max_output_tokens: int = 1024,
    ) -> VisionResult:
        if not images:
            raise VisionInvalidRequestError("Au moins une image est requise", provider=self.name)

        # SHA combiné images + prompt pour déterminisme.
        h = hashlib.sha256()
        for img in images:
            h.update(img.data)
        h.update(prompt.encode("utf-8"))
        sha_short = h.hexdigest()[:16]

        n = len(images)
        dims = (
            ", ".join(f"{img.width}x{img.height}" for img in images if img.width and img.height)
            or "unknown"
        )
        text = (
            f"[MOCK vision sha={sha_short} tier={tier}] {n} image(s) "
            f"({dims}), prompt: '{prompt[:80]}" + ("…" if len(prompt) > 80 else "") + "'"
        )

        # Tokens synthétiques : 258 tokens par image (règle Gemini tiles)
        # + ~len(prompt)//4 pour le texte.
        tokens_input = 258 * n + max(1, len(prompt) // 4)
        tokens_output = min(max_output_tokens, 100 + n * 20)

        log.debug(
            "vision.mock.analyze",
            n_images=n,
            tier=tier,
            tokens_input=tokens_input,
        )

        return VisionResult(
            text=text,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            model=f"mock-vision-{tier}",
            provider=self.name,
            cost_usd=0.0,
        )
