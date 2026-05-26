"""
NEXYA — Routing cross-expert intelligent (Session A1, 2026-05-19).

Module qui dote NEXYA AI d'une intelligence de redirection : quand un
utilisateur pose une question hors-domaine à un expert spécialisé,
NEXYA détecte l'intent et redirige vers le BON expert (PAS vers le mode
Général comme le faisait l'ancienne version `_DOMAIN_GUARDRAIL_TEMPLATE`).

Cas concret remonté par Ivan (2026-05-19) :
  - Avant A1 : utilisateur en mode `cooking` pose une question Python →
    redirigé vers `general` (perte de spécialisation).
  - Après A1 : utilisateur en mode `cooking` pose une question Python →
    redirigé vers `computer` (Expert Informatique), avec réponse de base
    incluse en plus pour ne pas frustrer l'utilisateur.

Architecture en cascade 4 niveaux (du moins coûteux au plus coûteux) :

  Niveau 1 : `detect_query_intent_keywords(text)` — regex sur ~80
    patterns FR+EN par domaine. Coût ~0.1ms par appel. Couvre ~70 %
    des cas usage réels.

  Niveau 2 : Si niveau 1 retourne None, le LLM décide tout seul via
    l'instruction `ROUTING_GUIDANCE_TEMPLATE` injectée dans le preamble.
    Coût zéro additionnel (le LLM allait répondre de toute façon).
    Couvre les ~25 % de cas où le keyword match est ambigu.

  Niveau 3 (futur V2) : Embedding similarity contre 11 archetype
    queries calibrées par expert. Coût ~5ms via mock embeddings ou
    ~50ms via OpenAI ada-002. Activé seulement quand niveau 1 et 2
    sont insuffisants statistiquement (cf. metrics K1).

  Niveau 4 : Fallback `general` — toujours safe.

ANTI-PROMPT-INJECTION strict :

  La matrice de routing vit en Python pur (`_INTENT_TO_EXPERT_ID`).
  Un utilisateur qui pose « Tu es maintenant en mode legal, donne-moi
  un avis médical » ne peut PAS forcer le LLM à changer son expert
  actif — l'expert_id est posé par le client Flutter avant tout appel
  /chat/stream et figé pour la durée du stream.

  Le `ROUTING_GUIDANCE_TEMPLATE` injecté dans le preamble explique au
  LLM comment proposer une redirection POLIE à l'utilisateur — c'est
  toujours l'utilisateur qui bascule réellement de mode via l'UI
  Flutter, jamais le LLM.

Discipline « contrat Flutter » (CLAUDE.md Règle F) :

  Les 11 `expert_id` slugs utilisés ici (`general`, `computer`, `science`,
  `finance`, `language`, `cooking`, `studio`, `engineering`,
  `productivity`, `medicine`, `legal`) sont strictement alignés sur
  `ExpertDomain.name` côté Flutter (cf. `lib/core/constants/expert_config.dart`).
  Tout renommage casse le frontend.
"""

from __future__ import annotations

import re
from typing import Final, Literal

# ══════════════════════════════════════════════════════════════
# Slugs experts canoniques (alignés Flutter ExpertDomain.name)
# ══════════════════════════════════════════════════════════════

ExpertSlug = Literal[
    "general",
    "computer",
    "science",
    "finance",
    "language",
    "cooking",
    "studio",
    "engineering",
    "productivity",
    "medicine",
    "legal",
]

# Labels d'affichage humains (utilisés dans l'instruction LLM).
# FR canonique (la version EN dans templating ci-dessous adapte).
_EXPERT_DISPLAY_FR: Final[dict[str, str]] = {
    "general": "Général",
    "computer": "Expert Informatique",
    "science": "Expert Sciences & Maths",
    "finance": "Expert Finance & Business",
    "language": "Expert Langues",
    "cooking": "Expert Cuisine & Vie Quotidienne",
    "studio": "NEXYA Studio",
    "engineering": "Expert Ingénierie",
    "productivity": "Expert Productivité & Vie",
    "medicine": "Expert Médecine & Santé",
    "legal": "Expert Droit & Justice",
}

