"""Tests `detect_rich_content` (entry point combinant email + whatsapp)."""

from __future__ import annotations

from app.features.rich_content import detect_rich_content


class TestDetectRichContent:
    def test_email_takes_precedence_when_no_whatsapp_intent(self) -> None:
        result = detect_rich_content(
            user_message="Rédige un mail de relance à mon fournisseur",
            assistant_text=(
                "Sujet : Relance commande\n\n"
                "Bonjour Monsieur Diallo,\n\n"
                "Je me permets de vous relancer.\n\n"
                "Cordialement,\nIvan"
            ),
        )
        assert result is not None
        assert result["kind"] == "email_draft"

    def test_whatsapp_detected_when_intent(self) -> None:
        result = detect_rich_content(
            user_message="Rédige un WhatsApp à Marie",
            assistant_text="Salut Marie ! On se voit demain à 10h ?",
        )
        assert result is not None
        assert result["kind"] == "whatsapp_draft"

    def test_whatsapp_intent_priority_over_email_body(self) -> None:
        # WhatsApp intent + email-looking body → WhatsApp wins (intent more specific)
        result = detect_rich_content(
            user_message="Rédige un WhatsApp pour mon client",
            assistant_text=(
                "Sujet : Confirmation\n\n"
                "Bonjour Madame,\n\nVoici la confirmation.\n\nCordialement,\nIvan"
            ),
        )
        assert result is not None
        assert result["kind"] == "whatsapp_draft"

    def test_no_intent_no_body_returns_none(self) -> None:
        result = detect_rich_content(
            user_message="Quelle est la capitale du Cameroun ?",
            assistant_text="La capitale est Yaoundé.",
        )
        assert result is None

    def test_empty_assistant_text_returns_none(self) -> None:
        assert detect_rich_content("Rédige un mail", "") is None
        assert detect_rich_content("Rédige un mail", "   ") is None

    def test_non_string_inputs_return_none(self) -> None:
        assert detect_rich_content(None, "body") is None  # type: ignore[arg-type]
        assert detect_rich_content("user", None) is None  # type: ignore[arg-type]
        assert detect_rich_content(123, "body") is None  # type: ignore[arg-type]

    def test_payload_is_dict_ready_for_metadata_json(self) -> None:
        """Le retour doit être un dict directement insérable dans metadata_json JSONB."""
        result = detect_rich_content(
            user_message="Rédige un mail à Marie",
            assistant_text=(
                "Sujet : Bonjour\n\nBonjour Marie,\n\nÇa va ?\n\nCordialement,\nIvan"
            ),
        )
        assert isinstance(result, dict)
        assert "kind" in result
        assert "data" in result
        assert isinstance(result["data"], dict)


