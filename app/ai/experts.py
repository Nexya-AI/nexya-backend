"""
NEXYA Couche IA — Configuration des 10 experts.

Chaque expert mappe vers :
- Un provider + modèle primaire (ex: Gemini Flash par défaut, Pro pour les modes
  qui demandent de la réflexion profonde)
- Une chaîne de fallback (si le primaire est KO, on bascule)
- Un prompt système qui définit la personnalité et les garde-fous
- Une température et un max_tokens adaptés au domaine
- Un éventuel disclaimer à coller en préfixe de la première réponse
  (médecine, juridique : NEXYA ne remplace pas un professionnel)
- Une matrice `model_pill_mapping` (11 experts × 3 pills GEEK/LOTH/JUSTO)
  qui résout chaque pill UI vers (modèle Gemini, thinking_mode) selon
  l'expert actif. Permet aux pills d'avoir une vraie sémantique backend
  (pas seulement cosmétique) tout en préservant les invariants safety-
  critical (medicine = thinking always on) et G2 V8 (cooking = disable
  thinking partout).

Mapping frontend ↔ backend :
- Les `expert_id` correspondent EXACTEMENT à `ExpertDomain.name` côté Flutter
  (voir `expert_config.dart`). Tout renommage côté backend casse le contrat.

Liste des 10 experts :
- Actifs       : computer, science, finance, language, cooking
- Bientôt      : studio, engineering, productivity, medicine, legal
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

from app.ai.expert_prompts import (
    COMPUTER_PROMPT,
    COOKING_PROMPT,
    ENGINEERING_PROMPT,
    FINANCE_PROMPT,
    GENERAL_PROMPT,
    LANGUAGE_PROMPT,
    LEGAL_PROMPT,
    MEDICINE_PROMPT,
    PRODUCTIVITY_PROMPT,
    SCIENCE_PROMPT,
    STUDIO_PROMPT,
)

# ═══════════════════════════════════════════════════════════════════
# MODEL PILLS — résolution UI → backend (GEEK / LOTH / JUSTO)
# ═══════════════════════════════════════════════════════════════════
#
# Trois "pills" affichées dans la NxInputBar (frontend) :
# - GEEK  : puissance maximale (modèle pro + thinking selon contexte)
# - LOTH  : équilibré (défaut quotidien, qualité/vitesse)
# - JUSTO : rapide (réponse express, contextes simples)
#
# Chaque pill se résout vers un couple (model_tier, disable_thinking)
# DÉPENDANT DE L'EXPERT actif. Cette dépendance permet :
# 1. Préserver G2 V8 (Cuisine garde disable_thinking=True partout — le
#    benchmark a prouvé que Flash sans thinking sort 2.2× plus vite et
#    5× plus riche pour le format recette structuré, cf. CLAUDE.md
#    §15 entrée 2026-05-18).
# 2. Préserver safety-critical (Médecine garde thinking always on sur
#    les 3 pills — un patient mérite la même rigueur clinique qu'il
#    soit en JUSTO express ou GEEK approfondi).
# 3. Préserver safety-critical Légal (GEEK + LOTH thinking on, JUSTO
#    thinking off pour la rapidité — l'expert Légal info reste cadré).
# 4. Studio est image-only : pas de pill mapping, l'endpoint
#    /image/generate ignore le pill (geste utilisateur transparent).
#
# Decisions clés (validées Ivan 2026-05-23) :
# - Gemini-only V1 (Claude API trop cher : ~$1500/mois vs ~$300/mois
#   Gemini à 1k users payants). OpenRouter facile à brancher V2.
# - Default pattern (7 experts non-spéciaux) : GEEK = pro+thinking,
#   LOTH = pro sans thinking, JUSTO = flash sans thinking.
# ═══════════════════════════════════════════════════════════════════

ModelPillId = Literal["geek", "loth", "justo"]
ModelTier = Literal["flash", "pro"]


@dataclass(frozen=True, slots=True)
class ModelPillConfig:
    """Résolution backend d'un pill UI (GEEK/LOTH/JUSTO).

    Attributes:
        model_tier: "flash" → gemini-2.5-flash, "pro" → gemini-2.5-pro.
            Le helper `resolve_model_for_pill` fait la résolution vers
            le nom de modèle effectif.
        disable_thinking: True = thinking_budget=0 (Flash) ou 128 (Pro,
            minimum API forcé). False = thinking adaptatif activé.
            Voir [gemini.py:163-198] pour la traduction Flash/Pro.
    """

    model_tier: ModelTier
    disable_thinking: bool


# Pattern par défaut appliqué à 7 experts conversationnels :
# general, computer, science, finance, language, engineering, productivity
_DEFAULT_PILL_MAPPING: dict[str, ModelPillConfig] = {
    "geek": ModelPillConfig(model_tier="pro", disable_thinking=False),
    "loth": ModelPillConfig(model_tier="pro", disable_thinking=True),
    "justo": ModelPillConfig(model_tier="flash", disable_thinking=True),
}

# Cuisine — G2 V8 preserve : disable_thinking=True sur les 3 pills.
# Le benchmark 2026-05-18 a prouvé que pour le format recette (RAG +
# structure ingredients/étapes), Flash sans thinking est objectivement
# meilleur (2.2× plus rapide, 5× plus riche en sortie).
_COOKING_PILL_MAPPING: dict[str, ModelPillConfig] = {
    "geek": ModelPillConfig(model_tier="pro", disable_thinking=True),
    "loth": ModelPillConfig(model_tier="flash", disable_thinking=True),
    "justo": ModelPillConfig(model_tier="flash", disable_thinking=True),
}

# Médecine — Safety-critical MAX : thinking always on sur les 3 pills.
# Un patient en JUSTO mérite la même rigueur clinique qu'en GEEK.
# Le modèle reste Pro partout (vies humaines > coût latence).
_MEDICINE_PILL_MAPPING: dict[str, ModelPillConfig] = {
    "geek": ModelPillConfig(model_tier="pro", disable_thinking=False),
    "loth": ModelPillConfig(model_tier="pro", disable_thinking=False),
    "justo": ModelPillConfig(model_tier="pro", disable_thinking=False),
}

# Légal — Safety-critical : thinking on sur GEEK+LOTH (rigueur juridique),
# off sur JUSTO (rapidité pour cas factuels simples type "définition SARL").
# Modèle Pro partout (même JUSTO — pas de dégradation Flash sur sujets
# OHADA, articles Code civil exact obligatoire).
_LEGAL_PILL_MAPPING: dict[str, ModelPillConfig] = {
    "geek": ModelPillConfig(model_tier="pro", disable_thinking=False),
    "loth": ModelPillConfig(model_tier="pro", disable_thinking=False),
    "justo": ModelPillConfig(model_tier="pro", disable_thinking=True),
}

# Studio — image-only, mapping vide. Le helper `resolve_model_for_pill`
# court-circuite et retourne (None, None) — l'endpoint /image/generate
# ignore le pill (geste utilisateur transparent sur Studio).
_STUDIO_PILL_MAPPING: dict[str, ModelPillConfig] = {}


# Résolution du nom de modèle effectif depuis le tier.
_TIER_TO_MODEL: dict[ModelTier, str] = {
    "flash": "gemini-2.5-flash",
    "pro": "gemini-2.5-pro",
}


def resolve_model_for_pill(
    expert_id: str | None,
    pill: str | None,
) -> tuple[str | None, bool | None]:
    """Résout un pill UI vers (model_name, disable_thinking) pour un expert.

    Args:
        expert_id: slug expert ("general", "computer", ...). None ou
            inconnu → retombe sur "general" (cohérent avec
            `get_expert_config`).
        pill: "geek", "loth", "justo" (case-insensitive). None ou
            inconnu → retourne (None, None) — l'appelant doit retomber
            sur `config.primary_model` + `config.disable_thinking`
            (comportement legacy A1+A2 préservé).

    Returns:
        Tuple (model_name, disable_thinking) :
        - model_name : "gemini-2.5-flash" ou "gemini-2.5-pro" ou None.
        - disable_thinking : True/False/None.

        (None, None) signifie : pas de résolution pill possible
        (studio, pill inconnu, expert sans mapping). L'appelant doit
        utiliser la config primaire de l'expert.

    Fail-safe : aucune exception levée. Pill malformé, expert inconnu,
    studio → retour (None, None) silencieux. L'appelant continue avec
    la config legacy.
    """
    if not pill:
        return None, None
    normalized_pill = pill.strip().lower()
    if normalized_pill not in ("geek", "loth", "justo"):
        return None, None

    config = get_expert_config(expert_id)
    pill_config = config.model_pill_mapping.get(normalized_pill)
    if pill_config is None:
        # Studio (mapping vide) ou expert sans mapping configuré.
        return None, None

    model_name = _TIER_TO_MODEL.get(pill_config.model_tier)
    if model_name is None:
        # Garde-fou défensif — ne devrait jamais arriver.
        return None, None
    return model_name, pill_config.disable_thinking

# ═══════════════════════════════════════════════════════════════════
# DATACLASS — ExpertConfig
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ExpertConfig:
    """Configuration IA d'un mode expert NEXYA.

    `fallback_chain` : liste ordonnée `[(provider_name, model_name), ...]`.
    Le LlmRouter essaie le premier élément, et si le provider renvoie une
    erreur `retryable`, passe au suivant. Si tous échouent → `LLM_UNAVAILABLE`.
    """

    expert_id: str
    display_name: str
    is_coming_soon: bool
    primary_provider: str
    primary_model: str
    fallback_chain: tuple[tuple[str, str], ...]
    system_prompt: str
    temperature: float = 0.7
    max_tokens: int | None = None
    disclaimer: str | None = None

    # Métadonnées pour le suivi coût / analytics (non envoyées au LLM)
    tier: str = "flash"  # "flash" (léger/rapide) | "pro" (réflexion profonde)
    tags: tuple[str, ...] = field(default_factory=tuple)

    # G1 — Corpus RAG spécialisé (expert_corpus_chunks). Si True, le
    # router `/chat/stream` appelle `build_expert_corpus_context` avant
    # l'estimation tokens + cache key, et injecte top-K chunks Tatoeba /
    # recettes / docs techniques dans le system prompt via framing D5.
    # False par défaut — activé expert par expert (G1 Langues, G2 Cuisine,
    # G3 Studio, G4 Ingénierie, G5 Productivité, G6 Informatique, G7 Sciences).
    corpus_enabled: bool = False

    # F2.5 — Function calling. Si True, le router `/chat/stream` injecte
    # `tool_registry.build_openai_tools()` dans `StreamContext.tools` ;
    # le LLM peut alors décider d'appeler `create_task`, `list_tasks`,
    # `update_task` ou `pause_task` (4 tools Planner enregistrés au
    # lifespan).
    # [planner-from-chat LOT 4, 2026-05-22] Activé sur les **11 experts**,
    # y compris `medicine` et `legal` (décision produit Ivan). F2.5 les
    # avait exclus par prudence ; mais les 4 tools Planner sont bénins —
    # ils posent des rappels, ne prescrivent ni ne rédigent aucun acte.
    # Pouvoir planifier « prendre mes médicaments » depuis le mode
    # Médecine est un cas d'usage légitime et même plus pertinent là.
    tools_allowed: bool = True

    # [Fix 2026-05-22] Thinking mode Gemini 2.5 Pro/Flash DÉSACTIVÉ par
    # défaut sur TOUS les experts. Cause du bug « réponse vide » remonté
    # terrain : Gemini 2.5 a un `thinkingBudget=-1` (adaptatif) qui consomme
    # 5-25k tokens de raisonnement. Or `max_output_tokens` est plafonné à
    # 2048-4096 par expert → le modèle épuise TOUT son budget en pensant et
    # émet ZÉRO token de réponse visible (log `ai.chat.completed chunks_count=0`,
    # stream vide, l'app affiche une bulle sans contenu).
    # `disable_thinking=True` rend tout le budget à la réponse réelle ET
    # supprime la latence ~15-30s. La richesse des prompts A1/A2 (persona +
    # méthodologie + few-shot examples) rend la phase de thinking séparée
    # non nécessaire — la qualité est portée par le prompt, pas par le
    # raisonnement caché. Propagé via `request.extra["disable_thinking"]`.
    # ⚠️ Ne mettre `False` sur un expert QUE si on relève AUSSI son
    # `max_tokens` à 8192+ (sinon le bug « réponse vide » revient).
    disable_thinking: bool = True

    # Pills modèles UI (GEEK / LOTH / JUSTO). Si l'utilisateur a sélectionné
    # une pill avant d'envoyer son message, le router `/chat/stream` résout
    # via `resolve_model_for_pill(expert_id, pill)` puis override
    # `request.model` et `request.extra["disable_thinking"]` AVANT l'appel
    # provider. Si la pill est absente / inconnue / studio → comportement
    # legacy A1+A2 préservé (config.primary_model + config.disable_thinking).
    # Mapping vide par défaut → le champ existe sur tous les ExpertConfig
    # mais reste no-op tant qu'on ne le peuple pas explicitement.
    model_pill_mapping: dict[str, ModelPillConfig] = field(default_factory=dict)

    @property
    def full_chain(self) -> tuple[tuple[str, str], ...]:
        """Chaîne complète primaire + fallbacks, dans l'ordre de priorité."""
        return ((self.primary_provider, self.primary_model), *self.fallback_chain)


