"""
ABC `VisionProvider` + types neutres + hiérarchie d'erreurs.

Miroir strict du pattern `VoiceProvider` (E1), `EmbeddingsProvider` (D1),
`ChatProvider` (B1) :

- `ImageInput` / `VisionResult` dataclasses neutres côté SDK.
- Erreurs typées `VisionAuthError` / `VisionRateLimitError` /
  `VisionUnavailableError` / `VisionInvalidRequestError` /
  `VisionContentFilteredError` — mapping identique pour tous les providers.
- Contrat minimal : `analyze_images(images, prompt, tier, system_prompt, max_output_tokens)`.

Discipline :
- Aucune dépendance SDK externe ici — testable sans Gemini/OpenAI/Claude.
- `supports_tiers` déclaratif : un provider peut supporter `{flash}`,
  `{pro}`, ou `{flash, pro}`. La factory runtime choisit le bon provider
  selon le tier demandé.
- `cost_usd` calculé côté provider avec sa grille de prix — permet
  l'agrégation cross-provider dans la table `vision_analyses`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

# ══════════════════════════════════════════════════════════════
# TYPES
# ══════════════════════════════════════════════════════════════

# Tier de qualité/prix. `flash` = cheapest (Gemini Flash), `pro` = premium
# (Gemini Pro ou GPT-4o selon `vision_pro_provider`). L'user Free est
# forcé à `flash`, le Pro peut choisir.
VisionTier = Literal["flash", "pro"]


@dataclass(frozen=True, slots=True)
class ImageInput:
    """Image prête à envoyer au LLM multimodal.

    - `data` : bytes après resize Pillow (≤ 2048×2048).
    - `mime_type` : `image/png` / `image/jpeg` / `image/webp` / `image/gif`.
    - `width` / `height` : dimensions post-resize (tracés dans la DB).
    """

    data: bytes
    mime_type: str
    width: int | None
    height: int | None


@dataclass(frozen=True, slots=True)
class VisionResult:
    """Résultat d'un appel `analyze_images()`.

    `cost_usd` calculé par le provider selon sa grille de prix actuelle.
    Permet l'agrégation `SUM(cost_usd) GROUP BY model` cross-provider.
    """

    text: str
    tokens_input: int
    tokens_output: int
    model: str
    provider: str
    cost_usd: float


# ══════════════════════════════════════════════════════════════
# ERREURS TYPÉES — miroir voice/embeddings/providers
# ══════════════════════════════════════════════════════════════


class VisionError(Exception):
    def __init__(self, message: str, *, provider: str = "unknown") -> None:
        super().__init__(message)
        self.provider = provider


class VisionAuthError(VisionError):
    """Clé API absente ou invalide."""


class VisionRateLimitError(VisionError):
    """Quota provider dépassé côté upstream."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "unknown",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.retry_after = retry_after


class VisionUnavailableError(VisionError):
    """Provider injoignable (réseau, 5xx, timeout)."""


class VisionInvalidRequestError(VisionError):
    """Requête mal formée (image trop grosse, format non supporté)."""


class VisionContentFilteredError(VisionError):
    """Le contenu (image ou prompt) a été bloqué par la safety policy du
    provider. Renvoyé 400 côté API — l'user reformule."""


# ══════════════════════════════════════════════════════════════
# INTERFACE ABSTRAITE
# ══════════════════════════════════════════════════════════════


class VisionProvider(ABC):
    """Contrat minimal pour un fournisseur de vision multimodale.

    Chaque impl expose :
    - `name` : `gemini` / `openai` / `anthropic` / `mock`.
    - `supports_tiers` : ensemble des tiers supportés. Un provider qui
      ne supporte pas un tier demandé doit être filtré en amont par la
      factory runtime.

    Méthode `analyze_images(images, prompt, *, tier, system_prompt, max_output_tokens)` :
    - `images` : liste non-vide (cap 4 côté service) de `ImageInput`.
    - `prompt` : texte utilisateur (≤ 4000 chars).
    - `tier` : choix qualité/prix `flash` | `pro`.
    - `system_prompt` : préfixe système optionnel (défense prompt-injection).
    - `max_output_tokens` : cap sur la réponse générée.
    """

    name: str
    supports_tiers: set[VisionTier]

    @abstractmethod
    async def analyze_images(
        self,
        images: list[ImageInput],
        prompt: str,
        *,
        tier: VisionTier = "flash",
        system_prompt: str | None = None,
        max_output_tokens: int = 1024,
    ) -> VisionResult:
        """Analyse N images + un prompt, retourne du texte + tracking coût."""
