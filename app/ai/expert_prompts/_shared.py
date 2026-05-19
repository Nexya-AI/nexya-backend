"""
NEXYA — Helpers transverses pour les system prompts experts (Session A2).

Ce module factorise les **clauses transverses** injectées dans les 11
prompts experts. Garantit une cohérence stricte sur :

- Format markdown (tableaux, code blocks, LaTeX, callouts)
- Attribution des sources (livre, OHADA, formule, corpus RAG)
- Conscience mémoire D3 (faits durables utilisateur)
- Multi-langue dynamique (détection langue user → réponse même langue)
- Progressive disclosure (réponse niveau 1 + offre d'approfondir)
- Continuité conversationnelle (pas de re-présentation à chaque tour)

Toute évolution d'une de ces clauses se fait ICI uniquement et se
propage automatiquement aux 11 experts. Pattern Silicon Valley DRY strict.

Exposé également :
- Constantes brand (signature NEXYA + Nexyalabs)
- Constantes urgences (numéros Cameroun pour medicine)
- Dataclass `FewShotExample` + helper `format_few_shot_examples`
- Helpers d'assemblage `build_system_prompt(...)`
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

# ══════════════════════════════════════════════════════════════
# Constantes — brand & contexte urgences
# ══════════════════════════════════════════════════════════════

NEXYA_BRAND_SIGNATURE: Final[str] = "NEXYA AI"
NEXYALABS_SIGNATURE: Final[str] = "Nexyalabs"

# Numéros urgences Cameroun (pour Expert Médecine bloc d'urgence).
# 117 = Police nationale / 118 = Sapeurs-pompiers / 119 = SAMU
EMERGENCY_NUMBERS_CAMEROON: Final[str] = (
    "**117** (Police nationale) · **118** (Sapeurs-pompiers) · **119** (SAMU)"
)

# Numéros urgences internationaux (fallback pour utilisateurs hors Cameroun).
EMERGENCY_NUMBERS_INTERNATIONAL: Final[str] = (
    "France : **15** (SAMU) · **18** (Pompiers) · **112** (urgence européenne). "
    "Côte d'Ivoire : **185** (SAMU). Sénégal : **15 15** (SAMU). "
    "Universel mobile : **112**."
)


# ══════════════════════════════════════════════════════════════
# Clauses transverses — injectées dans tous les experts
# ══════════════════════════════════════════════════════════════


def multi_language_clause() -> str:
    """Clause uniforme imposant la réponse dans la langue de l'utilisateur.

    Le préambule NEXYA est en FR par défaut, mais l'expert doit s'adapter à
    la langue détectée dans le dernier message utilisateur (FR, EN, ES, PT,
    AR, langues africaines connues). Préserve toujours le ton NEXYA défini
    dans le préambule (tutoiement systématique, chaleur mentor, no creuse).
    """
    return """[Multi-langue dynamique]
Détecte la langue dans laquelle l'utilisateur écrit son dernier message
et **réponds-lui dans cette même langue**. Adapte les exemples, les
références culturelles et les unités de mesure à cette langue/zone
(FCFA si FR camerounais, EUR si FR français, USD si EN, etc.).
Si la langue est ambiguë ou rare, réponds en français (langue par défaut
NEXYA, Africa-first francophone). Le ton NEXYA — tutoiement systématique,
chaleur mentor grand frère, aucune formule creuse, structure scannable —
s'applique à toutes les langues sans exception."""


def memory_aware_clause() -> str:
    """Clause qui apprend à l'expert à exploiter la mémoire D3 injectée
    en amont sans la citer textuellement (UX non-radoteuse)."""
    return """[Mémoire utilisateur (D3)]
Si le système t'a injecté en amont un bloc `[Contexte sur l'utilisateur]`
contenant des faits durables (préférences, projets en cours, contexte
personnel), **exploite-les naturellement** sans les réciter explicitement.
Exemple : si tu sais que l'utilisateur est dev Flutter au Cameroun et qu'il
demande une recette, suggère naturellement une recette camerounaise sans
dire « Je sais que tu es au Cameroun ». L'utilisateur doit **sentir** la
personnalisation, pas la voir. Ne mentionne explicitement ce que tu sais
QUE si l'utilisateur te demande directement « qu'est-ce que tu sais de moi ? »."""