# ═══════════════════════════════════════════════════════════════════
# PROMPTS SYSTÈME — identité NEXYA partagée + spécialisation par expert
# ═══════════════════════════════════════════════════════════════════
#
# Principe : chaque prompt commence par l'identité `NEXYA` commune, puis
# spécialise le rôle. On évite le "Tu es une IA Google" — NEXYA parle en son
# propre nom.
# ═══════════════════════════════════════════════════════════════════

# Session A1 (2026-05-19) : l'identité NEXYA (ton + identité fondateur +
# sécurité brand + routing cross-expert) est désormais injectée EN AMONT
# des system_prompts experts par `app/ai/nexya_preamble.py` via le wiring
# dans `_stream_link`. La constante `_NEXYA_IDENTITY` historique est
# conservée vide pour préserver la compatibilité avec les concaténations
# `_NEXYA_IDENTITY + """..."""` ci-dessous (string vide + string = string).
# Les system_prompts experts deviennent ainsi purement métier (rôle +
# spécialité + garde-fou domaine), sans duplication de l'identité brand.
_NEXYA_IDENTITY = ""


# ═══════════════════════════════════════════════════════════════════
# GARDE-FOU DOMAINE — applique à chaque expert spécialisé
# ═══════════════════════════════════════════════════════════════════
#
# Objectif (G2 2026-05-16) : empêcher qu'un expert spécialisé réponde
# à une question hors-domaine (ex: l'expert Cuisine ne doit pas tenter
# de répondre à une question de finance ou de code).
#
# Mécanisme prompt-level (pas de classifier ML — pourquoi :
#   1. Coût zéro additionnel par requête (pas d'appel LLM extra).
#   2. Latence zéro (pas de roundtrip avant le main call).
#   3. Le LLM gère lui-même le routing avec une instruction claire.
#   4. Évite le risque d'un faux-positif classifier qui bloquerait
#      un cas légitime ambigu (ex: « combien coûte un kg de riz ? » →
#      légitime en mode Cuisine ET Finance).
#
# Tolérance volontaire :
#   - Questions méta (« qui es-tu », « que sais-tu faire ») toujours OK.
#   - Questions transverses ambiguës : le modèle décide en premier lieu
#     d'aider, et redirige UNIQUEMENT si la question est manifestement
#     hors-scope (ex: « écris-moi un script Python » en mode Cuisine).
# ═══════════════════════════════════════════════════════════════════

