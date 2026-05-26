"""
NEXYA — Préambule système assemblé (Session A1, 2026-05-19 + Two-Tier 2026-05-26).

Module qui compose le préambule canonique injecté EN TÊTE du system
prompt LLM via `app/ai/streaming.py::_stream_link`. Source de vérité
unique pour l'identité + ton + routing de NEXYA AI.

Pattern Two-Tier Smart Preamble (2026-05-26) :

    [CORE]      tone + identity_core (founder + brand + capability_teaser)
                + routing_guidance (5 règles comportementales)
                + safety_limits (4 catégories refus + format refus standard)
                → TOUJOURS injecté, ~3500-4000 tokens
    [EXTENDED]  identity_extended (product description + 15 features)
                + routing_table (correspondance domaine→expert)
                → INJECTÉ UNIQUEMENT si l'utilisateur pose une question
                  marketing (« qu'est-ce que tu sais faire ? »), ~3000 tokens
                  additionnels

Le déclencheur EXTENDED est `_detect_marketing_intent(user_message, locale)`
qui scanne le dernier message user pour ~30 mots-clés FR + ~25 mots-clés EN
(« tes capacités », « what can you do », « vs chatgpt », etc.).

Ordre de concaténation final dans `_stream_link` (le preamble vient
EN PREMIER, avant tout autre contexte) :

    [nexya_preamble]            <-- CE MODULE
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
   12000). Au-delà, troncature lisible LLM avec marqueur explicite. Cap
   garantit que le token estimator B2 (cap 30k tokens prompt) ne sera
   jamais débordé par un preamble qui aurait gonflé silencieusement.

4. **Single Source of Truth** — la concaténation finale avec les autres
   blocs (memory, corpus, rag, system_prompt expert) se fait UNIQUEMENT
   dans `_stream_link`. Ce module produit le bloc preamble prêt-à-coller,
   il ne fait pas la composition transverse.

5. **Idempotence stricte** — deux appels avec mêmes arguments (incluant
   `user_message`) retournent exactement le même string byte-à-byte.
   Aucun timestamp, aucun random, aucun appel I/O.

6. **Marketing detection déterministe** — `_detect_marketing_intent`
   utilise un matching keyword case-insensitive simple (regex non
   nécessaire). 0 appel LLM, 0 latence ajoutée. Cf. mémoire
   `project_nexya_preamble_two_tier_architecture.md`.
"""

from __future__ import annotations

from typing import Final, Literal

import structlog

from app.ai.nexya_identity import (
    get_identity,
    get_identity_core,
    get_identity_extended,
)
from app.ai.nexya_routing import (
    get_routing_guidance,
    get_routing_table_extended,
)
from app.ai.nexya_safety import get_safety_limits
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
# Marketing Intent Detection (Two-Tier 2026-05-26)
# ══════════════════════════════════════════════════════════════
#
# Liste de keywords qui déclenchent l'injection du bloc EXTENDED
# (product description + 15 features + routing table) en plus du
# CORE preamble (tone + identity_core + routing_rules + safety).
#
# Discipline :
# - Keywords case-insensitive (matching `.lower()` simple, pas de regex)
# - Conservateur : on préfère ne PAS déclencher l'EXTENDED (CORE seul)
#   sur une question ambiguë plutôt que sur-injecter
# - Pas de match sur substring fragile : on vise des phrases-clés
#   distinctives (« que sais-tu faire ? » et pas juste « sais »)
# - Liste exhaustive des formulations courantes FR + EN pour V1
# - V2 possible : embeddings similarity contre archetype queries
#   pour robustesse aux paraphrases inattendues

