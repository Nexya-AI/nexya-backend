"""
NEXYA — Préambule système assemblé (Session A1, 2026-05-19).

Module qui compose le préambule canonique injecté EN TÊTE du system
prompt LLM via `app/ai/streaming.py::_stream_link`. Source de vérité
unique pour l'identité + ton + routing de NEXYA AI.

Ordre de concaténation final dans `_stream_link` (le preamble vient
EN PREMIER, avant tout autre contexte) :

    [nexya_preamble]            <-- CE MODULE (Session A1)
    [memory_context]            <-- D3 : qui est l'utilisateur
    [expert_corpus_context]     <-- G1 : corpus spécialisé (cooking RAG)
    [rag_block]                 <-- I1 : documents utilisateur
    [expert system_prompt]      <-- experts.py : identité métier expert

Pattern miroir architectural strict de :
  - `app/features/memory/context_builder.py` (D3, helper `build_memory_context`)
  - `app/features/experts/context_builder.py` (G1, helper `build_expert_corpus_context`)

Discipline non-négociable :

1. **Fail-safe absolue** — toute exception interne (erreur de format,
   import circulaire potentiel, valeur de settings corrompue, etc.)
   capturée et convertie en `return None`. Le chat ne doit JAMAIS être
   bloqué par un dysfonctionnement du preamble. L'utilisateur préfère
   une réponse sans branding NEXYA à une 503 LLM_UNAVAILABLE.

2. **Kill-switch global** — si `settings.nexya_preamble_enabled=False`,
   retour `None` immédiat sans aucune autre opération. Permet à Ivan
   de désactiver tout le preamble en hotfix config sans déployer de
   code en cas d'incident prod (ex: la matrice routing déclenche trop
   de faux positifs, le ton génère des réponses étranges, etc.).

3. **Cap chars strict** — `settings.nexya_preamble_max_chars` (défaut
   4000). Au-delà, troncature lisible LLM avec marqueur explicite. Cap
   garantit que le token estimator B2 (cap 30k tokens prompt) ne sera
   jamais débordé par un preamble qui aurait gonflé silencieusement.

4. **Single Source of Truth** — la concaténation finale avec les autres
   blocs (memory, corpus, rag, system_prompt expert) se fait UNIQUEMENT
   dans `_stream_link`. Ce module produit le bloc preamble prêt-à-coller,
   il ne fait pas la composition transverse.

5. **Idempotence stricte** — deux appels avec mêmes arguments retournent
   exactement le même string byte-à-byte. Aucun timestamp, aucun random,
   aucun appel I/O. Permet le caching B2 efficace.
"""

from __future__ import annotations

from typing import Final, Literal

import structlog

from app.ai.nexya_identity import get_identity
from app.ai.nexya_routing import get_routing_guidance
from app.ai.nexya_tone import get_tone
from app.config import settings

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes locales
# ══════════════════════════════════════════════════════════════

_SECTION_SEPARATOR: Final[str] = "\n\n"

# Marqueur de troncature lisible par le LLM (FR + EN — on injecte le
# bon selon la locale au moment de l'appel).
_TRUNCATION_MARKER_FR: Final[str] = "\n\n[... préambule tronqué pour respecter la limite de taille]"
_TRUNCATION_MARKER_EN: Final[str] = "\n\n[... preamble truncated to respect size limit]"


Locale = Literal["fr", "en"]


# ══════════════════════════════════════════════════════════════
# API publique
# ══════════════════════════════════════════════════════════════