class TestDetectRichContentCascadeC45:
    """Tests de la cascade C4.5 (6 kinds : WhatsApp > SMS > Tweet > LinkedIn > Email > Document)."""

    def test_whatsapp_priority_over_sms(self) -> None:
        # Both intents could match → WhatsApp wins (first in cascade)
        result = detect_rich_content(
            user_message="Rédige un WhatsApp à Marie",
            assistant_text="Salut Marie ! On se voit demain à 10h ?",
        )
        assert result is not None
        assert result["kind"] == "whatsapp_draft"

    def test_sms_detected_when_intent_sms_only(self) -> None:
        result = detect_rich_content(
            user_message="Rédige un SMS à mon père",
            assistant_text="Salut papa, je rentre tard ce soir. À+",
        )
        assert result is not None
        assert result["kind"] == "sms_draft"

    def test_tweet_detected(self) -> None:
        result = detect_rich_content(
            user_message="Rédige un tweet pour ma promotion",
            assistant_text="Heureux d'annoncer ma promotion au poste de Lead Dev ! 🚀 #flutter",
        )
        assert result is not None
        assert result["kind"] == "tweet_draft"

    def test_linkedin_detected(self) -> None:
        result = detect_rich_content(
            user_message="Rédige un post LinkedIn pour annoncer ma promotion",
            assistant_text=(
                "Aujourd'hui je suis fier d'annoncer ma promotion. "
                "Un grand merci à l'équipe. Hâte de continuer cette aventure ! "
                "#flutter #africa"
            ),
        )
        assert result is not None
        assert result["kind"] == "linkedin_post_draft"

    def test_document_detected_with_formal_letter(self) -> None:
        result = detect_rich_content(
            user_message="Rédige une lettre formelle au maire de Yaoundé",
            assistant_text=(
                "Objet : Demande d'acte de naissance\n\n"
                "Madame la Maire de Yaoundé,\n\n"
                "Je soussigné, Loth Ivan Ngassa Yimga, ai l'honneur de solliciter de votre "
                "haute bienveillance la délivrance d'un acte de naissance.\n\n"
                "Né le 1er janvier 1990 à Yaoundé, je joins à la présente les pièces "
                "justificatives habituelles.\n\n"
                "Dans l'attente de votre réponse, je vous prie d'agréer, Madame la Maire, "
                "l'expression de mes salutations distinguées.\n\n"
                "Loth Ivan"
            ),
        )
        assert result is not None
        assert result["kind"] == "document_draft"

    def test_document_detected_with_course_intent(self) -> None:
        body = (
            "# Les boucles for en Python\n\n"
            "Une boucle `for` permet d'itérer.\n\n"
            "## Syntaxe\n\n```python\nfor item in iterable:\n    print(item)\n```\n\n"
            "## Exemples\n\nIteration sur une liste, range, dict...\n\n"
        ) * 5
        result = detect_rich_content(
            user_message="Génère un cours détaillé sur les boucles for en Python",
            assistant_text=body,
        )
        assert result is not None
        assert result["kind"] == "document_draft"

    def test_email_priority_over_document_on_short_email(self) -> None:
        # Email body short (~500 chars) + email intent → email wins, document
        # ne match pas car body trop court pour son seuil 500 chars
        result = detect_rich_content(
            user_message="Rédige un mail de relance",
            assistant_text=(
                "Sujet : Relance livraison\n\n"
                "Bonjour Marie,\n\n"
                "Je me permets de vous relancer.\n\n"
                "Cordialement,\nIvan"
            ),
        )
        assert result is not None
        assert result["kind"] == "email_draft"