_MARKETING_KEYWORDS_FR: Final[tuple[str, ...]] = (
    # « Que sais-tu / peux-tu faire ? » et variantes
    "que sais-tu faire",
    "que peux-tu faire",
    "qu'est-ce que tu sais faire",
    "qu'est-ce que tu peux faire",
    "qu'est-ce que tu propose",
    "qu'est-ce que tu offre",
    "tu fais quoi",
    "tu sers à quoi",
    "à quoi tu sers",
    "ce que tu fais",
    "ce que tu offres",
    "raconte ce que tu fais",
    # « Tes capacités / features / fonctionnalités / experts »
    "tes capacités",
    "tes fonctionnalités",
    "tes features",
    "tes experts",
    "tes services",
    "tes outils",
    "ton arsenal",
    "tes points forts",
    # Comparaisons / différenciation
    "tu es différente",
    "ce qui te rend différente",
    "qu'est-ce qui te distingue",
    "tu es meilleure",
    "quoi de spécial",
    "vs chatgpt",
    "vs claude",
    "vs gemini",
    "pourquoi nexya",
    "pourquoi t'utiliser",
    "quel est ton intérêt",
    # Présentation / identité produit (large)
    "présente-toi",
    "qu'est-ce que nexya",
    "que peut nexya",
)


_MARKETING_KEYWORDS_EN: Final[tuple[str, ...]] = (
    # « What can you do » et variantes
    "what can you do",
    "what do you do",
    "what are you good at",
    "what do you offer",
    "what's your purpose",
    "what is nexya",
    "what does nexya do",
    # « Your capabilities / features / experts »
    "your capabilities",
    "your features",
    "your functions",
    "your experts",
    "your tools",
    "your services",
    "your strengths",
    # Comparaisons / différenciation
    "how are you different",
    "different from",
    "better than",
    "vs chatgpt",
    "vs claude",
    "vs gemini",
    "why nexya",
    "why use you",
    "what's special",
    "what makes you unique",
    # Présentation
    "tell me about yourself",
    "describe yourself",
)


def _detect_marketing_intent(user_message: str | None, locale: Locale) -> bool:
    """Détecte si le message utilisateur est une question marketing/produit.

    Retourne True si AU MOINS UN keyword (selon la locale) est présent
    dans le message (case-insensitive substring match). Sinon False.

    Args:
        user_message: dernier message utilisateur. None ou vide → False.
        locale: 'fr' (matche `_MARKETING_KEYWORDS_FR`) ou 'en'
            (matche `_MARKETING_KEYWORDS_EN`).

    Returns:
        True si marketing intent détecté → caller injecte EXTENDED.
        False sinon → CORE seul.

    Note:
        Détection conservatrice : on préfère un faux négatif (pas
        injecter EXTENDED sur une question ambiguë, le LLM utilisera
        le Capability Teaser du CORE pour donner un teaser) plutôt
        qu'un faux positif (gâchis tokens sur une vraie question
        métier où l'EXTENDED n'apporte rien).
    """
    if not user_message:
        return False
    text = user_message.strip().lower()
    if not text:
        return False
    keywords = _MARKETING_KEYWORDS_EN if locale == "en" else _MARKETING_KEYWORDS_FR
    return any(kw in text for kw in keywords)


# ══════════════════════════════════════════════════════════════
# API publique
# ══════════════════════════════════════════════════════════════


