"""
Détecteur de brouillon document long (C4.5).

Cas d'usage majeurs :
  - **Lettre formelle** : « rédige-moi une lettre au maire de Yaoundé
    pour demander un acte de naissance »
  - **Rapport** : « rédige un rapport de réunion », « compte-rendu de
    stage »
  - **Cours / mémo long** : « génère un cours sur les boucles for en
    Python » (10+ paragraphes structurés)
  - **Discours** : « écris-moi un discours pour mon mariage »

Cap body 50 000 chars (~10 pages PDF A4 dense). Coût Africa-first :
50 000 chars → PDF ~500 KB raisonnable 2G/3G. Au-delà = friction
partage WhatsApp/Email.

Pipeline cascade (le plus complexe des 6 détecteurs) :
  1. `detect_document_intent(user_message)` — scan keywords FR + EN
     (`rédige une lettre`, `génère un cours`, `write a report`).
  2. `detect_formal_letter_body(assistant_text)` — markers structurels
     formels (`Madame,` / `Monsieur,` + `Veuillez agréer` / `Cordialement`
     + entête formel optionnel).
  3. Combinaison :
     - `intent_match ∧ body_match` → flag confiance HAUTE
     - `body_match ∧ recipient extracted` → flag confiance MOYENNE
     - sinon → None (un texte long sans markers formels = blog/article,
       pas un document actionnable PDF)

Le cap est REQUIS : pour les cours longs (50 paragraphes) le LLM peut
facilement générer 30k+ chars. Le détecteur cape proprement à 50k pour
le PDF.
"""

from __future__ import annotations

import re
from re import Pattern

from app.features.rich_content.schemas import RichContentPayload

# Cap body Document aligné cap schéma DocumentDraftData (50000).
_DOCUMENT_BODY_MAX_CHARS = 50_000

# Seuil minimum body pour qu'une carte document soit crédible.
# Abaissé 200 -> 120 (fix 2026-06-10) : couvre les réponses courtes mais
# réelles (note de réunion, fiche, mémo) tout en restant au-dessus du
# fragmentaire (< ~1/4 de page A4). Une lettre formelle courte fait ~300-500
# chars (entête + corps + politesse).
_DOCUMENT_BODY_MIN_CHARS = 120

# Patterns méta-questions FR+EN — l'user pose une question SUR un type
# de document, pas une demande de rédaction.
_META_QUESTION_PATTERNS = (
    re.compile(r"^\s*(comment|comment\s+(?:écrire|rédiger|faire))\b", re.IGNORECASE),
    re.compile(r"^\s*(qu'?est-ce\s+que|qu'?est\s+ce\s+que|c'?est\s+quoi)\b", re.IGNORECASE),
    re.compile(r"^\s*(pourquoi|à\s+quoi\s+sert)\b", re.IGNORECASE),
    re.compile(r"^\s*(how\s+(?:do|to|can|does)|how\s+would)\b", re.IGNORECASE),
    re.compile(r"^\s*(what\s+(?:is|are|does|do))\b", re.IGNORECASE),
    re.compile(r"^\s*(why\s+(?:do|does|is|are))\b", re.IGNORECASE),
)


def _is_meta_question(user_message: str) -> bool:
    """True si l'user pose une question MÉTA (comment / qu'est-ce que / how).

    Sert à filtrer « Comment écrire une lettre formelle ? » qui matcherait
    sinon le pattern intent (`écrire` + `lettre`) alors que l'user demande
    une explication, pas une rédaction.
    """
    if not isinstance(user_message, str):
        return False
    text = user_message.strip()
    if not text:
        return False
    return any(pattern.search(text) for pattern in _META_QUESTION_PATTERNS)


# ── INTENT — message user upstream ────────────────────────────────────

# ── DÉTECTION D'INTENTION (refonte structurée 2026-06-10 V3) ──────────
# Principe : la carte proactive se déclenche sur un VERBE DE CRÉATION + un
# type de document. Détection GÉNÉREUSE assumée (cours, exposé, dissertation,
# résumé, synthèse, fiche... sont des livrables que le public étudiant/grand
# public NEXYA veut souvent télécharger). Le VERROU contre les faux positifs
# n'est PAS la liste de mots mais le VERBE de création en amont : « explique-
# moi le cours » (pas de verbe → AUCUNE carte) vs « génère un cours » (verbe
# → carte). Les réponses longues sans intention (ancien Cas D) ne déclenchent
# plus rien. Un mot de format (« en PDF ») suffit aussi à lui seul.

