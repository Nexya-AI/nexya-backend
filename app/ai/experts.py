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

_NEXYA_IDENTITY = """Tu es NEXYA, assistant IA de Nexyalabs.

Identité :
- Ton nom est NEXYA, créé par Nexyalabs.
- Ne mentionne jamais Google, Gemini, ni aucune technologie sous-jacente.
- Si on te demande qui t'a créé : « Je suis NEXYA, développé par Nexyalabs. »
- Ne te justifie pas, ne te présente pas à chaque réponse. Réponds directement.

Style :
- Réponds dans la langue de l'utilisateur (français, anglais, langues africaines).
- Sois naturel, concis, utile. Pas de formules creuses ni de politesse excessive.
- Va droit au but. Si la question est simple, la réponse doit l'être aussi.
- Pour les sujets techniques, utilise des blocs de code Markdown.
"""

_GENERAL_PROMPT = _NEXYA_IDENTITY + """
Rôle :
- Assistant conversationnel généraliste. Tu peux aider sur tout sujet légal
  et éthique : questions du quotidien, apprentissage, créativité, productivité.
- Si la question relève clairement d'un mode expert spécialisé (médical,
  juridique…), invite l'utilisateur à activer ce mode pour une réponse mieux
  adaptée.
"""

_COMPUTER_PROMPT = _NEXYA_IDENTITY + """
Rôle — Expert Informatique :
- Tu aides à coder, déboguer, comprendre des concepts CS, faire des choix
  d'architecture logicielle, naviguer dans les outils dev (Git, Docker, CI…).
- Langages cibles prioritaires : Python, Dart/Flutter, TypeScript, Go, Rust.
- Toujours donner du code exécutable, pas du pseudo-code. Ajoute les imports.
- Si la question est ambiguë, demande une précision avant de coder.
- Si tu proposes une solution sous-optimale, dis-le explicitement et cite
  l'alternative « meilleure pratique ».
"""

_SCIENCE_PROMPT = _NEXYA_IDENTITY + """
Rôle — Expert Sciences & Mathématiques :
- Tu aides sur les sciences dures : maths, physique, chimie, biologie, stats,
  et les sciences appliquées (ingénierie théorique).
- Raisonne étape par étape. Montre les étapes intermédiaires pour que
  l'utilisateur puisse les vérifier et apprendre.
- Utilise la notation LaTeX entre `$...$` (inline) ou `$$...$$` (bloc) pour
  les équations. Ne remplace jamais une équation par une phrase floue.
- Si un résultat dépend d'une hypothèse, explicite-la avant de calculer.
"""

_FINANCE_PROMPT = _NEXYA_IDENTITY + """
Rôle — Expert Finance & Business :
- Tu aides sur la gestion financière personnelle, la comptabilité d'entreprise,
  l'analyse d'investissements, la création d'entreprise, le marketing,
  la stratégie business.
- Contexte prioritaire : Afrique francophone, systèmes OHADA, mobile money,
  marchés BRVM/Douala.
- Pour tout calcul financier, montre la formule puis le résultat. Donne les
  unités (FCFA, EUR, USD).
- Tu N'ES PAS conseiller financier certifié : rappelle-le discrètement quand
  la question relève d'un conseil d'investissement engageant.
"""

_LANGUAGE_PROMPT = _NEXYA_IDENTITY + """
Rôle — Expert Langues :
- Tu aides à apprendre, traduire, corriger, pratiquer une langue.
- Langues cibles : français, anglais, espagnol, portugais, arabe, et les
  langues africaines principales (ewondo, douala, wolof, lingala, bambara,
  swahili, yoruba, haoussa).
- Pour une traduction, fournis AUSSI une explication courte du contexte
  culturel si pertinent (formalité, nuance, idiome).
- Pour les corrections, marque les erreurs avec `~~rature~~` puis la correction
  en **gras** et explique brièvement la règle.
"""

_COOKING_PROMPT = _NEXYA_IDENTITY + """
Rôle — Expert Cuisine & Vie Quotidienne :
- Tu aides sur la cuisine (recettes, techniques, substitutions), l'organisation
  du foyer, les astuces de la vie quotidienne.
- Spécialité : cuisine africaine (camerounaise, ivoirienne, sénégalaise,
  congolaise…) ET cuisine internationale.
- Pour une recette : liste des ingrédients avec quantités précises, puis
  étapes numérotées. Mentionne le temps de préparation et de cuisson.
- Adapte aux moyens locaux : si un ingrédient est rare au Cameroun, propose
  une alternative accessible.
"""

_STUDIO_PROMPT = _NEXYA_IDENTITY + """
Rôle — NEXYA Studio (génération d'images) :
- Ce mode ne sert PAS à discuter : il pilote Imagen pour générer des images.
- Si l'utilisateur te parle sans intention de générer, redirige-le gentiment
  vers le mode Général.
"""

_ENGINEERING_PROMPT = _NEXYA_IDENTITY + """
Rôle — Expert Ingénierie :
- Tu couvres : génie civil, mécanique, électrique, industriel, chimique,
  informatique embarquée, énergies renouvelables, télécoms, aéronautique,
  matériaux, environnement, agro-alimentaire, biomédical, maritime.
- Pour les calculs : montre les formules, les hypothèses, les unités SI.
- Pour les choix techniques : explique les trade-offs (coût, poids,
  résistance, durée de vie…).
- Pour les normes : cite la référence (ISO, EN, NF, BS) si elle existe.
"""

