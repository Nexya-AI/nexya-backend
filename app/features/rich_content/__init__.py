"""
Module `rich_content` (C4.4 + C4.5 + C4.6 — NxDraftCard 8 variantes).

Détection automatique de **contenu rédactionnel actionnable** dans la
réponse LLM (brouillons d'email, messages WhatsApp/SMS, posts LinkedIn/
Tweet, documents long PDF, fichiers de code, projets code multi-fichiers).
Quand la détection réussit, le caller
(`chat/router._finalize_in_fresh_session`) injecte un payload structuré
dans `messages.metadata_json.rich_content` qui survit à la réouverture de
la conversation et permet au client Flutter de rendre la carte
`NxDraftCard` (ou `NxCodeFileCard` / `NxCodeProjectCard` pour C4.6) au
lieu d'un bloc texte brut.

V1 (C4.4 — 2026-05-26) : email_draft + whatsapp_draft.
V2 (C4.5 — 2026-05-30) : sms_draft + linkedin_post_draft + tweet_draft +
                         document_draft.
V3 (C4.6 — 2026-05-30) : code_file_draft + code_project_draft.

Cascade de détection (ordre du plus spécifique au plus générique) :
  1. **Code Project** — 2+ blocs ```language\n```` markdown avec
     filenames explicites (≥ 50 %) OU intent fort (« écris une API
     complète »). Code Project doit passer AVANT Code File car un
     projet bien formé contient plusieurs blocs (sinon Code File
     capturerait le 1ᵉʳ isolément). Cap 50 fichiers + 5 MB total.
  2. **Code File** — 1 SEUL bloc ```language\n```` markdown avec
     filename inféré 4 stratégies fallback (ligne précédente / md
     bold / commentaire inline / fallback `main.{ext}`). Cap content
     30-100k chars.
  3. **WhatsApp** — intent-based fast short-circuit (« rédige un
     message WhatsApp ») + body 10k chars cap.
  4. **SMS** — intent-based, body court 1600 chars cap.
  5. **Tweet** — intent-based + cap dur 280 chars.
  6. **LinkedIn** — intent-based + cap 3000 chars.
  7. **Document** — intent + body formel STRUCTURÉ (lettre
     administrative avec recipient « Madame la Maire de Yaoundé » +
     formules de politesse « Veuillez agréer / Salutations
     distinguées »), cap 50k.
  8. **Email** — heuristique markers semi-formels (subject + greeting
     `Bonjour` + closing `Cordialement`).

Ordre justifié — pourquoi Code Project AVANT Code File :
- Un projet code bien formé (3 fichiers Python + requirements.txt)
  contient PLUSIEURS blocs ```python```. Si Code File était testé
  avant, il capturerait `0` ou `none` blocs (car cap = 1 bloc) puis
  Code Project capturerait correctement les 4 fichiers.
- En réalité, Code File a un check strict `len(matches) != 1` qui
  short-circuit si plusieurs blocs présents — donc l'ordre serait
  cohérent même inversé. Mais on respecte la sémantique « du plus
  spécifique au plus général » pour la clarté du contrat.

Ordre justifié — pourquoi Document AVANT Email :
- Une lettre formelle administrative cumule TOUS les markers d'un
  email (subject, greeting, closing) PLUS des markers spécifiques
  (recipient formel avec titre + organisme, formules de politesse
  littéraires).
- Si Email était testé avant, une lettre administrative serait
  classée comme email → cassé en Flutter (rendu carte email avec
  mailto: alors qu'il faut un PDF).
- Document a un seuil minimal de 200 chars + recipient + closing
  formel → ne wrongly classe pas un email court "Bonjour Marie, ..."
- Email reste défensif sur le passage informel/semi-formel.

Aucun appel LLM, aucun I/O, aucun side-effect — le module est pur
Python synchrone, testable en isolation sans fixture réseau ni DB.

V4 (différé) : LLM tool call `propose_rich_content({kind, data})` qui
produit une carte 100% structurée sans heuristique. Architecture
compatible (le tool result alimenterait directement
`RichContentPayload`).
"""

from __future__ import annotations