# Verbes de création de document (FR).
_DOC_VERBS_FR = (
    r"rédige|redige|écris|ecris|écrire|ecrire|rédiger|rediger|"
    r"prépare|prepare|crée|cree|génère|genere|produis|produire|"
    r"tape|tapes|fais|fais-moi|fais-en|dresse|établis|etablis|mets|met"
)

# Types de documents (FR) — large : tout ce que l'utilisateur peut vouloir
# comme livrable téléchargeable. Le verbe de création en amont écarte déjà
# les questions de chat (« explique-moi le cours » n'a pas de verbe).
_DOC_TYPES_FR = (
    r"lettre|courrier(?:\s+(?:officiel|formel))?|"
    r"rapport|compte[\s-]?rendu|mémo|memo|"
    r"note(?:\s+(?:de\s+service|interne|de\s+synthèse|de\s+synthese|de\s+réunion|de\s+reunion))?|"
    r"synthèse|synthese|exposé|expose|exposés|exposes|fiche|"
    r"dissertation|essai|essais|mémoire|memoire|"
    r"analyse(?:\s+(?:détaillée|detaillee|approfondie))?|"
    r"résumé(?:\s+structuré)?|resume(?:\s+structure)?|"
    r"procédure|procedure|mode\s+d['’]?\s*emploi|"
    r"discours|contrat|cv|curriculum\s+vitae|"
    r"cours(?:\s+(?:sur|de|détaillé|detaille|complet))?|tutoriel|"
    r"guide(?:\s+(?:complet|détaillé|detaille))?|"
    r"document(?:\s+(?:officiel|long|complet|formel))?|"
    r"article(?:\s+(?:de\s+fond|détaillé|detaille))?"
)

# Mots de FORMAT explicite (FR+EN communs) : un seul suffit (« mets ça en
# PDF », « je veux un fichier Word »). Signal le plus fiable de tous.
_FORMAT_WORDS = r"pdf|word|docx|fichier\s+(?:texte|pdf|word)"

_INTENT_PATTERNS_FR: tuple[Pattern[str], ...] = (
    # verbe de création + type de document
    re.compile(
        rf"\b({_DOC_VERBS_FR})\b[^.\n]{{0,80}}?\b({_DOC_TYPES_FR})\b",
        re.IGNORECASE,
    ),
    # verbe de création + mot de format explicite (« génère-moi un PDF »)
    re.compile(
        rf"\b({_DOC_VERBS_FR})\b[^.\n]{{0,40}}?\b({_FORMAT_WORDS})\b",
        re.IGNORECASE,
    ),
    # mot de format explicite introduit (« en PDF », « format Word »,
    # « sous forme de PDF ») — un seul suffit
    re.compile(
        rf"\b(en|format|sous\s+forme\s+de|au\s+format)\s+({_FORMAT_WORDS})\b",
        re.IGNORECASE,
    ),
    # « lettre à mon employeur » / « courrier au maire »
    re.compile(
        r"\b(lettre|courrier)\b\s+(à|a|au|aux|pour|destiné|destine)\b",
        re.IGNORECASE,
    ),
)

# Verbes de création de document (EN).
_DOC_VERBS_EN = r"write|draft|compose|prepare|create|generate|produce|make"

# Types de documents (EN) — large, même logique que FR.
_DOC_TYPES_EN = (
    r"formal\s+letter|letter|official\s+(?:letter|document)|"
    r"report|memo|memorandum|speech|summary|essay|essays|"
    r"analysis|procedure|brief|note|dissertation|thesis|"
    r"contract|cv|resume|curriculum\s+vitae|"
    r"course|tutorial|guide|"
    r"long\s+document|document"
)