_DOMAIN_GUARDRAIL_TEMPLATE = """
Garde-fou de domaine — {domain_label} :
- Tu es spécialisé en : **{domain_description}**.
- Si l'utilisateur pose une question manifestement hors de ce champ
  (ex: code informatique en mode Cuisine, recette de cuisine en mode
  Finance, traduction en mode Sciences), réponds brièvement et redirige :
  « Cette question relève plutôt du mode {suggested_mode}. Bascule sur
  ce mode pour une réponse spécialisée. »
- Tu peux toujours répondre aux questions méta (« qui es-tu ? »,
  « que sais-tu faire ? », « quels sont tes domaines ? »).
- Pour une question ambiguë ou transverse, aide d'abord puis suggère
  le mode plus adapté en fin de réponse si pertinent.
- Ne fabule jamais une réponse hors-domaine : la précision prime sur
  l'exhaustivité — mieux vaut rediriger que mal répondre.
"""


def _with_guardrail(
    prompt: str,
    *,
    domain_label: str,
    domain_description: str,
    suggested_mode: str = "Général",
) -> str:
    """Concatène un prompt expert avec le garde-fou de domaine.

    Args:
        prompt: prompt expert complet (identité + rôle).
        domain_label: titre court du domaine (« Cuisine », « Finance »…).
        domain_description: 1 phrase qui résume le scope précis.
        suggested_mode: mode vers lequel rediriger les hors-domaine.
            Défaut « Général » (catch-all). Pour `medicine`/`legal` on
            redirige aussi vers Général (jamais vers un autre safety-
            critical sans intention claire).
    """
    guardrail = _DOMAIN_GUARDRAIL_TEMPLATE.format(
        domain_label=domain_label,
        domain_description=domain_description,
        suggested_mode=suggested_mode,
    )
    return prompt + guardrail