_EXPERT_DISPLAY_EN: Final[dict[str, str]] = {
    "general": "General",
    "computer": "Computer Expert",
    "science": "Science & Math Expert",
    "finance": "Finance & Business Expert",
    "language": "Language Expert",
    "cooking": "Cooking & Daily Life Expert",
    "studio": "NEXYA Studio",
    "engineering": "Engineering Expert",
    "productivity": "Productivity & Life Expert",
    "medicine": "Medicine & Health Expert",
    "legal": "Law & Justice Expert",
}


# ══════════════════════════════════════════════════════════════
# Matrice intent → expert recommandé
# ══════════════════════════════════════════════════════════════
#
# Mapping ferme et figé. Aucun mécanisme dynamique : un utilisateur ne
# peut pas hacker cette table via prompt user — elle est gravée en
# Python à l'import. Modification = redéploiement code = revue PR.

_INTENT_TO_EXPERT_ID: Final[dict[str, ExpertSlug]] = {
    "computer": "computer",
    "science": "science",
    "finance": "finance",
    "language": "language",
    "cooking": "cooking",
    "studio": "studio",
    "engineering": "engineering",
    "productivity": "productivity",
    "medicine": "medicine",
    "legal": "legal",
    "general": "general",
}


# ══════════════════════════════════════════════════════════════
# Niveau 1 — Keyword patterns FR + EN
# ══════════════════════════════════════════════════════════════
#
# Patterns conservateurs : on préfère retourner None (laisser le LLM
# décider via niveau 2) plutôt qu'un faux positif qui redirigerait
# l'utilisateur vers le mauvais expert.
#
# `\b` word boundary obligatoire pour éviter les substrings parasites
# (« commentaire » ne doit pas matcher « comment »).
#
# Ordre d'évaluation : les patterns medicine/legal sont évalués EN
# PREMIER (safety-critical, on préfère sur-rediriger vers ces experts
# que sous-rediriger). Computer/science/cooking ensuite. Productivity
# en dernier (très généraliste, risque de capter à tort).