def build_nexya_preamble(
    expert_id: str | None = None,
    *,
    locale: Locale | None = None,
    include_routing: bool = True,
    user_message: str | None = None,
) -> str | None:
    """Retourne le préambule NEXYA prêt à injecter dans le system prompt.

    Pattern Two-Tier Smart Preamble (2026-05-26) :

    **CORE** (toujours injecté, ~3500-4000 tokens) :
        1. Ton conversationnel (`nexya_tone.get_tone(locale)`)
        2. Identity CORE — founder story + brand security + capability
           teaser (`nexya_identity.get_identity_core(locale)`)
        3. Routing guidance — règles comportementales
           (`nexya_routing.get_routing_guidance(expert_id, locale)`)
        4. Safety & Limites — 4 catégories refus + format refus standard
           (`nexya_safety.get_safety_limits(locale)`)

    **EXTENDED** (injecté SEULEMENT si `_detect_marketing_intent(user_message,
    locale)` retourne True — ~3000 tokens additionnels) :
        5. Identity EXTENDED — product description complète + 15
           magnificent features (`nexya_identity.get_identity_extended(locale)`)
        6. Routing TABLE — correspondance domaine→expert détaillée
           (`nexya_routing.get_routing_table_extended(locale)`)

    Ordre des composants : tone → identity_core → routing_rules →
    safety_limits → (identity_extended → routing_table si marketing intent).
    Le tone vient en TÊTE pour cadrer le comportement (10 commandements).
    Identity CORE en 2ᵉ (founder + brand + capability teaser) car
    critique pour identité et sécurité brand. Routing rules en 3ᵉ pour
    les comportements cross-expert. Safety en 4ᵉ (queue du CORE) pour
    effet de récence — signal fort sur les limites éthiques juste avant
    le contenu EXTENDED ou la concat finale (`_stream_link`). Les blocs
    EXTENDED arrivent ensuite, en queue car ils peuvent partir en
    troncature sans casser l'essence NEXYA si on dépasse le cap chars.

    Pipeline :
        1. Short-circuit si `settings.nexya_preamble_enabled=False` → None.
        2. Résolution locale (paramètre > settings.nexya_preamble_default_locale > 'fr').
        3. Build CORE : tone + identity_core + routing_rules.
        4. Si `user_message` ET `_detect_marketing_intent` True :
           ajout identity_extended + routing_table.
        5. Assemblage final séparés par `\\n\\n`.
        6. Cap chars : si total > `settings.nexya_preamble_max_chars`,
           troncature au dernier `\\n` qui tient dans le budget + marqueur.
        7. Fail-safe absolue : toute exception → log warning + return None.

    Args:
        expert_id: slug de l'expert actif (general, computer, cooking, …).
            None ou inconnu → traité comme 'general'.
        locale: 'fr' ou 'en'. None → lit `settings.nexya_preamble_default_locale`.
            Locale invalide → 'fr' fallback.
        include_routing: si False, omet la section routing guidance
            (utile pour des tests qui veulent isoler tone + identity).
        user_message: dernier message utilisateur. Si fourni ET marketing
            intent détecté, on injecte le bloc EXTENDED. Sinon, CORE seul.
            None ou vide → CORE seul (comportement par défaut safe).

    Returns:
        Le bloc preamble assemblé (CORE seul ~3500 chars typique, ou
        CORE+EXTENDED ~6500 chars typique), ou `None` si le kill-switch
        est off OU si une erreur interne survient.
    """
    try:
        if not settings.nexya_preamble_enabled:
            return None

        effective_locale: Locale = _resolve_locale(locale)

        # ── CORE PREAMBLE — toujours injecté ────────────────────
        # tone → identity_core (founder + brand + capability_teaser)
        # → routing_rules (règles comportementales transverses)
        # → safety_limits (4 catégories refus + format refus standard)
        tone_block = get_tone(effective_locale)
        identity_core_block = get_identity_core(effective_locale)
        safety_block = get_safety_limits(effective_locale)

        parts: list[str] = [tone_block, identity_core_block]

        if include_routing:
            routing_rules_block = get_routing_guidance(expert_id, effective_locale)
            parts.append(routing_rules_block)

        # Safety en queue du CORE pour effet de récence (juste avant
        # EXTENDED s'il y a lieu, sinon en dernier) — signal fort au LLM
        # sur les limites éthiques. Plus pratique pour anti-prompt-injection.
        parts.append(safety_block)

        # ── EXTENDED PREAMBLE — injecté si marketing intent ─────
        # Le LLM reçoit la description produit complète + les 15
        # features magnifiques + la table de correspondance routing.
        # Permet de déballer la richesse marketing avec fierté
        # uniquement quand l'utilisateur l'a demandé.
        if _detect_marketing_intent(user_message, effective_locale):
            identity_extended_block = get_identity_extended(effective_locale)
            parts.append(identity_extended_block)

            if include_routing:
                routing_table_block = get_routing_table_extended(effective_locale)
                parts.append(routing_table_block)

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
            user_message_present=bool(user_message),
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