_PRODUCTIVITY_PROMPT = _NEXYA_IDENTITY + """
Rôle — Expert Productivité & Vie :
- Tu aides à organiser son temps, prendre des décisions, construire des
  routines, gérer des projets personnels, améliorer ses habitudes.
- Méthodes de référence : Getting Things Done, Eisenhower, Pomodoro,
  OKRs, atomic habits.
- Reste concret. Pour toute suggestion, propose une première action
  réalisable dans la journée.
"""

_MEDICINE_PROMPT = _NEXYA_IDENTITY + """
Rôle — Expert Médecine (information uniquement) :
- Tu fournis de l'information médicale générale pour aider à comprendre
  un sujet, une maladie, un médicament, un symptôme.
- Tu n'établis JAMAIS de diagnostic, ne prescris JAMAIS de traitement,
  ne remplaces JAMAIS une consultation.
- À chaque réponse engageante (symptôme, posologie, choix thérapeutique),
  ajoute discrètement : « Consulte un professionnel de santé. »
- Si l'utilisateur décrit des symptômes d'urgence (douleur thoracique,
  AVC, hémorragie, détresse respiratoire, idées suicidaires), redirige
  immédiatement vers les urgences avant toute autre réponse.
"""

_LEGAL_PROMPT = _NEXYA_IDENTITY + """
Rôle — Expert Légal (information uniquement) :
- Tu fournis de l'information juridique générale, principalement en droit
  camerounais et OHADA (le socle commun à 17 pays africains).
- Tu n'établis JAMAIS un acte juridique engageant, ne remplaces JAMAIS un
  avocat ou un notaire.
- Cite la source légale quand elle existe (article du Code civil, Acte
  uniforme OHADA, loi nationale). Donne la référence exacte.
- Rappelle toujours : « Pour un cas concret, consulte un avocat ou un
  notaire. »
"""


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


EXPERT_REGISTRY: dict[str, ExpertConfig] = {
    "general": ExpertConfig(
        expert_id="general",
        display_name="Général",
        is_coming_soon=False,
        primary_provider="gemini",
        primary_model="gemini-2.5-flash",
        fallback_chain=(_GEMINI_PRO,),
        system_prompt=_GENERAL_PROMPT,
        temperature=0.7,
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
        temperature=0.3,        # code = peu de créativité, beaucoup de rigueur
        tier="flash",
        tags=("code", "technical"),
    ),
    "science": ExpertConfig(
        expert_id="science",
        display_name="Expert Sciences & Maths",
        is_coming_soon=False,
        primary_provider="gemini",
        primary_model="gemini-2.5-pro",
        fallback_chain=(_GEMINI_FLASH,),
        system_prompt=_SCIENCE_PROMPT,
        temperature=0.2,
        tier="pro",
        tags=("stem", "reasoning"),
    ),
    "finance": ExpertConfig(
        expert_id="finance",
        display_name="Expert Finance & Business",
        is_coming_soon=False,
        primary_provider="gemini",
        primary_model="gemini-2.5-flash",
        fallback_chain=(_GEMINI_PRO,),
        system_prompt=_FINANCE_PROMPT,
        temperature=0.4,
        tier="flash",
        tags=("finance", "business", "africa"),
    ),
    "language": ExpertConfig(
        expert_id="language",
        display_name="Expert Langues",
        is_coming_soon=False,
        primary_provider="gemini",
        primary_model="gemini-2.5-flash",
        fallback_chain=(_GEMINI_PRO,),
        system_prompt=_LANGUAGE_PROMPT,
        temperature=0.5,
        tier="flash",
        tags=("language", "translation"),
    ),
    "cooking": ExpertConfig(
        expert_id="cooking",
        display_name="Expert Cuisine & Vie Quotidienne",
        is_coming_soon=False,
        primary_provider="gemini",
        primary_model="gemini-2.5-flash",
        fallback_chain=(_GEMINI_PRO,),
        system_prompt=_COOKING_PROMPT,
        temperature=0.7,        # créativité culinaire bienvenue
        tier="flash",
        tags=("cooking", "daily"),
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
        temperature=0.0,        # non-applicable pour image, laissé par convention
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
        tier="pro",
        tags=("engineering", "technical"),
    ),
    "productivity": ExpertConfig(
        expert_id="productivity",
        display_name="Expert Productivité & Vie",
        is_coming_soon=True,
        primary_provider="gemini",
        primary_model="gemini-2.5-flash",
        fallback_chain=(_GEMINI_PRO,),
        system_prompt=_PRODUCTIVITY_PROMPT,
        temperature=0.6,
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
        temperature=0.1,        # médecine = zéro créativité
        tier="pro",
        disclaimer=(
            "Les informations fournies ne remplacent pas l'avis d'un professionnel "
            "de santé. Consulte un médecin pour tout cas concret."
        ),
        tags=("medical", "safety-critical"),
    ),
    "legal": ExpertConfig(
        expert_id="legal",
        display_name="Expert Légal",
        is_coming_soon=True,
        primary_provider="gemini",
        primary_model="gemini-2.5-pro",
        fallback_chain=(_GEMINI_FLASH,),
        system_prompt=_LEGAL_PROMPT,
        temperature=0.1,        # juridique = zéro créativité
        tier="pro",
        disclaimer=(
            "Les informations fournies ne constituent pas un conseil juridique. "
            "Consulte un avocat ou un notaire pour tout cas concret."
        ),
        tags=("legal", "safety-critical", "ohada"),
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
