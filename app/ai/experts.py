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

Mapping frontend ↔ backend :
- Les `expert_id` correspondent EXACTEMENT à `ExpertDomain.name` côté Flutter
  (voir `expert_config.dart`). Tout renommage côté backend casse le contrat.

Liste des 10 experts :
- Actifs       : computer, science, finance, language, cooking
- Bientôt      : studio, engineering, productivity, medicine, legal
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
    # lifespan). Désactivé pour `medical` et `legal` parce qu'un expert
    # médical / juridique ne devrait pas créer une tâche planifiée
    # depuis une consultation — risque de confusion fonctionnelle (le
    # user attend un avis, pas un side-effect DB silencieux).
    tools_allowed: bool = True

    # G2 V1.1 2026-05-18 — Désactivation du thinking mode Gemini 2.5 Pro/Flash
    # pour les experts où le raisonnement multi-étapes n'apporte pas de
    # valeur ajoutée (cooking : recette = formatage de contenu RAG, pas
    # de raisonnement complexe). Réduit la latence first-token de ~20s à
    # ~3s sur Gemini 2.5 Pro Vertex AI. False par défaut — conservé pour
    # science/medicine/legal/engineering où le raisonnement profond aide.
    # Propagé via `request.extra["disable_thinking"]` au provider Gemini.
    disable_thinking: bool = False

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
        # Cap anti-runaway facture (audit 2026-05-01 finding S1).
        # 2048 tokens ≈ 5 pages — couvre largement une réponse conversationnelle.
        max_tokens=2048,
        tier="flash",
        tags=("general", "conversation"),
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
        # 2048 tokens ≈ ~150 lignes de code — suffisant pour un module
        # autonome ; au-delà l'user devrait découper sa demande.
        max_tokens=2048,
        tier="flash",
        tags=("code", "technical"),
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
        # Tier pro = raisonnement multi-étapes (LaTeX, démonstrations,
        # calculs détaillés). 4096 couvre une preuve complète.
        max_tokens=4096,
        tier="pro",
        tags=("stem", "reasoning"),
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
        max_tokens=2048,
        tier="flash",
        tags=("finance", "business", "africa"),
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
        # Tier pro = traduction, conjugaisons, explications culturelles
        # peuvent demander plusieurs paragraphes.
        max_tokens=4096,
        tier="pro",
        tags=("language", "translation"),
        corpus_enabled=False,
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
        # G2 V1.1 — Thinking désactivé (latence ~20s -> ~3s sur Pro Vertex,
        # qualité préservée car format recette = formatage de contenu RAG
        # pas de raisonnement multi-étapes complexe). Mesure et décision
        # documentées dans CLAUDE.md §15 entrée 2026-05-18.
        disable_thinking=True,
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
        # Tier pro = calculs détaillés + trade-offs + normes citées.
        max_tokens=4096,
        tier="pro",
        tags=("engineering", "technical"),
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
        max_tokens=2048,
        tier="flash",
        tags=("productivity", "habits"),
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
        # Safety-critical : info structurée + disclaimers + redirection
        # urgences. 3072 cap entre flash (2048) et pro standard (4096) —
        # pas de génération créative justifiée au-delà.
        max_tokens=3072,
        tier="pro",
        disclaimer=(
            "Les informations fournies ne remplacent pas l'avis d'un professionnel "
            "de santé. Consulte un médecin pour tout cas concret."
        ),
        tags=("medical", "safety-critical"),
        # F2.5 — pas de function calling sur safety-critical : un expert
        # médical ne doit pas créer une tâche planifiée silencieusement
        # depuis une consultation (l'user attend un avis, pas un effet
        # de bord DB).
        tools_allowed=False,
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
        # Safety-critical : info juridique structurée + références (Code
        # civil, Acte uniforme OHADA) + redirection avocat. 3072 idem
        # `medicine` — pas de génération créative au-delà.
        max_tokens=3072,
        tier="pro",
        disclaimer=(
            "Les informations fournies ne constituent pas un conseil juridique. "
            "Consulte un avocat ou un notaire pour tout cas concret."
        ),
        tags=("legal", "safety-critical", "ohada"),
        # F2.5 — idem `medicine` : aucun tool LLM autorisé sur le mode légal.
        tools_allowed=False,
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