# Session A2 (2026-05-19) : les 11 system_prompts experts sont désormais
# définis dans le package `app/ai/expert_prompts/` (un module par expert).
# `experts.py` se contente d'importer + d'appliquer `_with_guardrail` aux
# 9 experts spécialisés (computer/science/finance/language/cooking/
# engineering/productivity/medicine/legal). general et studio n'ont pas
# besoin de guardrail (general = catch-all, studio = image-only avec
# redirection native).

_GENERAL_PROMPT = GENERAL_PROMPT  # general n'a pas de guardrail

_COMPUTER_PROMPT = _with_guardrail(
    COMPUTER_PROMPT,
    domain_label="Informatique",
    domain_description=(
        "code, debug, architecture logicielle, outils dev (Git, Docker, CI), "
        "concepts informatiques"
    ),
)

_SCIENCE_PROMPT = _with_guardrail(
    SCIENCE_PROMPT,
    domain_label="Sciences & Mathématiques",
    domain_description=(
        "maths, physique, chimie, biologie, statistiques, sciences appliquées"
    ),
)

_FINANCE_PROMPT = _with_guardrail(
    FINANCE_PROMPT,
    domain_label="Finance & Business",
    domain_description=(
        "gestion financière personnelle, comptabilité, investissements, "
        "création/stratégie d'entreprise, marketing, contexte OHADA"
    ),
)