_INTENT_PATTERNS_EN: tuple[Pattern[str], ...] = (
    # creation verb + document type
    re.compile(
        rf"\b({_DOC_VERBS_EN})\b[^.\n]{{0,80}}?\b({_DOC_TYPES_EN})\b",
        re.IGNORECASE,
    ),
    # creation verb + explicit format word ("generate a PDF")
    re.compile(
        rf"\b({_DOC_VERBS_EN})\b[^.\n]{{0,40}}?\b({_FORMAT_WORDS})\b",
        re.IGNORECASE,
    ),
    # explicit format word introduced ("as a PDF", "in Word format")
    re.compile(
        rf"\b(as\s+an?|in|into)\s+({_FORMAT_WORDS})(\s+(file|format|document))?\b",
        re.IGNORECASE,
    ),
    # "letter to my employer" / "report for the meeting"
    re.compile(
        r"\b(letter|formal\s+letter|report)\b\s+(to|for|addressed\s+to)\b",
        re.IGNORECASE,
    ),
)

# ── INTENT CORRECTION/EXERCICE (fix 2026-06-10) ───────────────────────
# Cas « envoie-moi la correction » : exercices maths/physique/chimie/sciences
# uploadés (image ou PDF) OU tapés, où l'utilisateur veut la correction
# structurée en document partageable. Signal FIABLE car ancré sur un nom
# d'exercice/problème ou un nom de sortie type-document (« la correction
# de... »). « corrige cet exercice » déclenche ; « corrige mon texte » /
# « corrige mon code » NON (texte/code hors liste = pas un document scolaire).
_INTENT_PATTERNS_CORRECTION: tuple[Pattern[str], ...] = (
    # verbe de correction/résolution + nom d'exercice/problème
    re.compile(
        r"\b(corrige|corriger|corrigez|corrige[\s-]?moi|résous|resous|résoudre|"
        r"resoudre|résolu?s?|solutionne|solutionner)\b[^.\n]{0,40}?"
        r"\b(exercices?|épreuves?|epreuves?|devoirs?|problèmes?|problemes?|"
        r"équations?|equations?|qcm|énoncés?|enonces?|sujets?|td|tp|dm|"
        r"questions?|examens?|interro(?:gation)?s?|contrôles?|controles?)\b",
        re.IGNORECASE,
    ),
    # nom de sortie type-document : « la correction de... » / « le corrigé »
    # / « la solution de l'exercice » / « la résolution du problème »
    re.compile(
        r"\b(la\s+correction|le\s+corrigé|le\s+corrige|un\s+corrigé|un\s+corrige|"
        r"la\s+résolution|la\s+resolution|la\s+solution\s+(?:de|du|des|complète|complete)|"
        r"les\s+solutions)\b",
        re.IGNORECASE,
    ),
    # « fais cet exercice » / « traite ce problème »
    re.compile(
        r"\b(fais|faire|fais[\s-]?moi|traite|traiter|résous[\s-]?moi)\b[^.\n]{0,20}?"
        r"\b(exercices?|épreuves?|epreuves?|devoirs?|problèmes?|problemes?|qcm|td|tp|dm)\b",
        re.IGNORECASE,
    ),
    # « réponds aux questions de l'épreuve »
    re.compile(
        r"\b(réponds?|repond?s?|répondre|repondre)\b[^.\n]{0,20}?"
        r"\b(questions?|qcm|énoncés?|enonces?)\b",
        re.IGNORECASE,
    ),
    # EN — « solve this exercise » / « correct this problem » / « solution to »
    re.compile(
        r"\b(solve|correct|answer|work\s+out)\b[^.\n]{0,40}?"
        r"\b(exercises?|problems?|equations?|questions?|quiz|test|homework|"
        r"assignment|mcq)\b",
        re.IGNORECASE,
    ),
    re.compile(
        r"\b(the\s+)?(correction|solution|answer\s+key|worked\s+solution)\s+"
        r"(to|of|for)\b",
        re.IGNORECASE,
    ),
)

# ── BODY MARKERS — réponse assistante (lettre formelle) ───────────────