class TestDetectRichContentCascadeC46:
    """Tests de la cascade C4.6 — Code Project + Code File ajoutés en tête.

    Ordre attendu : code_project → code_file → whatsapp → sms → tweet
    → linkedin → document → email.
    """

    def test_code_project_priority_over_code_file_when_multi_blocks(self) -> None:
        # 3 blocs Python nommés → Code Project capture, Code File skip.
        assistant_text = (
            "**main.py**\n```python\nfrom fastapi import FastAPI\napp = FastAPI()\n```\n\n"
            "**routes.py**\n```python\nfrom fastapi import APIRouter\nrouter = APIRouter()\n```\n\n"
            "**requirements.txt**\n```text\nfastapi==0.100.0\n```"
        )
        result = detect_rich_content(
            user_message="écris-moi une API FastAPI complète",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["kind"] == "code_project_draft"
        assert len(result["data"]["files"]) == 3

    def test_code_file_detected_when_single_block(self) -> None:
        # 1 SEUL bloc → Code File capture (Code Project skip cap min 2).
        assistant_text = (
            "Voici une implémentation récursive :\n\n"
            "**fibonacci.py**\n```python\n"
            "def fib(n):\n    return n if n < 2 else fib(n-1) + fib(n-2)\n"
            "```\n\n"
            "Tu peux l'utiliser comme `fib(10)`."
        )
        result = detect_rich_content(
            user_message="écris-moi un script Python qui calcule fibonacci",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["kind"] == "code_file_draft"
        assert result["data"]["filename"] == "fibonacci.py"

    def test_code_file_priority_over_email_when_code_block_present(self) -> None:
        # 1 bloc de code + body email-looking → Code File wins (cascade
        # priority sur les markers email semi-formels).
        assistant_text = (
            "Bonjour Ivan,\n\n"
            "Voici la fonction demandée :\n\n"
            "```python\n"
            "def validate_email(addr):\n    return '@' in addr and '.' in addr\n"
            "```\n\n"
            "Cordialement,\nNEXYA"
        )
        result = detect_rich_content(
            user_message="code-moi une fonction de validation email",
            assistant_text=assistant_text,
        )
        assert result is not None
        # Code File wins même si markers email présents.
        assert result["kind"] == "code_file_draft"

    def test_code_project_priority_over_document_when_multi_code_blocks(self) -> None:
        # 3+ blocs de code nommés + intent fort projet → Code Project,
        # PAS Document (même si le body est long > 500 chars).
        assistant_text = (
            "Voici une API Python complète pour gérer des tâches.\n\n"
            "**main.py**\n```python\n"
            "from fastapi import FastAPI\nfrom routes import router\n"
            "app = FastAPI()\napp.include_router(router, prefix='/tasks')\n"
            "```\n\n"
            "**routes.py**\n```python\n"
            "from fastapi import APIRouter\nrouter = APIRouter()\n\n"
            "@router.get('/')\ndef list_tasks(): return []\n"
            "```\n\n"
            "**requirements.txt**\n```text\nfastapi==0.100.0\nuvicorn==0.20.0\n```"
        )
        result = detect_rich_content(
            user_message="écris-moi une API FastAPI complète pour gérer des tâches",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["kind"] == "code_project_draft"

    def test_no_code_block_falls_through_to_email_when_email_intent(self) -> None:
        # Intent EMAIL clair + body sans bloc de code → Code Project +
        # Code File skip (pas de bloc) → cascade descend jusqu'à Email
        # qui capture.
        assistant_text = (
            "Sujet : Rappel facture\n\n"
            "Bonjour Marie,\n\n"
            "Je me permets de vous relancer concernant la facture en attente.\n\n"
            "Cordialement,\nIvan"
        )
        result = detect_rich_content(
            user_message="Rédige un mail à Marie pour rappel facture",
            assistant_text=assistant_text,
        )
        # Pas de bloc → Code File/Project skip. Email intent + markers
        # → email_draft capture.
        assert result is not None
        assert result["kind"] == "email_draft"

    def test_code_file_with_subdir_filename(self) -> None:
        # Filename avec sous-dossier `src/utils/parser.py` → préservé.
        assistant_text = (
            "src/utils/parser.py\n"
            "```python\n"
            "def parse(s):\n    return s.strip().lower()\n"
            "```"
        )
        result = detect_rich_content(
            user_message="parse helper",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["kind"] == "code_file_draft"
        assert result["data"]["filename"] == "src/utils/parser.py"

    def test_payload_dict_structure_for_code_kinds(self) -> None:
        # Vérifie que les payloads code_* ont bien la structure dict
        # attendue par DraftPayload.tryFromMetadata côté Flutter.
        # Code File
        cf = detect_rich_content(
            user_message="code",
            assistant_text=(
                "**main.py**\n```python\n"
                "def hello():\n    print('hello world from NEXYA')\n"
                "```"
            ),
        )
        assert cf is not None
        assert isinstance(cf, dict)
        assert cf["kind"] == "code_file_draft"
        assert "filename" in cf["data"]
        assert "content" in cf["data"]
        assert "language" in cf["data"]
        # Code Project
        cp = detect_rich_content(
            user_message="écris une API complète",
            assistant_text=(
                "**main.py**\n```python\nfrom fastapi import FastAPI\napp = FastAPI()\n```\n\n"
                "**routes.py**\n```python\nfrom fastapi import APIRouter\nrouter = APIRouter()\n```"
            ),
        )
        assert cp is not None
        assert isinstance(cp, dict)
        assert cp["kind"] == "code_project_draft"
        assert "project_name" in cp["data"]
        assert "files" in cp["data"]
        assert isinstance(cp["data"]["files"], list)