from app.features.rich_content.code_file_draft_detector import (
    detect_rich_content_code_file,
)
from app.features.rich_content.code_project_draft_detector import (
    detect_code_project_intent,
    detect_rich_content_code_project,
)
from app.features.rich_content.document_draft_detector import (
    detect_document_intent,
    detect_rich_content_document,
)
from app.features.rich_content.email_draft_detector import (
    detect_email_intent,
    detect_rich_content_email,
)
from app.features.rich_content.linkedin_draft_detector import (
    detect_linkedin_intent,
    detect_rich_content_linkedin,
)
from app.features.rich_content.schemas import (
    CodeFileDraftData,
    CodeProjectDraftData,
    CodeProjectFileItem,
    DocumentDraftData,
    EmailDraftData,
    LinkedInPostDraftData,
    RichContentKind,
    RichContentPayload,
    SmsDraftData,
    TweetDraftData,
    WhatsAppDraftData,
)
from app.features.rich_content.sms_draft_detector import (
    detect_rich_content_sms,
    detect_sms_intent,
)
from app.features.rich_content.tweet_draft_detector import (
    detect_rich_content_tweet,
    detect_tweet_intent,
)
from app.features.rich_content.whatsapp_draft_detector import (
    detect_rich_content_whatsapp,
    detect_whatsapp_intent,
)


def detect_rich_content(user_message: str, assistant_text: str) -> dict | None:
    """Point d'entrée principal — cascade Code Project → Code File → WhatsApp → SMS → Tweet → LinkedIn → Document → Email.

    Cascade ordonnée du plus spécifique (Code Project = 2+ blocs nommés
    + intent fort) au plus générique (markers formels potentiellement
    multi-usage).

    Retourne un `dict` prêt à être stocké dans
    `messages.metadata_json["rich_content"]` (format conforme à
    `RichContentPayload`), ou `None` si aucun draft détecté.

    Premier détecteur qui matche gagne — pas de combinaison multiple
    (la carte UI n'affiche qu'un seul type de draft par message).

    Fail-safe : chaque détecteur est défensif sur ses inputs (empty
    string, None coerce, exception Pydantic catché). Le caller
    `chat/router` l'enveloppe quand même en try/except par défense en
    profondeur.
    """
    if not isinstance(user_message, str) or not isinstance(assistant_text, str):
        return None
    if not assistant_text.strip():
        return None

    # 1. Code Project (2+ blocs ```language\n``` nommés OU intent fort).
    #    DOIT être testé AVANT Code File qui n'accepte que 1 bloc isolé.
    code_project = detect_rich_content_code_project(user_message, assistant_text)
    if code_project is not None:
        return code_project

    # 2. Code File (1 SEUL bloc ```language\n``` avec filename inféré).
    #    body-driven : pas besoin d'intent user — un seul bloc de code
    #    dans la réponse suffit (UX cohérente ChatGPT/Claude).
    code_file = detect_rich_content_code_file(user_message, assistant_text)
    if code_file is not None:
        return code_file

    # 3. WhatsApp (intent-strict, court-circuit le plus rapide)
    wa = detect_rich_content_whatsapp(user_message, assistant_text)
    if wa is not None:
        return wa

    # 4. SMS (intent-strict, body cap 1600)
    sms = detect_rich_content_sms(user_message, assistant_text)
    if sms is not None:
        return sms

    # 5. Tweet (intent-strict, body cap 280 dur)
    tweet = detect_rich_content_tweet(user_message, assistant_text)
    if tweet is not None:
        return tweet

    # 6. LinkedIn (intent-strict, body cap 3000)
    linkedin = detect_rich_content_linkedin(user_message, assistant_text)
    if linkedin is not None:
        return linkedin

    # 7. Document long PDF AVANT Email — une lettre formelle (recipient
    #    + closing formel + cap 200 chars minimum) cumule tous les markers
    #    d'un email. Si Email était testé avant, on classerait à tort.
    document = detect_rich_content_document(user_message, assistant_text)
    if document is not None:
        return document

    # 8. Email (intent OU body markers semi-formels — Bonjour/Cordialement)
    email = detect_rich_content_email(user_message, assistant_text)
    if email is not None:
        return email

    return None


__all__ = [
    "CodeFileDraftData",
    "CodeProjectDraftData",
    "CodeProjectFileItem",
    "DocumentDraftData",
    "EmailDraftData",
    "LinkedInPostDraftData",
    "RichContentKind",
    "RichContentPayload",
    "SmsDraftData",
    "TweetDraftData",
    "WhatsAppDraftData",
    "detect_code_project_intent",
    "detect_document_intent",
    "detect_email_intent",
    "detect_linkedin_intent",
    "detect_rich_content",
    "detect_rich_content_code_file",
    "detect_rich_content_code_project",
    "detect_rich_content_document",
    "detect_rich_content_email",
    "detect_rich_content_linkedin",
    "detect_rich_content_sms",
    "detect_rich_content_tweet",
    "detect_rich_content_whatsapp",
    "detect_sms_intent",
    "detect_tweet_intent",
    "detect_whatsapp_intent",
]