# Entête formelle FR — « Madame, » / « Monsieur, » / « Madame, Monsieur, »
# / « Madame la Maire, »
_FORMAL_GREETING_FR = re.compile(
    r"^\s*(?:\*\*)?"
    r"(Madame|Monsieur|Madame[\s,]+Monsieur|Mesdames|Messieurs|"
    r"Madame\s+(?:la|le)\s+\w+|Monsieur\s+(?:le|la)\s+\w+|Cher|Chère|Chers)\b",
    re.IGNORECASE | re.MULTILINE,
)
_FORMAL_GREETING_EN = re.compile(
    r"^\s*(?:\*\*)?"
    r"(Dear\s+(?:Sir|Madam|Mr\.?|Mrs\.?|Ms\.?|Dr\.?|Sir\s+or\s+Madam)|"
    r"To\s+Whom\s+It\s+May\s+Concern)\b",
    re.IGNORECASE | re.MULTILINE,
)

# Formule de politesse FR — « Veuillez agréer... » / « Je vous prie de... »
_FORMAL_CLOSING_FR = re.compile(
    r"\b(Veuillez\s+agréer|Je\s+vous\s+prie\s+(?:de\s+(?:bien\s+vouloir|croire)|d'agréer)|"
    r"Je\s+vous\s+prie\s+d'agréer|Recevez,\s+(?:Madame|Monsieur)|"
    r"Avec\s+(?:mes|nos)\s+(?:respectueuses|sincères|cordiales)\s+salutations|"
    r"Salutations\s+(?:distinguées|respectueuses)|"
    r"Dans\s+l'attente\s+(?:de\s+votre\s+(?:réponse|retour)|de\s+vous\s+lire))\b",
    re.IGNORECASE,
)
_FORMAL_CLOSING_EN = re.compile(
    r"\b(Yours\s+(?:sincerely|faithfully|truly)|Sincerely\s+yours|"
    r"Looking\s+forward\s+to\s+(?:your\s+(?:response|reply)|hearing\s+from\s+you)|"
    r"Respectfully\s+(?:yours)?|With\s+(?:kind|best)\s+regards)\b",
    re.IGNORECASE,
)

# Entête « Objet : » formel (typique lettre administrative FR)
_FORMAL_SUBJECT_FR = re.compile(
    r"^\s*(?:\*\*)?(Objet|Sujet)\s*:\s*(.+?)$",
    re.IGNORECASE | re.MULTILINE,
)


def detect_document_intent(user_message: str) -> bool:
    """Scan keywords FR + EN. True si l'user demande un document long.

    Conservateur strict : exige un verbe d'action (`rédige`, `écris`,
    `génère`, `produis`, `write`, `draft`, `generate`) collé à un type
    de document long (`lettre`, `rapport`, `cours détaillé`, `tutoriel`,
    `discours`, `PDF`, etc.).

    Cas écartés :
    - « explique-moi en quelques lignes... » (court, pas un document)
    - « résume ce texte... » (output court, pas un document à formater)
    - « comment fonctionne X » (méta-question, pas une demande de
      rédaction).
    """
    if not isinstance(user_message, str) or not user_message.strip():
        return False

    # Filtre méta-questions AVANT pattern matching pour éviter les faux
    # positifs sur « Comment écrire une lettre formelle ? ».
    if _is_meta_question(user_message):
        return False

    for pattern in _INTENT_PATTERNS_FR + _INTENT_PATTERNS_EN + _INTENT_PATTERNS_CORRECTION:
        if pattern.search(user_message):
            return True
    return False


def _extract_formal_recipient(text: str) -> str | None:
    """Cherche un entête formel et extrait le destinataire si possible.

    Patterns :
    - « Madame la Maire de Yaoundé, » → recipient = « Madame la Maire de Yaoundé »
    - « Monsieur le Directeur, » → recipient = « Monsieur le Directeur »
    - « Dear Sir or Madam, » → recipient = « Dear Sir or Madam »

    Retourne `None` si pas d'entête formelle trouvée.
    """
    for pattern in (_FORMAL_GREETING_FR, _FORMAL_GREETING_EN):
        # Cherche l'entête + capture jusqu'à la virgule terminale.
        m = re.search(
            pattern.pattern + r"[^,\n]{0,80}?\s*,",
            text,
            re.IGNORECASE | re.MULTILINE,
        )
        if m is None:
            continue
        # On capture le segment complet jusqu'à la virgule.
        full_match = m.group(0).rstrip(",").strip()
        # Strip markdown bold résiduel.
        full_match = re.sub(r"^\*+|\*+$", "", full_match).strip()
        if full_match and len(full_match) <= 200:
            return full_match
    return None