_LANGUAGE_PROMPT = _with_guardrail(
    LANGUAGE_PROMPT,
    domain_label="Langues",
    domain_description=(
        "apprentissage, traduction, correction, pratique de langues "
        "(internationales et africaines)"
    ),
)

_COOKING_PROMPT = _with_guardrail(
    COOKING_PROMPT,
    domain_label="Cuisine & Vie Quotidienne",
    domain_description=(
        "recettes, techniques culinaires, substitutions d'ingrédients, "
        "organisation du foyer, astuces du quotidien"
    ),
)

_STUDIO_PROMPT = STUDIO_PROMPT  # studio image-only, pas de guardrail

_ENGINEERING_PROMPT = _with_guardrail(
    ENGINEERING_PROMPT,
    domain_label="Ingénierie",
    domain_description=(
        "génie civil, mécanique, électrique, industriel, chimique, énergies, "
        "télécoms, matériaux, normes ISO/EN/NF"
    ),
)

_PRODUCTIVITY_PROMPT = _with_guardrail(
    PRODUCTIVITY_PROMPT,
    domain_label="Productivité & Vie",
    domain_description=(
        "organisation du temps, prise de décision, routines, gestion de "
        "projets personnels, habitudes (GTD, Eisenhower, OKRs)"
    ),
)

_MEDICINE_PROMPT = _with_guardrail(
    MEDICINE_PROMPT,
    domain_label="Médecine (information)",
    domain_description=(
        "information médicale générale (maladies, médicaments, symptômes), "
        "JAMAIS diagnostic ni prescription"
    ),
)

_LEGAL_PROMPT = _with_guardrail(
    LEGAL_PROMPT,
    domain_label="Légal (information)",
    domain_description=(
        "information juridique générale (droit camerounais, OHADA, références "
        "légales), JAMAIS acte engageant ni conseil personnalisé"
    ),
)


# ═══════════════════════════════════════════════════════════════════
# REGISTRE — les 10 experts + "general" (hors expert)
# ═══════════════════════════════════════════════════════════════════
#
# Choix modèle :
# - Flash (`gemini-2.5-flash`) : défaut — rapide, peu cher (~$0.001/req)
# - Pro   (`gemini-2.5-pro`)   : réflexion profonde — Sciences, Ingénierie,
#                                Médecine (domaines où l'erreur coûte cher)
#
# Chaîne de fallback (même tier côté qualité) :
# - Flash → Pro Gemini (mêmes capacités, plus cher mais marche si Flash crash)
# - Pro Gemini → Flash Gemini (dégradation gracieuse : mieux vaut rapide
#   qu'un 503)
# - Quand OpenAI/Anthropic/Qwen seront branchés, on insèrera un modèle tier
#   équivalent comme second fallback.
# ═══════════════════════════════════════════════════════════════════