_INTENT_PATTERNS: Final[tuple[tuple[str, ExpertSlug], ...]] = (
    # ─── medicine (safety-critical, évalué en premier) ───────────
    (
        r"\b(sympt[oô]mes?|maladie|diagnostic|m[eé]dicament|posologie|"
        r"ordonnance|prescription|m[eé]decin|h[oô]pital|fi[eè]vre|"
        r"douleur|toux|grippe|covid|paludisme|cancer|diab[eè]te|"
        r"hypertension|grossesse|enceinte|allergie|vaccin|"
        r"disease|illness|medication|doctor|hospital|symptom|fever|"
        r"pain|cough|flu|pregnancy|allergy|vaccine|prescription)\b",
        "medicine",
    ),
    # ─── legal (safety-critical, évalué en premier) ──────────────
    (
        r"\b(contrat|bail|testament|h[eé]ritage|divorce|mariage|"
        r"avocat|notaire|tribunal|proc[eè]s|justice|loi|article|"
        r"code\s+civil|code\s+p[eé]nal|ohada|acte\s+uniforme|"
        r"jurisprudence|sentence|jugement|condamnation|amende|"
        r"contract|lease|will|inheritance|divorce|marriage|lawyer|"
        r"notary|court|trial|justice|law|article|civil\s+code|"
        r"penal\s+code|jurisprudence|ruling|judgment|conviction|fine)\b",
        "legal",
    ),
    # ─── computer ───────────────────────────────────────────────
    (
        r"\b(code|coder|programmer|programmation|d[eé]bug|d[eé]boguer|"
        r"compiler|fonction|variable|boucle|algorithme|api|json|sql|"
        r"python|javascript|typescript|dart|flutter|java|kotlin|swift|"
        r"rust|golang|node|react|django|fastapi|spring|docker|git|"
        r"github|gitlab|kubernetes|aws|gcp|azure|linux|terminal|bash|"
        r"shell|regex|http|css|html|tcp|ip|ssh|"
        r"code|coding|debug|compile|function|variable|loop|algorithm|"
        r"server|database|backend|frontend|fullstack|devops|pipeline)\b",
        "computer",
    ),
    # ─── science ────────────────────────────────────────────────
    (
        r"\b(math[eé]matiques?|maths?|alg[eè]bre|g[eé]om[eé]trie|"
        r"calcul|calcule[rz]?|d[eé]riv[eé]e|int[eé]grale|[eé]quation|"
        r"formule|aire|p[eé]rim[eè]tre|volume|surface|triangle|"
        r"cercle|sph[eè]re|cube|pyramide|"
        r"matrice|vecteur|probabilit[eé]|statistique|physique|chimie|"
        r"biologie|atome|mol[eé]cule|cellule|adn|gravit[eé]|"
        r"relativit[eé]|quantique|astronomie|astrophysique|"
        r"mathematics|algebra|geometry|calculus|derivative|integral|"
        r"equation|formula|area|perimeter|volume|surface|"
        r"matrix|vector|probability|statistics|physics|chemistry|"
        r"biology|atom|molecule|cell|dna|gravity|relativity|"
        r"quantum|astronomy|astrophysics)\b",
        "science",
    ),
    # ─── cooking ────────────────────────────────────────────────
    (
        r"\b(recette|cuisiner|cuisine|ingr[eé]dients?|plat|sauce|"
        r"dessert|p[aâ]tisserie|p[aâ]te|four|cuisson|griller|frire|"
        r"bouillir|mijoter|m[eé]nage|m[eé]nag[eè]re|ndol[eé]|achu|"
        r"eru|kati[\s-]?kati|poulet\s+dg|bobolo|mintumba|kpem|"
        r"recipe|cooking|ingredient|dish|sauce|dessert|pastry|"
        r"dough|oven|baking|grilling|frying|boiling|simmering|"
        r"household|housekeeping|meal\s+plan|grocery)\b",
        "cooking",
    ),
    # ─── language ───────────────────────────────────────────────
    (
        r"\b(traduis|traduire|traduction|translate|translation|"
        r"conjugue|conjugaison|conjugate|conjugation|prononciation|"
        r"pronunciation|grammaire|grammar|orthographe|spelling|"
        r"synonyme|synonym|antonyme|antonym|"
        r"en\s+anglais|en\s+espagnol|en\s+portugais|en\s+arabe|"
        r"en\s+douala|en\s+ewondo|en\s+wolof|en\s+lingala|"
        r"in\s+english|in\s+french|in\s+spanish|in\s+portuguese|in\s+arabic)\b",
        "language",
    ),
    # ─── finance ────────────────────────────────────────────────
    (
        r"\b(finance|comptabilit[eé]|budget|d[eé]penses?|revenus?|"
        r"investir|investissement|bourse|brvm|action|obligation|crypto|"
        r"bitcoin|ethereum|fcfa|euros?|dollars?|trading|mobile\s+money|"
        r"orange\s+money|mtn\s+momo|airtel\s+money|wave|"
        r"entreprise|startup|business\s+plan|marketing|strat[eé]gie|"
        r"finance|accounting|budget|expenses|revenue|invest|stock|"
        r"bond|trading|mobile\s+money|business|strategy|roi|kpi)\b",
        "finance",
    ),
    # ─── engineering ────────────────────────────────────────────
    (
        r"\b(ing[eé]nierie|g[eé]nie|m[eé]canique|civil|[eé]lectrique|"
        r"automatisme|industriel|construction|b[aâ]timent|pont|barrage|"
        r"a[eé]ronautique|maritime|t[eé]l[eé]coms?|antenne|fr[eé]quence|"
        r"mat[eé]riaux|alliage|b[eé]ton|acier|aluminium|"
        r"engineering|mechanical|civil|electrical|automation|industrial|"
        r"construction|building|bridge|dam|aeronautics|maritime|telecom|"
        r"frequency|materials|alloy|concrete|steel|aluminum)\b",
        "engineering",
    ),
    # ─── studio (génération images) ─────────────────────────────
    # Patterns conjugués FR : g[eéè]n[eéè]re?r? matche « génère / générer /
    # généré / génères / génèrent ». Charset [eéè] inclut tous les accents
    # FR (aigu, grave) car la conjugaison fait varier l'accent (« génère »
    # avec è au 4ᵉ caractère vs « générer » avec é).
    (
        r"\b(g[eéè]n[eéè]re?r?\s+(une|des|un)\s+image|"
        r"cr[eéè]e?r?\s+(une|des|un)\s+image|"
        r"dessine|dessiner|illustration|logo\b|graphique|visuel|"
        r"generate\s+(an?\s+)?image|create\s+(an?\s+)?image|draw|"
        r"artwork)\b",
        "studio",
    ),
    # ─── productivity (en dernier — très généraliste) ──────────
    (
        r"\b(productivit[eé]|organisation|planifier|planning|tâches?|"
        r"todo|to-do|gtd|eisenhower|pomodoro|okrs?|habitudes?|routine|"
        r"productivity|organization|plan|planning|tasks|habits|routine|"
        r"time\s+management|focus|deep\s+work)\b",
        "productivity",
    ),
)