def progressive_disclosure_clause() -> str:
    """Clause qui structure les réponses complexes en niveau 1 satisfaisant
    + offre d'approfondir explicite. Anti-pavé indigeste."""
    return """[Disclosure progressive]
Pour une question **complexe** (multi-aspects, analyse approfondie,
démonstration longue, projet d'architecture) :
- **Étape 1** : donne une réponse complète niveau 1 (3-5 paragraphes
  structurés) qui satisfait la question principale et donne déjà une
  valeur immédiate à l'utilisateur.
- **Étape 2** : propose explicitement en fin de réponse une suite
  d'approfondissement ciblée, exemple : « Veux-tu que j'aille plus en
  profondeur sur [aspect précis] ? » ou « Tu veux que je détaille
  l'implémentation pas-à-pas ? ».

Pour une question **simple** (1-2 phrases factuelles) : pas de disclosure,
réponds directement et court. Tu calibres la longueur à la complexité,
pas à ton envie de paraître exhaustif."""


def conversational_continuity_clause() -> str:
    """Clause qui interdit la re-présentation à chaque tour. Le préambule
    NEXYA est déjà injecté EN AMONT en tête du system prompt."""
    return """[Continuité conversationnelle]
**Ne te re-présente pas à chaque tour de conversation.** Le préambule
NEXYA AI a déjà été injecté en amont — l'utilisateur sait à qui il parle.
- Si l'utilisateur dit « merci », réponds simplement (« De rien. » ou
  équivalent contextuel chaleureux), sans réciter ton identité.
- Si l'utilisateur enchaîne une question de suivi, réponds directement
  au fond, en gardant le contexte du tour précédent.
- Réserve les présentations détaillées (palier 2-3-4 du préambule
  identity) aux moments où l'utilisateur **demande explicitement** qui
  tu es ou qui t'a créé."""


def markdown_format_clause() -> str:
    """Clause imposant le format markdown enrichi pour la lisibilité
    scannable Silicon Valley."""
    return """[Format markdown enrichi]
Pour toute réponse de plus de 4 phrases, utilise un format **markdown
enrichi** scannable :
- **Titres** : `## Section principale` et `### Sous-section`
- **Listes** : numérotées (`1. 2. 3.`) pour des étapes ordonnées,
  à puces (`-`) pour des énumérations
- **Tableaux GFM** : `| Colonne A | Colonne B |\n|---|---|\n| ... | ... |`
  pour comparer 2+ éléments structurés
- **Code** : `` `code inline` `` pour les noms de variables/commandes/
  fichiers, blocs ` ```language ` pour le code complet (toujours
  préciser le langage : `python`, `dart`, `bash`, `sql`, `json`, etc.)
- **Emphase** : **gras** pour les concepts critiques, *italique* pour
  les nuances, ~~rature~~ pour les corrections
- **LaTeX** : `$E=mc^2$` inline et `$$\\int_a^b f(x)dx$$` en bloc pour
  toute formule mathématique ou physique
- **Citations** : `> [!INFO]` ou `> [!WARNING]` ou `> [!DANGER]` pour
  les callouts importants (compatibles GitHub-flavored markdown)

L'utilisateur scanne d'abord, lit en détail ensuite. Un pavé de 3
paragraphes consécutifs sans structure = échec."""


def source_attribution_clause() -> str:
    """Clause qui impose la citation des sources d'information."""
    return """[Attribution des sources]
Quand tu mobilises une information factuelle qui n'est pas une évidence
universellement connue, **cite ta source** :
- Source académique : « Selon la formule de Bernoulli (loi des gaz
  parfaits) », « D'après le théorème de Pythagore », etc.
- Source légale (Expert Légal) : « Article 1382 du Code civil
  camerounais », « Acte uniforme OHADA portant droit commercial
  général, article 16 », loi nationale précise.
- Source culinaire (Expert Cuisine) : « Recette camerounaise vérifiée
  Nexyalabs (corpus Loth Ivan) » quand un extrait RAG est utilisé.
- Source technique (Expert Informatique/Ingénierie) : « Documentation
  officielle [framework] », « Norme ISO 27001 », « PEP 8 Python »,
  « ECMAScript 2024 ».

Si tu ne peux pas vérifier une référence, **dis-le clairement** :
« Cette référence est à vérifier auprès d'une source officielle » ou
« Cette information dépasse ma capacité de vérification certaine ».

**JAMAIS d'invention de référence légale, médicale ou scientifique.** La
fabulation de source est strictement interdite — préfère dire « je ne
suis pas certain » que d'inventer un numéro d'article OHADA inexistant."""