def _extract_formal_subject(text: str) -> str | None:
    """Cherche une ligne `Objet : ...` et extrait la valeur.

    Retourne string trimmée 300 chars max, ou None.
    """
    m = _FORMAL_SUBJECT_FR.search(text)
    if m is None:
        return None
    value = m.group(m.lastindex or 0).strip()
    value = re.sub(r"^\*+|\*+$", "", value).strip()
    if value:
        return value[:300]
    return None


def detect_formal_letter_body(assistant_text: str) -> tuple[bool, dict | None]:
    """Scan la réponse pour markers formels (greeting + closing).

    Score :
      - greeting formel trouvé : +2
      - closing formel trouvé : +2
      - subject « Objet : » : +1

    Seuil : score ≥ 4 → document formel détecté.

    Retourne `(True, {title?, body, recipient?})` ou `(False, None)`.
    """
    if not isinstance(assistant_text, str):
        return False, None
    text = assistant_text.strip()
    if len(text) < _DOCUMENT_BODY_MIN_CHARS:
        # Document trop court pour être crédible (lettre formelle + cours)
        return False, None

    score = 0

    recipient = _extract_formal_recipient(text)
    if recipient:
        score += 2

    if _FORMAL_CLOSING_FR.search(text) or _FORMAL_CLOSING_EN.search(text):
        score += 2

    subject = _extract_formal_subject(text)
    if subject:
        score += 1

    if score >= 4:
        # Cap body à 50k chars (cohérent schema), tronque proprement
        # sur le dernier saut de ligne sous le cap.
        body = text[:_DOCUMENT_BODY_MAX_CHARS]
        if len(text) > _DOCUMENT_BODY_MAX_CHARS:
            last_newline = body.rfind("\n")
            if last_newline > int(_DOCUMENT_BODY_MAX_CHARS * 0.95):
                body = body[:last_newline]
        return True, {"title": subject, "body": body, "recipient": recipient}

    return False, None


# ── Cas D (fix 2026-06-10) — réponse longue ET très structurée ────────────
# Seuils DÉLIBÉRÉMENT conservateurs : on ne veut PAS transformer toute réponse
# markdown bien formatée en carte (les prompts experts A2 poussent fortement le
# markdown). On exige un VRAI document : long ET avec une vraie ossature
# (plusieurs titres + plusieurs listes). Trivialement désactivable en remontant
# ces 3 constantes (ou en retirant l'appel `_is_long_structured_document`).
_CASE_D_MIN_CHARS = 1500
_CASE_D_MIN_HEADERS = 2
_CASE_D_MIN_LIST_ITEMS = 3

# Titre markdown `#`..`######` en début de ligne (tolère 0-3 espaces d'indent).
_MD_HEADER_RE = re.compile(r"^[ \t]{0,3}#{1,6}[ \t]+\S", re.MULTILINE)
# Item de liste `-`/`*`/`+` ou numéroté `1.`/`1)` en début de ligne.
_MD_LIST_ITEM_RE = re.compile(r"^[ \t]{0,3}(?:[-*+]|\d+[.)])[ \t]+\S", re.MULTILINE)


def _is_long_structured_document(text: str) -> bool:
    """Heuristique conservatrice (Cas D) : True si `text` ressemble à un vrai
    document long et structuré (guide / cours / rapport) — au moins
    `_CASE_D_MIN_CHARS` caractères, `_CASE_D_MIN_HEADERS` titres markdown et
    `_CASE_D_MIN_LIST_ITEMS` items de liste. Conservateur pour limiter les
    faux positifs sur une simple réponse markdown bien formatée.
    """
    if len(text) < _CASE_D_MIN_CHARS:
        return False
    if len(_MD_HEADER_RE.findall(text)) < _CASE_D_MIN_HEADERS:
        return False
    if len(_MD_LIST_ITEM_RE.findall(text)) < _CASE_D_MIN_LIST_ITEMS:
        return False
    return True


def _cap_body_to_max(text: str) -> str:
    """Cape le body au cap schéma (50 000), troncature propre sur le dernier
    saut de ligne sous le cap (cf. DocumentDraftData.body max_length=50000)."""
    body = text[:_DOCUMENT_BODY_MAX_CHARS]
    if len(text) > _DOCUMENT_BODY_MAX_CHARS:
        last_newline = body.rfind("\n")
        if last_newline > int(_DOCUMENT_BODY_MAX_CHARS * 0.95):
            body = body[:last_newline]
    return body