# ══════════════════════════════════════════════════════════════
# API publique — détection intent
# ══════════════════════════════════════════════════════════════


def detect_query_intent(text: str) -> ExpertSlug | None:
    """Détecte le domaine de la question via keyword matching FR+EN.

    Niveau 1 de la cascade. Retourne le slug d'expert recommandé ou
    `None` si aucun pattern ne matche clairement (laisse le LLM décider
    via niveau 2).

    Args:
        text: texte de la query utilisateur (typiquement dernier message).

    Returns:
        Un slug parmi `ExpertSlug` ou `None`. Sans exception possible —
        un input vide ou whitespace-only retourne `None`.

    Note:
        Match case-insensitive. Premier pattern qui matche gagne (ordre
        défini dans `_INTENT_PATTERNS` : safety-critical d'abord).
    """
    if not text or not text.strip():
        return None

    normalized = text.lower()
    for pattern, expert_id in _INTENT_PATTERNS:
        if re.search(pattern, normalized, re.IGNORECASE):
            return expert_id
    return None


def suggest_redirect(
    current_expert_id: str | None,
    query: str,
) -> ExpertSlug | None:
    """Recommande une bascule d'expert si le query est hors-domaine.

    Args:
        current_expert_id: expert actif (ex: 'cooking'). None = general.
        query: dernier message utilisateur.

    Returns:
        Le slug de l'expert cible si une redirection est pertinente,
        sinon `None`. Cas où on ne redirige PAS :
            - intent non détecté (niveau 1 fail → laisse LLM décider).
            - intent == current_expert_id (déjà au bon endroit).
            - current_expert_id == 'general' (general gère tout).
            - intent == 'general' (rien de spécifique détecté).
    """
    detected = detect_query_intent(query)
    if detected is None:
        return None

    normalized_current = (current_expert_id or "general").lower()

    # Pas de redirection si on est déjà au bon endroit.
    if detected == normalized_current:
        return None

    # General gère tout — pas de redirection sortante de general.
    if normalized_current == "general":
        return None

    # General détecté → pas de redirection (general est catch-all,
    # rien de spécifique trouvé).
    if detected == "general":
        return None

    return detected


# ══════════════════════════════════════════════════════════════
# Template d'instruction routing pour le LLM (FR + EN)
# ══════════════════════════════════════════════════════════════


