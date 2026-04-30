"""
ABC `VoiceProvider` + types neutres + hiérarchie d'erreurs.

Miroir strict du pattern `EmbeddingsProvider` (D1) et `ChatProvider` (B1) :

- `TranscriptionResult` / `TTSResult` dataclasses neutres côté SDK.
- Erreurs typées `VoiceAuthError` / `VoiceRateLimitError` /
  `VoiceUnavailableError` / `VoiceInvalidRequestError` — mapping
  identique pour tous les futurs providers (OpenAI, faster-whisper,
  Deepgram, Google Speech-to-Text).
- Contrat minimal : `transcribe(audio_bytes, ...)` et `synthesize(text, ...)`.
- Les futurs providers (faster-whisper self-hosted, Deepgram) se
  contentent d'implémenter ces 2 méthodes.

Discipline :
- Aucune dépendance SDK externe ici — testable sans OpenAI ni GPU.
- `cost_usd` calculé côté provider avec sa grille de prix — permet
  l'agrégation cross-provider dans la table `voice_transcriptions`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Literal

# ══════════════════════════════════════════════════════════════
# TYPES
# ══════════════════════════════════════════════════════════════

# Voix TTS supportées par OpenAI (6 voix stables). Si un futur provider
# n'expose pas ces voix exactes, il doit les mapper sur les siennes.
TTSVoice = Literal["alloy", "echo", "fable", "onyx", "nova", "shimmer"]

# Formats audio de sortie TTS supportés par OpenAI. MP3 = défaut
# universel, accepté par tous les clients Flutter / web.
TTSFormat = Literal["mp3", "opus", "aac", "flac"]


@dataclass(frozen=True, slots=True)
class TranscriptionResult:
    """Résultat d'un appel `transcribe()`.

    - `text` : texte transcrit (peut être vide si audio silencieux).
    - `language` : code ISO-639-1 détecté (ex: 'fr', 'en') ou None si le
      provider ne fournit pas la détection.
    - `duration_seconds` : durée réelle de l'audio d'entrée, **retournée
      par le provider** (pas l'estimation pré-appel). Clé pour la
      facturation côté `BudgetTracker`.
    - `model` : identifiant modèle (`whisper-1`, `faster-whisper-large-v3`,
      `mock-whisper`). Utilisé pour l'agrégation GROUP BY dans
      `voice_transcriptions`.
    - `provider` : `openai` / `faster-whisper-local` / `mock`.
    - `cost_usd` : coût calculé par le provider selon sa grille de prix
      (Whisper $0.006/min, Deepgram $0.0043/min, etc.). 0.0 pour mock.
    """

    text: str
    language: str | None
    duration_seconds: float
    model: str
    provider: str
    cost_usd: float


@dataclass(frozen=True, slots=True)
class TTSResult:
    """Résultat d'un appel `synthesize()`.

    - `audio_bytes` : MP3 (ou format demandé) complet en mémoire.
      OpenAI TTS API retourne des fichiers de l'ordre de 10-100 KB
      pour les textes de 1000-4000 chars, tenir en RAM est acceptable.
    - `mime_type` : `audio/mpeg` pour MP3, `audio/ogg` pour Opus, etc.
    - `voice` : voix utilisée (pour traçabilité côté Library).
    - `model` : `tts-1` / `tts-1-hd` / `mock-tts`.
    - `chars` : nombre de caractères facturés (len(text)).
    - `cost_usd` : coût facturé. 0.0 pour mock.
    """

    audio_bytes: bytes
    mime_type: str
    voice: str
    model: str
    provider: str
    chars: int
    cost_usd: float


# ══════════════════════════════════════════════════════════════
# ERREURS TYPÉES — miroir embeddings/providers
# ══════════════════════════════════════════════════════════════


class VoiceError(Exception):
    def __init__(self, message: str, *, provider: str = "unknown") -> None:
        super().__init__(message)
        self.provider = provider


class VoiceAuthError(VoiceError):
    """Clé API absente ou invalide."""


class VoiceRateLimitError(VoiceError):
    """Quota provider dépassé côté upstream (pas notre quota interne)."""

    def __init__(
        self,
        message: str,
        *,
        provider: str = "unknown",
        retry_after: float | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.retry_after = retry_after


class VoiceUnavailableError(VoiceError):
    """Provider injoignable (réseau, 5xx, timeout)."""


class VoiceInvalidRequestError(VoiceError):
    """Requête mal formée (audio trop gros, format non supporté, voix
    inconnue côté provider)."""


# ══════════════════════════════════════════════════════════════
# INTERFACE ABSTRAITE
# ══════════════════════════════════════════════════════════════


class VoiceProvider(ABC):
    """Contrat minimal pour un fournisseur voice.

    2 méthodes obligatoires :
    - `transcribe(audio_bytes, *, filename, mime_type, language)` — STT.
    - `synthesize(text, *, voice, speed, model, fmt)` — TTS.

    Chaque implémentation est responsable de :
    - Mapper les erreurs SDK natives en `VoiceError` typée.
    - Calculer `cost_usd` selon sa grille de prix (les coûts ne sont
      pas hardcodés dans `VoiceService`).
    """

    name: str

    @abstractmethod
    async def transcribe(
        self,
        audio_bytes: bytes,
        *,
        filename: str,
        mime_type: str,
        language: str | None = None,
    ) -> TranscriptionResult:
        """Transcrit l'audio en texte.

        `language` optionnel : si `None`, le provider auto-détecte.
        Si fourni (ex: `'fr'`), le provider respecte (meilleur WER sur
        une langue fixée que sur l'auto-détection).
        """

    @abstractmethod
    async def synthesize(
        self,
        text: str,
        *,
        voice: TTSVoice = "alloy",
        speed: float = 1.0,
        model: str = "tts-1",
        fmt: TTSFormat = "mp3",
    ) -> TTSResult:
        """Génère un audio à partir du texte."""