_GEMINI_FLASH = ("gemini", "gemini-2.5-flash")
_GEMINI_PRO = ("gemini", "gemini-2.5-pro")

# OpenRouter sert de **second fallback généraliste** sur les experts non
# safety-critical (general, productivity, science). On ne le met PAS sur
# medicine/legal — l'agrégateur peut router vers un modèle communautaire
# dont l'alignement éthique n'a pas été vérifié par NEXYA.
_OPENROUTER_SONNET = ("openrouter", "anthropic/claude-3.5-sonnet")


EXPERT_REGISTRY: dict[str, ExpertConfig] = {
    "general": ExpertConfig(
        expert_id="general",
        display_name="Général",
        is_coming_soon=False,
        primary_provider="gemini",
        primary_model="gemini-2.5-flash",
        fallback_chain=(_GEMINI_PRO, _OPENROUTER_SONNET),
        system_prompt=_GENERAL_PROMPT,
        temperature=0.7,
        # [2026-05-22] Cap relevé 2048→4096 — confort longues réponses.
        # Surcoût réel nul : on facture les tokens générés, pas le plafond
        # (le cap anti-runaway de l'audit 2026-05-01 reste — 4096 ≈ 3000 mots).
        max_tokens=4096,
        tier="flash",
        tags=("general", "conversation"),
        model_pill_mapping=_DEFAULT_PILL_MAPPING,
    ),
    "computer": ExpertConfig(
        expert_id="computer",
        display_name="Expert Informatique",
        is_coming_soon=False,
        primary_provider="gemini",
        primary_model="gemini-2.5-flash",
        fallback_chain=(_GEMINI_PRO,),
        system_prompt=_COMPUTER_PROMPT,
        temperature=0.3,  # code = peu de créativité, beaucoup de rigueur
        # [2026-05-22] Cap relevé 2048→4096 — un gros fichier de code
        # (~300 lignes) ne sera plus tronqué en plein milieu.
        max_tokens=4096,
        tier="flash",
        tags=("code", "technical"),
        model_pill_mapping=_DEFAULT_PILL_MAPPING,
    ),
    "science": ExpertConfig(
        expert_id="science",
        display_name="Expert Sciences & Maths",
        is_coming_soon=False,
        primary_provider="gemini",
        primary_model="gemini-2.5-pro",
        fallback_chain=(_GEMINI_FLASH, _OPENROUTER_SONNET),
        system_prompt=_SCIENCE_PROMPT,
        temperature=0.2,
        # [2026-05-22] Cap relevé 4096→8192 — démonstrations LaTeX longues
        # + calculs détaillés sans troncature en plein milieu.
        max_tokens=8192,
        tier="pro",
        tags=("stem", "reasoning"),
        model_pill_mapping=_DEFAULT_PILL_MAPPING,
    ),
    "finance": ExpertConfig(
        expert_id="finance",
        display_name="Expert Finance & Business",
        is_coming_soon=True,
        primary_provider="gemini",
        primary_model="gemini-2.5-flash",
        fallback_chain=(_GEMINI_PRO,),
        system_prompt=_FINANCE_PROMPT,
        temperature=0.4,
        # [2026-05-22] Cap relevé 2048→4096 (confort longues réponses).
        max_tokens=4096,
        tier="flash",
        tags=("finance", "business", "africa"),
        model_pill_mapping=_DEFAULT_PILL_MAPPING,
    ),
    "language": ExpertConfig(
        expert_id="language",
        display_name="Expert Langues",
        is_coming_soon=False,
        primary_provider="gemini",
        # G1 active `gemini-2.5-pro` pour ancrer la traduction/conjugaison
        # 2026-04-24 : `corpus_enabled` désactivé après blind test G1
        # (13/30 wins RAG vs Gemini brut, échec seuil 24/30). Diagnostic :
        # Gemini 2.5 Pro déjà excellent sur FR/EN/ES/PT, le corpus Tatoeba
        # n'apporte pas de valeur ; les langues vernaculaires camerounaises
        # (Duala, Bassa, Medumba, Fulfulde…) seront couvertes par bloc H
        # (fine-tuning Gemma) où le RAG ne suffit pas. Infra G1 conservée
        # pour G2 Cuisine / G4 Ingénierie / G6 Informatique où le RAG a
        # un sens. Voir CLAUDE.md §15 entrée G1 du 2026-04-24.
        primary_model="gemini-2.5-pro",
        fallback_chain=(_GEMINI_FLASH,),
        system_prompt=_LANGUAGE_PROMPT,
        temperature=0.5,
        # [2026-05-22] Cap relevé 4096→8192 — traductions + conjugaisons
        # + explications culturelles longues sans troncature.
        max_tokens=8192,
        tier="pro",
        tags=("language", "translation"),
        corpus_enabled=False,
        model_pill_mapping=_DEFAULT_PILL_MAPPING,
    ),
    "cooking": ExpertConfig(
        expert_id="cooking",
        display_name="Expert Cuisine & Vie Quotidienne",
        is_coming_soon=False,
        primary_provider="gemini",
        # G2 V1.1 2026-05-18 — Bascule Flash après benchmark latence :
        # - Pro+thinking : TTFT 19,5 s + réponse 1216 chars (thinking
        #   consomme le budget output, réponses tronquées)
        # - Flash+thinking : TTFT 12,2 s + réponse 1480 chars
        # - **Flash sans thinking : TTFT 8,8 s + réponse 6505 chars** ✅
        # Le format recette (titre + ingrédients + étapes numérotées)
        # est du formatage de contenu RAG, pas du raisonnement multi-
        # étapes complexe. Flash sans thinking est objectivement supérieur
        # pour ce use case : 2,2× plus rapide ET 5× plus riche en sortie.
        # Pro reste fallback si Flash échoue. Mesure et décision dans
        # CLAUDE.md §15 entrée 2026-05-18.
        primary_model="gemini-2.5-flash",
        fallback_chain=(_GEMINI_PRO,),
        system_prompt=_COOKING_PROMPT,
        # 0.5 < 0.7 : corpus présent → on veut que le modèle exploite les
        # extraits plutôt que d'inventer une variante créative.
        temperature=0.5,
        # 4096 (vs 2048 flash) : marges pour une recette complète
        # ingrédients + étapes + alternative locale + contexte régional.
        max_tokens=4096,
        tier="pro",
        tags=("cooking", "daily", "rag"),
        # G2 ON — corpus de ~100 recettes camerounaises propriétaires
        # (livres Loth Ivan / Nexyalabs, owner traçé pour AI Act Article 13).
        corpus_enabled=True,
        # [Fix 2026-05-22] `disable_thinking` n'est plus posé explicitement
        # ici — le défaut `ExpertConfig.disable_thinking=True` couvre
        # désormais les 11 experts (cf. commentaire du champ). Cooking était
        # le 1ᵉʳ à en bénéficier (G2 V1.1, latence ~20s → ~3s sur Pro Vertex).
        # G2 V8 preserve : pills GEEK/LOTH/JUSTO gardent disable_thinking=True.
        model_pill_mapping=_COOKING_PILL_MAPPING,
    ),
    # ─── Bientôt disponible ────────────────────────────────────────
    "studio": ExpertConfig(
        expert_id="studio",
        display_name="NEXYA Studio",
        is_coming_soon=True,
        primary_provider="gemini-imagen",
        primary_model="imagen-3.0-generate-002",
        fallback_chain=(),
        system_prompt=_STUDIO_PROMPT,
        temperature=0.0,  # non-applicable pour image, laissé par convention
        # Studio est image-only ; max_tokens posé par cohérence si jamais
        # un fallback texte est introduit (rejet user vers mode Général).
        max_tokens=2048,
        tier="image",
        tags=("image", "creative"),
        # Studio image-only : mapping vide, /image/generate ignore le pill.
        model_pill_mapping=_STUDIO_PILL_MAPPING,
    ),
    "engineering": ExpertConfig(
        expert_id="engineering",
        display_name="Expert Ingénierie",
        is_coming_soon=True,
        primary_provider="gemini",
        primary_model="gemini-2.5-pro",
        fallback_chain=(_GEMINI_FLASH,),
        system_prompt=_ENGINEERING_PROMPT,
        temperature=0.2,
        # [2026-05-22] Cap relevé 4096→8192 — calculs détaillés +
        # trade-offs + normes citées sans troncature.
        max_tokens=8192,
        tier="pro",
        tags=("engineering", "technical"),
        model_pill_mapping=_DEFAULT_PILL_MAPPING,
    ),
    "productivity": ExpertConfig(
        expert_id="productivity",
        display_name="Expert Productivité & Vie",
        is_coming_soon=True,
        primary_provider="gemini",
        primary_model="gemini-2.5-flash",
        fallback_chain=(_GEMINI_PRO, _OPENROUTER_SONNET),
        system_prompt=_PRODUCTIVITY_PROMPT,
        temperature=0.6,
        # [2026-05-22] Cap relevé 2048→4096 (confort longues réponses).
        max_tokens=4096,
        tier="flash",
        tags=("productivity", "habits"),
        model_pill_mapping=_DEFAULT_PILL_MAPPING,
    ),
    "medicine": ExpertConfig(
        expert_id="medicine",
        display_name="Expert Médecine",
        is_coming_soon=True,
        primary_provider="gemini",
        primary_model="gemini-2.5-pro",
        fallback_chain=(_GEMINI_FLASH,),
        system_prompt=_MEDICINE_PROMPT,
        temperature=0.1,  # médecine = zéro créativité
        # [2026-05-22] Cap relevé 3072→4096 — info structurée + disclaimers
        # + redirection urgences (5 symptômes vitaux) sans troncature.
        max_tokens=4096,
        tier="pro",
        disclaimer=(
            "Les informations fournies ne remplacent pas l'avis d'un professionnel "
            "de santé. Consulte un médecin pour tout cas concret."
        ),
        tags=("medical", "safety-critical"),
        # [planner-from-chat LOT 4] — function calling RÉACTIVÉ (était False
        # sous F2.5). Décision produit Ivan : un utilisateur en mode Médecine
        # doit pouvoir poser un rappel depuis le chat (« rappelle-moi de
        # prendre mes médicaments à 8h ») — c'est un cas d'usage légitime,
        # et même PLUS pertinent dans cet expert. Les 4 tools Planner sont
        # bénins (rappels, pas de prescription). Le bloc URGENCE en tête du
        # prompt medicine reste prioritaire ; l'intent classifier (LOT 5) ne
        # force JAMAIS un tool call sur une phrase d'urgence vitale.
        tools_allowed=True,
        # Safety-critical MAX : thinking on sur les 3 pills (un patient mérite
        # la même rigueur clinique en JUSTO express qu'en GEEK approfondi).
        model_pill_mapping=_MEDICINE_PILL_MAPPING,
    ),
    "legal": ExpertConfig(
        expert_id="legal",
        display_name="Expert Légal",
        is_coming_soon=False,
        primary_provider="gemini",
        primary_model="gemini-2.5-pro",
        fallback_chain=(_GEMINI_FLASH,),
        system_prompt=_LEGAL_PROMPT,
        temperature=0.1,  # juridique = zéro créativité
        # [2026-05-22] Cap relevé 3072→4096 — info juridique structurée +
        # références (Code civil, Acte uniforme OHADA) + redirection avocat.
        max_tokens=4096,
        tier="pro",
        disclaimer=(
            "Les informations fournies ne constituent pas un conseil juridique. "
            "Consulte un avocat ou un notaire pour tout cas concret."
        ),
        tags=("legal", "safety-critical", "ohada"),
        # [planner-from-chat LOT 4] — idem `medicine` : function calling
        # RÉACTIVÉ (décision produit Ivan). Un utilisateur en mode Légal
        # doit pouvoir poser un rappel (« rappelle-moi l'échéance du
        # contrat le 30 »). Les 4 tools Planner ne rédigent aucun acte
        # juridique engageant — ils planifient des rappels.
        tools_allowed=True,
        # Safety-critical : thinking on sur GEEK+LOTH (rigueur OHADA),
        # off sur JUSTO (cas factuels simples type « définition SARL »).
        model_pill_mapping=_LEGAL_PILL_MAPPING,
    ),
}


def get_expert_config(expert_id: str | None) -> ExpertConfig:
    """Retourne la config d'un expert. Si `expert_id` est inconnu ou None,
    retombe sur le mode "general" — aucune erreur levée.

    Ce choix permissif est volontaire : on ne bloque jamais un chat à cause
    d'un champ inconnu. Si le frontend envoie un expert_id qu'on ne connaît
    pas (nouveau mode côté Flutter pas encore déployé côté backend), on sert
    le général plutôt que de rendre un 400.
    """
    if not expert_id:
        return EXPERT_REGISTRY["general"]
    return EXPERT_REGISTRY.get(expert_id, EXPERT_REGISTRY["general"])


def all_expert_ids() -> list[str]:
    """Liste tous les `expert_id` enregistrés, general en tête."""
    return list(EXPERT_REGISTRY.keys())