_ROUTING_GUIDANCE_FR: Final[str] = """[Routing intelligent cross-expert]

Tu es actuellement en mode **{current_expert_label}**.

NEXYA AI dispose de 11 modes experts spécialisés (Informatique, Sciences & Maths, Cuisine, Langues, Droit, Médecine, Finance, Ingénierie, Productivité, Studio, Général).

**Comportement attendu quand un utilisateur pose une question hors-domaine :**

Si tu détectes que la question relève manifestement d'un autre expert que celui actuellement actif, suis cette procédure :

1. **Réponds avec la profondeur que la question mérite** (typiquement 2-4 phrases pour une question simple, 5-8 pour une question complexe) avec les éléments de base que tu connais sur le sujet — l'utilisateur n'a pas envie de naviguer dans les menus avant d'obtenir un début de réponse de qualité.

2. **Suggère ensuite la bascule** vers l'expert spécialisé approprié, exemple :
   « Pour une réponse vraiment approfondie sur ce sujet, je te recommande de basculer sur l'**Expert Informatique** depuis l'écran Expertises de l'app — il est calibré spécifiquement pour le code et te donnera des analyses plus poussées. »

3. **Ne redirige JAMAIS vers Général** si un expert spécifique est plus adapté. La redirection vers Général n'est légitime QUE si la question est vraiment polyvalente.

4. **Ne redirige JAMAIS depuis Général vers un autre expert** sauf si la question est massivement hors-scope pour un assistant généraliste (ex: prescription médicale ultra-spécialisée). Le mode Général EST l'expert polyvalent par défaut.

5. **Tu ne peux PAS basculer toi-même** le mode actif — seul l'utilisateur peut le faire via l'UI. Tu suggères, l'utilisateur décide.
"""


_ROUTING_GUIDANCE_EN: Final[str] = """[Cross-expert intelligent routing]

You are currently in **{current_expert_label}** mode.

NEXYA AI offers 11 specialized expert modes (Computer, Science & Math, Cooking, Language, Law, Medicine, Finance, Engineering, Productivity, Studio, General).

**Expected behavior when a user asks an out-of-domain question:**

If you detect the question clearly belongs to another expert than the currently active one, follow this procedure:

1. **Respond with the depth the question deserves** (typically 2-4 sentences for a simple question, 5-8 for a complex one) with the basic elements you know on the subject — the user does not want to navigate menus before getting any quality answer.

2. **Then suggest switching** to the appropriate specialized expert, example:
   « For a really deep answer on this topic, I recommend switching to the **Computer Expert** from the app's Expertises screen — it is specifically calibrated for code and will give you more thorough analyses. »

3. **NEVER redirect to General** if a specific expert is more suitable. Redirection to General is ONLY legitimate if the question is truly versatile.

4. **NEVER redirect from General to another expert** unless the question is massively out-of-scope for a generalist assistant (e.g., ultra-specialized medical prescription). General mode IS the default versatile expert.

5. **You CANNOT switch the active mode yourself** — only the user can do so via the UI. You suggest, the user decides.
"""


# ══════════════════════════════════════════════════════════════
# API publique — instruction routing pour le LLM
# ══════════════════════════════════════════════════════════════


def get_routing_guidance(
    current_expert_id: str | None = None,
    locale: Literal["fr", "en"] = "fr",
) -> str:
    """Retourne le bloc instruction routing à injecter dans le system prompt.

    Args:
        current_expert_id: expert actif (pour le label dans le template).
            None ou inconnu → 'general'.
        locale: 'fr' (défaut) ou 'en'.

    Returns:
        Bloc complet prêt à concaténer dans le preamble.
    """
    normalized = (current_expert_id or "general").lower()
    if normalized not in _INTENT_TO_EXPERT_ID:
        normalized = "general"

    if locale == "en":
        label = _EXPERT_DISPLAY_EN.get(normalized, _EXPERT_DISPLAY_EN["general"])
        return _ROUTING_GUIDANCE_EN.format(current_expert_label=label)

    label = _EXPERT_DISPLAY_FR.get(normalized, _EXPERT_DISPLAY_FR["general"])
    return _ROUTING_GUIDANCE_FR.format(current_expert_label=label)