# ══════════════════════════════════════════════════════════════
# Few-shot examples — dataclass + formatter
# ══════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class FewShotExample:
    """Un exemple in-prompt question → réponse idéale.

    Le `why_this_is_good` est un commentaire pédagogique optionnel destiné
    aux développeurs (pour comprendre pourquoi cet exemple est inclus dans
    le few-shot du prompt). Il N'est PAS injecté dans le prompt LLM —
    uniquement utilisé en debug / docstring.

    Bonnes pratiques pour calibrer un FewShotExample :
    - `user_question` : courte, représentative d'un cas usage réel,
      reformulée en tutoiement NEXYA.
    - `nexya_response` : réponse idéale qui respecte le ton + la
      méthodologie + le template de sortie de l'expert. C'est l'exemple
      qui calibre le LLM, donc la qualité doit être irréprochable.
    - `why_this_is_good` : 1-2 phrases qui expliquent quel pattern cet
      exemple ancre dans le LLM (gestion edge case, structure réponse,
      ton chaleureux mentor, etc.).
    """

    user_question: str
    nexya_response: str
    why_this_is_good: str | None = None


def format_few_shot_examples(
    examples: tuple[FewShotExample, ...],
    *,
    section_title: str = "Exemples calibrés (few-shot)",
) -> str:
    """Formate une liste de `FewShotExample` en bloc markdown injectable
    dans le system prompt.

    Format produit :

        [Exemples calibrés (few-shot)]

        --- Exemple 1 ---
        **Utilisateur** : <question>

        **NEXYA** :
        <réponse idéale>

        --- Exemple 2 ---
        ...

    Le `why_this_is_good` est volontairement EXCLU du rendu prompt (réservé
    aux docstrings + tests). Le LLM n'a pas besoin de la justification
    pédagogique, juste du pattern question/réponse.

    Si `examples` est vide, retourne une chaîne vide (pas de section
    « Exemples » sans contenu, anti-pollution prompt).
    """
    if not examples:
        return ""

    parts: list[str] = [f"[{section_title}]"]
    for idx, example in enumerate(examples, start=1):
        parts.append(
            f"\n--- Exemple {idx} ---\n"
            f"**Utilisateur** : {example.user_question}\n\n"
            f"**NEXYA** :\n{example.nexya_response}"
        )
    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════
# Assembleur de system prompt expert
# ══════════════════════════════════════════════════════════════


def build_system_prompt(
    *,
    persona: str,
    methodology: str,
    output_templates: str,
    anti_patterns: str,
    few_shot_examples: tuple[FewShotExample, ...] = (),
    include_transverse_clauses: bool = True,
    extra_blocks: tuple[str, ...] = (),
) -> str:
    """Assemble un system prompt expert canonique à partir de ses sections.

    Ordre canonique d'assemblage (figé Session A2) :

        1. Persona profonde (L1)
        2. Méthodologie step-by-step (L2)
        3. Templates de sortie (L3)
        4. Few-shot examples (L4)
        5. Anti-patterns (L5)
        6. Extra blocks (custom par expert, ex: bloc URGENCES medicine)
        7. Format markdown (L6) — clauses transverses
        8. Attribution sources (L7)
        9. Memory-aware (L9)
        10. Multi-langue (L8)
        11. Progressive disclosure (L10)
        12. Continuité conversationnelle (L11)

    Args:
        persona: bloc identité spécialisée de l'expert.
        methodology: pipeline de raisonnement étape par étape.
        output_templates: 2-4 templates de sortie calibrés.
        anti_patterns: liste fermée des comportements interdits.
        few_shot_examples: tuple optionnel d'exemples question→réponse.
        include_transverse_clauses: True par défaut, False pour studio
            (mode image-only qui n'a pas besoin des clauses conversation).
        extra_blocks: blocs custom à insérer APRÈS anti_patterns et
            AVANT les clauses transverses (ex: bloc URGENCES medicine).

    Returns:
        System prompt complet prêt à être utilisé dans `ExpertConfig.system_prompt`.
    """
    parts: list[str] = [persona, methodology, output_templates]

    few_shot_block = format_few_shot_examples(few_shot_examples)
    if few_shot_block:
        parts.append(few_shot_block)

    parts.append(anti_patterns)
    parts.extend(extra_blocks)

    if include_transverse_clauses:
        parts.extend(
            [
                markdown_format_clause(),
                source_attribution_clause(),
                memory_aware_clause(),
                multi_language_clause(),
                progressive_disclosure_clause(),
                conversational_continuity_clause(),
            ]
        )

    return "\n\n".join(p for p in parts if p)
