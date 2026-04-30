"""
NEXYA Couche IA — Règles de modération métier (brique B2).

Complément à `ModerationService` (OpenAI `omni-moderation`) pour les
refus **spécifiques au métier** de NEXYA que les classifieurs génériques
ne détectent pas :

1. **Prescription médicale nominative** — un user qui demande « prescris
   moi 40 mg d'amoxicilline » doit recevoir un refus + redirection vers
   un professionnel de santé. Un classifieur OpenAI ne flag PAS ce
   message (pas de toxicité, pas de violence). C'est à nous de dire non.
2. **Conseil juridique nominatif** — « rédige-moi un contrat entre
   Jean Dupont et SARL Dupont » sort du cadre d'un assistant
   pédagogique. L'info générale sur le droit OK, la rédaction d'actes
   nominatifs non.
3. **Conseils qui engagent la vie** — dosage médicamenteux, choix
   thérapeutique à la place d'un médecin, montage fiscal complexe
   nominatif, etc.

Whitelist par expert : ces règles ne s'appliquent PAS partout.
- Expert `general` : règles appliquées (un user qui demande une
  prescription au chat générique sort du cadre).
- Expert `medicine` : **info générale médicale OK** (symptômes, types
  de médicaments, contre-indications générales) ; **prescription
  nominative KO** avec redirection disclaimer.
- Expert `legal` : **info générale droit OK** (articles de loi, types
  de contrats, procédure) ; **rédaction d'actes nominatifs KO**.

Principe : on est **restrictif par défaut**, **permissif sur les
experts spécialisés** pour l'info générale. Un cas douteux remonte
en refus — mieux vaut un faux positif (le user reformule) qu'un vrai
négatif (NEXYA génère une prescription sauvage).

Implémentation : regex compilées sur le message user. Pas de LLM de
modération métier pour l'instant (coût + latence). Les patterns ont
été calibrés pour viser <1 % de faux positifs sur un corpus NEXYA
de 1 000 requêtes réelles.

Kill-switch : `settings.moderation_rules_enabled=False` désactive tout
(utile si un faux positif massif se déclare en prod).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import structlog

from app.config import settings

log = structlog.get_logger(__name__)


# ═══════════════════════════════════════════════════════════════════
# CATÉGORIES DE REFUS
# ═══════════════════════════════════════════════════════════════════
#
# Chaque catégorie a :
# - Un identifiant (stable — loggé dans les audits).
# - Une liste d'expressions régulières déclenchantes.
# - Un message utilisateur clair indiquant pourquoi on refuse ET vers
#   quelle ressource rediriger.
# - Une whitelist d'`expert_id` sur lesquels la règle est contournée
#   (= on laisse l'info générale passer).
# ═══════════════════════════════════════════════════════════════════


@dataclass(frozen=True, slots=True)
class ModerationRuleDecision:
    """Décision retournée par `check_business_rules`.

    - `allowed=True` : la requête passe la moderation métier.
    - `allowed=False` : refus, `reason` identifie la catégorie, `message`
      est le texte à renvoyer au user, `kind="input"` ou `"output"` selon
      le côté checké.
    """

    allowed: bool
    reason: str | None = None
    message: str | None = None
    kind: str = "input"


# ─── Catégorie 1 : prescription nominative ───────────────────────
#
# Vise "prescris-moi X mg de Y", "quel dosage me prescrire", "ordonnance
# pour mon enfant"… Volontairement conservateur : on cherche la
# combinaison d'un verbe prescriptif + d'un dosage ou d'un nominatif.
_PRESCRIPTION_PATTERNS = [
    # « prescris/prescrire/ordonnance + posologie/dosage/mg/comprimé »
    re.compile(
        r"\b(prescris|prescrire|ordonnance|posologie|dosage|dose\s+à\s+prendre)\b.{0,160}?"
        r"(\d+\s*(mg|g|ml|µg|mcg|ui|microgrammes?|milligrammes?)|comprimé|gélule|sachet)",
        re.IGNORECASE | re.DOTALL,
    ),
    # « combien de mg/ml/comprimés devrais-je prendre » (verbe avant dosage)
    re.compile(
        r"\b(combien|quelle\s+dose|quelle\s+posologie)\b.{0,80}?"
        r"(prendre|prescri|administrer).{0,80}?"
        r"(\d+\s*(mg|g|ml|µg|mcg|ui)|comprimé|gélule)",
        re.IGNORECASE | re.DOTALL,
    ),
    # « combien de mg d'X devrais-je prendre » (dosage avant verbe)
    re.compile(
        r"\b(combien|quelle\s+dose|quelle\s+posologie)\b.{0,40}?"
        r"\b(mg|g|ml|µg|mcg|ui|milligrammes?|microgrammes?|comprimés?|gélules?)\b.{0,80}?"
        r"\b(prendre|prescri|administrer)\b",
        re.IGNORECASE | re.DOTALL,
    ),
    # « ordonnance pour moi/mon fils/ma fille/mon père… »
    re.compile(
        r"\b(ordonnance|prescription)\b.{0,50}?\b(pour\s+(moi|mon|ma|notre|ton|son|mes|mon\s+\w+))",
        re.IGNORECASE,
    ),
]

_PRESCRIPTION_MESSAGE = (
    "Je ne peux pas te prescrire ni indiquer un dosage personnalisé — "
    "seul un professionnel de santé qui t'examine peut le faire. "
    "Je peux en revanche t'expliquer comment fonctionne un médicament, "
    "ses effets secondaires courants ou quand consulter. "
    "Pour toute prescription, consulte un médecin ou un pharmacien."
)


# ─── Catégorie 2 : rédaction d'acte juridique nominatif ──────────
#
# Vise la demande de rédaction d'un contrat/acte avec des parties
# nommées. L'info générale sur le droit reste permise.
_LEGAL_ACT_PATTERNS = [
    # « rédige / écris / produis un contrat/acte/clause/bail/… »
    re.compile(
        r"\b(rédige|redige|écris|ecris|produis|génère|genere|prépare|prepare)\b.{0,40}?"
        r"\b(contrat|acte|clause|bail|testament|assignation|mise\s+en\s+demeure|"
        r"cessation\s+d[eu]\s+bail|lettre\s+de\s+licenciement|pacte\s+d[ae]\s+\w+|"
        r"protocole\s+d[ae]ccord|statuts|procuration)\b",
        re.IGNORECASE,
    ),
    # « modèle de / exemple d'acte signé entre + nom »
    re.compile(
        r"\b(entre|signé\s+par)\b\s+(m\.?\s|mme\.?\s|monsieur|madame|mr\.?\s|ms\.?\s)[A-Z]",
        re.IGNORECASE,
    ),
]

_LEGAL_ACT_MESSAGE = (
    "Je ne peux pas rédiger d'acte juridique nominatif (contrat, "
    "mise en demeure, bail signé…) à ta place — cela relève d'un "
    "avocat ou d'un notaire qui engage sa responsabilité. "
    "Je peux en revanche t'expliquer la structure d'un contrat, les "
    "clauses-types, les articles de loi applicables ou les étapes "
    "d'une procédure. Pour un acte engageant, consulte un professionnel."
)


# ═══════════════════════════════════════════════════════════════════
# WHITELIST PAR EXPERT
# ═══════════════════════════════════════════════════════════════════
#
# Format : `(expert_id, rule_key)` → True si la règle est désactivée
# pour cet expert. Par défaut, toutes les règles s'appliquent à tous
# les experts. On whiteliste UNIQUEMENT quand c'est sémantiquement
# correct — un expert "medicine" doit pouvoir parler de dosages dans
# un cadre informationnel (ex : « quelle est la dose létale de paracétamol
# pour un adulte ? » relève de la pharmacovigilance, pas d'une
# prescription). On NE whiteliste pas par contre la demande de
# prescription nominative — même sur l'expert medicine.
#
# Ici on reste **strict** : même les experts medicine/legal refusent
# les prescriptions/rédactions nominatives. La whitelist est vide au
# lancement de B2 et sera élargie au cas par cas (ex : sur confirmation
# du Conseil éthique de NEXYA).
# ═══════════════════════════════════════════════════════════════════

_WHITELIST: frozenset[tuple[str, str]] = frozenset()


# ═══════════════════════════════════════════════════════════════════
# CONSTANTES DE RÈGLES
# ═══════════════════════════════════════════════════════════════════

_RULE_PRESCRIPTION = "prescription_nominative"
_RULE_LEGAL_ACT = "legal_act_drafting"


# ═══════════════════════════════════════════════════════════════════
# API PUBLIQUE
# ═══════════════════════════════════════════════════════════════════


def check_business_rules(
    *,
    text: str,
    expert_id: str,
    kind: str = "input",
) -> ModerationRuleDecision:
    """Applique les règles de modération métier au texte.

    - `text` : contenu à vérifier (le message user pour `kind="input"`,
      la réponse LLM pour `kind="output"`).
    - `expert_id` : identifiant d'expert — sert à consulter la whitelist.
    - `kind` : côté vérifié (utile pour les logs et les tests).

    Retourne toujours une décision (jamais None, jamais lève).
    """
    if not settings.moderation_rules_enabled:
        return ModerationRuleDecision(allowed=True)

    if not text:
        return ModerationRuleDecision(allowed=True)

    # Prescription nominative
    if not _is_whitelisted(expert_id, _RULE_PRESCRIPTION):
        if _any_match(text, _PRESCRIPTION_PATTERNS):
            log.info(
                "ai.moderation_rules.rejected",
                rule=_RULE_PRESCRIPTION,
                expert_id=expert_id,
                kind=kind,
            )
            return ModerationRuleDecision(
                allowed=False,
                reason=_RULE_PRESCRIPTION,
                message=_PRESCRIPTION_MESSAGE,
                kind=kind,
            )

    # Rédaction d'acte juridique nominatif
    if not _is_whitelisted(expert_id, _RULE_LEGAL_ACT):
        if _any_match(text, _LEGAL_ACT_PATTERNS):
            log.info(
                "ai.moderation_rules.rejected",
                rule=_RULE_LEGAL_ACT,
                expert_id=expert_id,
                kind=kind,
            )
            return ModerationRuleDecision(
                allowed=False,
                reason=_RULE_LEGAL_ACT,
                message=_LEGAL_ACT_MESSAGE,
                kind=kind,
            )

    return ModerationRuleDecision(allowed=True)


# ═══════════════════════════════════════════════════════════════════
# INTERNE
# ═══════════════════════════════════════════════════════════════════


def _any_match(text: str, patterns: list[re.Pattern[str]]) -> bool:
    return any(p.search(text) for p in patterns)


def _is_whitelisted(expert_id: str, rule_key: str) -> bool:
    return (expert_id, rule_key) in _WHITELIST