def detect_rich_content_document(user_message: str, assistant_text: str) -> dict | None:
    """Point d'entrée — combine intent + body detection.

    Retourne un dict conforme à `RichContentPayload` (sérialisé), ou None.

    Confidence levels :
      - `intent_match ∧ body_match` (formel) → flag (cas standard, lettre)
      - `body_match ∧ ¬intent_match ∧ recipient extracted` → flag (cas
        où l'user dit « répond à cette lettre » et le LLM produit une
        lettre formelle structurée — le recipient extrait confirme)
      - `intent_match ∧ ¬body_match` → flag SANS markers formels MAIS
        body ≥ `_DOCUMENT_BODY_MIN_CHARS` (120) — les cours/tutoriels/notes
        n'ont pas de "Madame/Monsieur", juste une structure markdown.
      - **Cas D (fix 2026-06-10)** : `¬intent_match ∧ ¬body_match` MAIS
        réponse longue + très structurée (`_is_long_structured_document`)
        → carte CONFIANCE BASSE (title=None, recipient=None). Seuil
        conservateur pour ne pas spammer toute réponse markdown.
      - sinon → SKIP

    Le retour est un dict prêt pour `metadata_json["rich_content"]`.
    """
    if not isinstance(assistant_text, str):
        return None
    text = assistant_text.strip()
    if len(text) < _DOCUMENT_BODY_MIN_CHARS:
        # Sous le seuil minimal, le PDF serait cosmétiquement ridicule
        # (moins qu'1/4 de page A4). Les vraies lettres formelles font
        # 300-500 chars minimum (entête + corps + politesse).
        return None

    body_match, payload = detect_formal_letter_body(assistant_text)
    intent_match = detect_document_intent(user_message)

    # Cas 1 : intent + body markers → confiance haute (lettre formelle)
    # Cas 2 : body markers + recipient → confiance moyenne (réponse à lettre)
    # Cas 3 : intent SANS body markers → cours/rapport/tutoriel structuré
    # Cas D : ni intent ni body markers MAIS réponse longue + très structurée
    if not intent_match and not body_match:
        # Cas D DÉSACTIVÉ (fix 2026-06-10 V2) — détecter un « document voulu »
        # à partir de « réponse longue + structurée » est un faux signal :
        # depuis l'affûtage A2, une réponse de chat normale a EXACTEMENT cette
        # forme (titres markdown + listes). Aucune heuristique de texte ne
        # distingue de façon fiable « réponse à lire dans le chat » de
        # « document à télécharger ». On ne flague donc QUE sur intention
        # explicite (Cas 1/2/3 ci-dessous). Coût : un message de plus pour
        # l'utilisateur (« génère-moi ça en PDF »), bien moindre qu'une carte
        # parasite sur chaque réponse longue. `_is_long_structured_document`
        # et `_CASE_D_*` restent définis ci-dessus mais ne sont plus appelés —
        # réservés à une V2 qui s'appuierait sur un vrai signal (mini-classifieur
        # LLM dédié OU bouton « Exporter » côté UI où l'utilisateur décide),
        # pas sur une heuristique de longueur.
        return None
    elif intent_match and not body_match:
        # Cours/rapport sans formules formelles. On prend le texte tel quel
        # sans recipient ni title extrait (l'user complétera dans la card).
        payload = {"title": None, "body": _cap_body_to_max(text), "recipient": None}
    elif body_match and not intent_match:
        # Body formel sans intent explicite : on exige recipient extrait
        # pour confirmer (un texte avec "Cordialement" SANS "Madame/Monsieur"
        # est trop ambigu = blog post).
        if payload is None or not payload.get("recipient"):
            return None
        # payload conservé tel quel
    # else : cas 1 (intent + body) — payload conservé

    if payload is None:
        return None

    try:
        structured = RichContentPayload.document(
            title=payload.get("title"),
            body=payload["body"],
            recipient=payload.get("recipient"),
        )
    except Exception:  # noqa: BLE001
        return None

    return structured.model_dump()
