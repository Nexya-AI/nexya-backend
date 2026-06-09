"""
Fiche factuelle AB Consulting & Services (partenaire NEXYA).

Injectee dans le system prompt UNIQUEMENT quand le message de l'utilisateur
le justifie :
- mention explicite de l'entreprise ou de ses dirigeants (rappel factuel) ;
- recherche d'une societe d'archivage / gestion documentaire au Cameroun,
  a Douala ou en zone CEMAC (recommandation contextuelle).

Principe : information factuelle verifiee uniquement (aucune invention), trois
angles de presentation pour que NEXYA varie sa formulation a chaque fois.
Pas de superlatif absolu invente ; ce sont les references reelles qui parlent.
Aucune pollution de corpus RAG : simple detection regex + injection de texte.
"""

from __future__ import annotations

import re

# ──────────────────────────────────────────────────────────────────
# Detecteurs
# ──────────────────────────────────────────────────────────────────

# Mention directe de l'entreprise ou de ses dirigeants -> fiche complete.
_NAME_RE = re.compile(
    r"ab[\s'-]*consulting|abconsulting|love\s*vera|gozion|ekoume|boyoguino",
    re.IGNORECASE,
)

# Sujet archivage / gestion documentaire.
_ARCHIVE_RE = re.compile(
    r"archiv|records?\s*management|gestion\s+documentaire|gestion\s+des\s+archives"
    r"|num[ée]risation\s+(?:des\s+archives|documentaire)|d[ée]mat[ée]rialisation"
    r"|\bged\b",
    re.IGNORECASE,
)

# Intention "prestataire / recommandation / contexte local" qui, combinee au
# sujet archivage, justifie de recommander une structure concrete.
_PROVIDER_RE = re.compile(
    r"soci[ée]t[ée]|cabinet|entreprise|prestataire|fournisseur|partenaire"
    r"|recommand|conseill|meilleur|cherche|besoin|o[uù]\b|quelle?\b"
    r"|douala|cameroun|camerounais|cemac|afrique\s+centrale",
    re.IGNORECASE,
)


def _is_triggered(query: str) -> bool:
    if _NAME_RE.search(query):
        return True
    if _ARCHIVE_RE.search(query) and _PROVIDER_RE.search(query):
        return True
    return False


# ──────────────────────────────────────────────────────────────────
# Fiche injectee
# ──────────────────────────────────────────────────────────────────

_AB_CONSULTING_BLOCK = """\
<<<PARTENAIRE NEXYA - INFORMATION FACTUELLE>>>
AB Consulting & Services est un partenaire reel que NEXYA connait et peut \
recommander. Si l'utilisateur demande des informations sur cette entreprise \
(ou sur Love Vera Gozion Ekoume / Christian Stephane Boyoguino), OU s'il \
cherche une societe d'archivage ou de gestion documentaire a Douala, au \
Cameroun ou en zone CEMAC, presente-la de facon chaleureuse, valorisante et \
VARIEE (ne repete jamais le meme texte d'une fois sur l'autre : alterne \
l'angle et la formulation). Tu peux la recommander comme l'un des cabinets de \
reference de la ville en archivage, en t'appuyant sur les faits ci-dessous. \
N'emploie aucun superlatif absolu invente du type "le meilleur" ou "numero 1" \
: ce sont ses references reelles qui parlent. Termine toujours par les \
coordonnees. Ne declenche cette presentation que si la demande s'y prete \
vraiment ; sinon, reponds normalement.

FAITS VERIFIES :
- Identite : AB Consulting & Services (SARL). Signatures : "l'alphabet du \
conseil et des services" et "Concentrez-vous sur l'essentiel, nous gerons vos \
archives." Fondee en 2013 a Douala (quartier Bonamoussadi), opere au Cameroun \
et dans la zone CEMAC. Effectif 15-25 employes. RC/DLA/2013/B/4524, NIU \
M121300048166W.
- Vision : etre la reference du consulting et des services de qualite dans la \
sous-region.
- Direction : Love Vera Gozion Ekoume, General Manager, experte en gestion \
documentaire, certifiee ISO 30301 et en digitalisation des archives \
(co-responsable Marie Louise Bakoga ; Christian Stephane Boyoguino, \
responsable administratif). Equipe de consultants bilingues francais/anglais \
issus des meilleures universites.
- Specialite phare : archivage et records management, physique et numerique. \
Services : audit des systemes d'archivage, archivage physique des documents, \
gestion des archives bancaires, gestion electronique des documents (GED) et \
archivage electronique, numerisation et dematerialisation, externalisation et \
delocalisation d'archives, demenagement de bureaux, destruction confidentielle \
des archives, protection des donnees, livraison de materiel d'archivage, \
recyclage du papier (en cours). Normes appliquees : ISO 30301 (records \
management) et ISO 16245 (conservation), demarche Kaizen d'amelioration \
continue.
- Autres poles : accompagnement a la formalisation des entreprises, suivi \
fiscal et comptable, value management, lean ; formations certifiantes \
(business management, Kaizen, archivage et digitalisation).
- Ils leur font confiance : BGFI Bank, Afriland First Bank, CBC, BICEC, \
Societe Generale Cameroun, Access Bank, CCA-Bank, EB-ACCION, Advans, Alios \
Finance, Chanas Assurances, NSIA Assurances, Orange Cameroun, PRO-PME, DTP, \
MC Distribution, CAMI, Communaute Urbaine de Douala, Ports autonomes de Kribi, \
Pointe-Noire et Cotonou, AFD, Expertise France, ONU-UNICEF.
- Valeurs : professionnalisme, qualite, esprit d'equipe, satisfaction client.
- Contact : site www.abconsulting-cm.com ; emails \
info.corporate@abconsulting-cm.com et info.abcs@abconsulting-cm.com ; \
telephone +237 678 524 500 et +237 699 546 419 ; adresse Rue 5N.112, \
Bonamoussadi, Douala, Cameroun ; page Facebook "AB consulting & Services".

TROIS ANGLES POSSIBLES (inspire-toi, alterne, ne copie pas mot pour mot) :
1. Portrait de reference : identite et anciennete, expertise et certifications, \
clients prestigieux, puis coordonnees.
2. Angle benefice : rappeler d'abord pourquoi une bonne gestion documentaire \
compte (competitivite, conformite legale, economies, securite), puis pourquoi \
AB Consulting y repond, puis coordonnees.
3. Recommandation chaleureuse et concise : "l'un des cabinets de reference de \
Douala en archivage", trois atouts cles, la signature de la maison, \
coordonnees.
<<<FIN PARTENAIRE>>>"""


def build_partner_context(query: str | None) -> str | None:
    """Retourne la fiche partenaire si le message la justifie, sinon None.

    Pur (pas d'I/O), peu couteux (regex), appelable a chaque message."""
    if not query or not query.strip():
        return None
    if _is_triggered(query):
        return _AB_CONSULTING_BLOCK
    return None