def build_nexya_preamble(
    expert_id: str | None = None,
    *,
    locale: Locale | None = None,
    include_routing: bool = True,
) -> str | None:
    """Retourne le préambule NEXYA prêt à injecter dans le system prompt.

    Composition (dans l'ordre) :
        1. Ton conversationnel (`nexya_tone.get_tone(locale)`)
        2. Routing guidance avec l'expert actif
           (`nexya_routing.get_routing_guidance(expert_id, locale)`)
        3. Identité fondateur + sécurité brand + produit + features
           (`nexya_identity.get_identity(locale)`)

    Ordre figé Session A1 (2026-05-19) : tone et routing sont les
    sections les plus compactes (~5500 chars cumulés) et toujours
    critiques (tone = comportement, routing = aiguillage cross-expert).
    Identity est la section la plus volumineuse (~10500 chars) et
    place les sections moins critiques (product description + 15
    features) en queue — c'est là que la troncature mord si overflow,
    préservant le tone + le routing + l'identité fondateur + sécurité
    brand qui restent intacts.

    Pipeline :
        1. Short-circuit si `settings.nexya_preamble_enabled=False` → None.
        2. Résolution locale (paramètre > settings.nexya_preamble_default_locale > 'fr').
        3. Assemblage des 3 sections séparées par `\\n\\n`.
        4. Cap chars : si total > `settings.nexya_preamble_max_chars`,
           troncature au dernier `\\n` qui tient dans le budget + marqueur.
        5. Fail-safe absolue : toute exception → log warning + return None.

    Args:
        expert_id: slug de l'expert actif (general, computer, cooking, …).
            None ou inconnu → traité comme 'general'.
        locale: 'fr' ou 'en'. None → lit `settings.nexya_preamble_default_locale`.
            Locale invalide → 'fr' fallback.
        include_routing: si False, omet la section routing guidance
            (utile pour des tests qui veulent isoler tone + identity).

    Returns:
        Le bloc preamble assemblé (1500-4000 chars typique), ou `None`
        si le kill-switch est off OU si une erreur interne survient.
    """
    try:
        if not settings.nexya_preamble_enabled:
            return None

        effective_locale: Locale = _resolve_locale(locale)

        # Composition des 3 sections — ordre figé tone → routing → identity.
        # Le routing vient en 2ᵉ position (et non en queue comme une version
        # antérieure) pour garantir qu'il ne soit JAMAIS tronqué — c'est la
        # valeur ajoutée critique de Session A1 (aiguillage cross-expert
        # intelligent). Identity en queue car ses sections terminales
        # (product description + 15 features) sont les moins critiques et
        # peuvent partir en troncature sans casser l'essence NEXYA.
        tone_block = get_tone(effective_locale)
        identity_block = get_identity(effective_locale)

        parts: list[str] = [tone_block]

        if include_routing:
            routing_block = get_routing_guidance(expert_id, effective_locale)
            parts.append(routing_block)

        parts.append(identity_block)

        assembled = _SECTION_SEPARATOR.join(p for p in parts if p)

        # Cap chars : si on dépasse, troncature lisible.
        max_chars = _get_max_chars()
        if len(assembled) <= max_chars:
            return assembled

        return _truncate_with_marker(assembled, max_chars, effective_locale)

    except Exception as exc:  # noqa: BLE001 — fail-safe absolue
        log.warning(
            "nexya.preamble.build_failed",
            expert_id=str(expert_id) if expert_id else None,
            locale=str(locale) if locale else None,
            error=str(exc),
            error_type=type(exc).__name__,
        )
        return None


# ══════════════════════════════════════════════════════════════
# Helpers privés
# ══════════════════════════════════════════════════════════════


def _resolve_locale(locale: Locale | None) -> Locale:
    """Résout la locale effective avec fallback safe.

    Priorité : param > settings.nexya_preamble_default_locale > 'fr'.
    Toute valeur invalide retombe sur 'fr' (langue principale Africa-first).
    """
    if locale in ("fr", "en"):
        return locale  # type: ignore[return-value]

    default = getattr(settings, "nexya_preamble_default_locale", "fr")
    if default == "en":
        return "en"
    return "fr"


def _get_max_chars() -> int:
    """Lit le cap chars depuis settings avec garde-fou.

    Cap minimal absolu = 500 chars (rationnel : sous ce seuil, le
    preamble n'a pas le minimum vital pour faire son travail — autant
    le désactiver via kill-switch).
    """
    value = getattr(settings, "nexya_preamble_max_chars", 4000)
    try:
        as_int = int(value)
    except (TypeError, ValueError):
        return 4000
    return max(500, as_int)


def _truncate_with_marker(text: str, max_chars: int, locale: Locale) -> str:
    """Tronque proprement au dernier `\\n` + marqueur lisible LLM.

    Préserve la structure markdown du preamble : on coupe au dernier
    saut de ligne pour ne pas hacher une section au milieu d'une phrase.

    Args:
        text: contenu complet à tronquer.
        max_chars: limite max (déjà validée >= 500 par caller).
        locale: 'fr' ou 'en' pour choisir le marqueur.

    Returns:
        Texte tronqué + marqueur. Garanti `len(result) <= max_chars`.
    """
    marker = _TRUNCATION_MARKER_EN if locale == "en" else _TRUNCATION_MARKER_FR
    budget = max_chars - len(marker)

    # Cas dégénéré : marker > max_chars (shouldn't happen mais defensif).
    if budget <= 0:
        # Renvoie au moins quelque chose qui tient dans la limite.
        return text[:max_chars]

    truncated = text[:budget]
    # Coupe au dernier `\n` pour finir sur une ligne complète.
    last_newline = truncated.rfind("\n")
    if last_newline > 0:
        truncated = truncated[:last_newline]
    return truncated + marker