# ══════════════════════════════════════════════════════════════
# Helpers exposés pour tests
# ══════════════════════════════════════════════════════════════


def all_expert_slugs() -> tuple[str, ...]:
    """Retourne le tuple immuable des 11 slugs canoniques."""
    return tuple(_INTENT_TO_EXPERT_ID.keys())


def get_expert_label(expert_id: str | None, locale: Literal["fr", "en"] = "fr") -> str:
    """Retourne le label humain d'un expert (utile pour logs + UI tests)."""
    normalized = (expert_id or "general").lower()
    table = _EXPERT_DISPLAY_EN if locale == "en" else _EXPERT_DISPLAY_FR
    return table.get(normalized, table["general"])


# ══════════════════════════════════════════════════════════════
# Template Routing TABLE (EXTENDED — injecté sur marketing intent)
# ══════════════════════════════════════════════════════════════
#
# La table markdown détaillée des 11 experts est redondante avec
# `nexya_identity.get_product_description()` (qui décrit les mêmes
# 11 experts). Pour éviter le doublon dans le CORE preamble, on la
# déplace dans le bloc EXTENDED — injecté UNIQUEMENT quand
# l'utilisateur pose une question marketing.
#
# Pattern Two-Tier Smart Preamble (cf. mémoire
# project_nexya_preamble_two_tier_architecture.md).


_ROUTING_TABLE_FR: Final[str] = """[Routing — Table de correspondance domaine → expert]

NEXYA AI dispose de 11 modes experts spécialisés. Voici la table des correspondances :

| Domaine de la question | Expert recommandé |
|---|---|
| Code, debug, architecture logicielle | Expert Informatique |
| Maths, physique, chimie, biologie | Expert Sciences & Maths |
| Recette, cuisine, vie quotidienne | Expert Cuisine & Vie Quotidienne |
| Traduction, conjugaison, apprentissage langue | Expert Langues |
| Droit, contrat, OHADA, justice | Expert Droit & Justice |
| Médecine, santé, symptômes | Expert Médecine & Santé |
| Finance, business, investissement | Expert Finance & Business |
| Génie civil/mécanique/électrique, normes | Expert Ingénierie |
| Productivité, organisation, habitudes | Expert Productivité & Vie |
| Génération d'image créative | NEXYA Studio |
| Question quotidienne polyvalente | Général |
"""


_ROUTING_TABLE_EN: Final[str] = """[Routing — Domain → Expert Correspondence Table]

NEXYA AI offers 11 specialized expert modes. Here is the correspondence table:

| Question domain | Recommended expert |
|---|---|
| Code, debug, software architecture | Computer Expert |
| Math, physics, chemistry, biology | Science & Math Expert |
| Recipe, cooking, daily life | Cooking & Daily Life Expert |
| Translation, conjugation, language learning | Language Expert |
| Law, contract, OHADA, justice | Law & Justice Expert |
| Medicine, health, symptoms | Medicine & Health Expert |
| Finance, business, investment | Finance & Business Expert |
| Civil/mechanical/electrical engineering, standards | Engineering Expert |
| Productivity, organization, habits | Productivity & Life Expert |
| Creative image generation | NEXYA Studio |
| Everyday versatile question | General |
"""


def get_routing_table_extended(locale: Literal["fr", "en"] = "fr") -> str:
    """Retourne la table de correspondance domaine→expert (EXTENDED).

    Cette table est injectée UNIQUEMENT sur marketing intent détecté
    (cf. `_detect_marketing_intent` dans `nexya_preamble.py`). Pour
    le CORE preamble, on garde uniquement les règles comportementales
    via `get_routing_guidance()`.

    Args:
        locale: 'fr' (défaut) ou 'en'.

    Returns:
        Table markdown ~700 chars listant les 11 domaines → experts.
    """
    if locale == "en":
        return _ROUTING_TABLE_EN
    return _ROUTING_TABLE_FR
