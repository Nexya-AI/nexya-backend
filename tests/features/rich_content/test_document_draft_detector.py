"""Tests `document_draft_detector` (C4.5)."""

from __future__ import annotations

import pytest

from app.features.rich_content.document_draft_detector import (
    detect_document_intent,
    detect_rich_content_document,
)


class TestDetectDocumentIntent:
    """Intent classifier — message user upstream."""

    @pytest.mark.parametrize(
        "user_message",
        [
            "Rédige une lettre formelle au maire de Yaoundé",
            "écris-moi un courrier officiel pour la mairie",
            "Génère un rapport de réunion",
            "Crée un compte-rendu de stage",
            "Génère un cours détaillé sur les boucles for en Python",
            "Rédige un discours pour mon mariage",
            "Produis un document PDF officiel",
            "Génère-moi un PDF avec tout le plan",
            "Lettre à mon employeur pour démissionner",
            "Écris un tutoriel détaillé sur Flutter",
        ],
    )
    def test_fr_intent_detected(self, user_message: str) -> None:
        assert detect_document_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            "Write a formal letter to the mayor",
            "draft a detailed report on the meeting",
            "compose a formal speech",
            "generate a long document about Python",
            "create a PDF with the full plan",
            "letter to my employer",
        ],
    )
    def test_en_intent_detected(self, user_message: str) -> None:
        assert detect_document_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            "Comment écrire une lettre formelle ?",
            "Explique-moi en quelques lignes",
            "Résume ce texte",
            "Comment fonctionne un PDF ?",
            "How does PDF work?",
            "",
            "   ",
        ],
    )
    def test_no_intent(self, user_message: str) -> None:
        assert detect_document_intent(user_message) is False

    def test_none_input(self) -> None:
        assert detect_document_intent(None) is False  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "user_message",
        [
            # Fix 2026-06-10 — intent FR élargi : document nu + synthèse + note
            # + exposé + fiche + dissertation + essai + analyse + résumé +
            # procédure + mode d'emploi.
            "Génère un document sur les fonctions mathématiques",
            "Fais-moi une synthèse de 5 pages sur la photosynthèse",
            "Rédige une note de réunion pour l'équipe",
            "Écris un exposé sur la Révolution française",
            "Rédige une fiche de révision sur les intégrales",
            "Rédige une dissertation sur la liberté",
            "Écris-moi un essai sur l'intelligence artificielle",
            "Génère une analyse détaillée du marché camerounais",
            "Rédige un résumé structuré de ce chapitre",
            "Rédige une procédure d'installation pas à pas",
            "Écris un mode d'emploi pour cette machine",
        ],
    )
    def test_fr_intent_broadened(self, user_message: str) -> None:
        assert detect_document_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            # Fix 2026-06-10 — intent EN élargi : summary/essay/analysis/
            # procedure/course/tutorial/guide (en plus de document/report/letter).
            "Write a summary about climate change",
            "Draft an essay on artificial intelligence",
            "Generate an analysis of the African market",
            "Write a procedure for the installation",
            "Generate a course on Python loops",
        ],
    )
    def test_en_intent_broadened(self, user_message: str) -> None:
        assert detect_document_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            # Fix 2026-06-10 — la méta-question ne doit JAMAIS matcher, même
            # avec les nouveaux mots-clés.
            "Qu'est-ce qu'une synthèse ?",
            "Comment rédiger une dissertation ?",
            "C'est quoi un mode d'emploi ?",
            "What is an essay?",
        ],
    )
    def test_meta_questions_still_none_after_broadening(self, user_message: str) -> None:
        assert detect_document_intent(user_message) is False


class TestDetectRichContentDocument:
    """Point d'entrée — combine intent + body formel."""

    def test_intent_with_formal_letter_body(self) -> None:
        result = detect_rich_content_document(
            user_message="Rédige une lettre formelle au maire de Yaoundé pour demander un acte de naissance",
            assistant_text=(
                "Objet : Demande d'acte de naissance\n\n"
                "Madame la Maire de Yaoundé,\n\n"
                "Je soussigné, Loth Ivan Ngassa Yimga, ai l'honneur de solliciter de votre haute "
                "bienveillance la délivrance d'un acte de naissance.\n\n"
                "Né le 1er janvier 1990 à Yaoundé, je joins à la présente les pièces justificatives "
                "habituelles.\n\n"
                "Dans l'attente de votre réponse favorable, je vous prie d'agréer, Madame la Maire, "
                "l'expression de mes salutations distinguées.\n\n"
                "Loth Ivan Ngassa Yimga"
            ),
        )
        assert result is not None
        assert result["kind"] == "document_draft"
        assert "Madame la Maire" in result["data"]["body"]
        # recipient extracted
        assert result["data"]["recipient"] is not None
        assert "Madame la Maire" in result["data"]["recipient"]
        # title (Objet) extracted
        assert result["data"]["title"] == "Demande d'acte de naissance"

    def test_intent_with_course_body_no_formal_markers(self) -> None:
        # Cours/tutoriel : pas de "Madame/Monsieur" mais intent explicite
        body = (
            "# Les boucles for en Python\n\n"
            "Une boucle `for` permet d'itérer sur une séquence.\n\n"
            "## Syntaxe de base\n\n"
            "```python\nfor item in iterable:\n    print(item)\n```\n\n"
            "## Exemples concrets\n\n"
            "Iteration sur une liste, un range, un dictionnaire...\n\n"
            "## Pièges courants\n\nNe pas modifier la liste pendant l'itération.\n\n"
        ) * 5  # > 500 chars
        result = detect_rich_content_document(
            user_message="Génère un cours détaillé sur les boucles for en Python",
            assistant_text=body,
        )
        assert result is not None
        assert result["kind"] == "document_draft"
        # Pas de recipient (pas de "Madame/Monsieur")
        assert result["data"]["recipient"] is None
        assert "boucles for" in result["data"]["body"]

    def test_formal_body_without_intent_with_recipient_extracted(self) -> None:
        # Cas où l'user a dit « réponds à cette lettre » sans dire « rédige »
        # Le body est formel + recipient extrait → on flag.
        result = detect_rich_content_document(
            user_message="Voici la lettre reçue, aide-moi à y répondre",
            assistant_text=(
                "Monsieur le Directeur,\n\n"
                "Je vous remercie pour votre courrier du 15 mai dernier.\n\n"
                "Suite à votre demande, je vous prie de trouver ci-joint les pièces "
                "demandées. Je reste à votre disposition pour toute information complémentaire.\n\n"
                "Veuillez agréer, Monsieur le Directeur, l'expression de mes salutations "
                "distinguées.\n\n"
                "Loth Ivan Ngassa Yimga\n" * 3
            ),
        )
        assert result is not None
        assert result["kind"] == "document_draft"
        assert result["data"]["recipient"] is not None

    def test_no_intent_no_formal_body_returns_none(self) -> None:
        result = detect_rich_content_document(
            user_message="Quelle est la capitale du Cameroun ?",
            assistant_text="La capitale du Cameroun est Yaoundé. C'est une grande ville " * 20,
        )
        assert result is None

    def test_short_body_returns_none(self) -> None:
        # Tout document fait minimum 500 chars
        result = detect_rich_content_document(
            user_message="Rédige une lettre",
            assistant_text="Madame, voici ma demande. Cordialement.",
        )
        assert result is None

    def test_long_body_capped_at_50000(self) -> None:
        long_body = "# Cours Python\n\n" + ("Contenu du cours. " * 5_000)  # > 50k chars
        result = detect_rich_content_document(
            user_message="Génère un cours détaillé sur Python",
            assistant_text=long_body,
        )
        assert result is not None
        assert len(result["data"]["body"]) <= 50_000

    def test_empty_inputs_return_none(self) -> None:
        assert detect_rich_content_document("", "") is None
        assert detect_rich_content_document("Rédige une lettre", "") is None

    # ── Fix 2026-06-10 — seuil body abaissé 200 -> 120 ──────────────────

    def test_threshold_lowered_to_120_chars(self) -> None:
        # Une réponse de 121 chars avec intent explicite déclenche désormais la
        # carte (avant : seuil 200 -> aucune carte sous 200 chars).
        result = detect_rich_content_document(
            user_message="Génère un pdf sur les fonctions mathématiques",
            assistant_text="A" * 121,
        )
        assert result is not None
        assert result["kind"] == "document_draft"
        assert result["data"]["recipient"] is None

    def test_body_119_chars_returns_none(self) -> None:
        result = detect_rich_content_document(
            user_message="Génère un pdf sur les fonctions",
            assistant_text="A" * 119,
        )
        assert result is None

    def test_bare_document_intent_with_body(self) -> None:
        # « génère un document sur X » + body >= 120 -> carte (intent FR nu).
        body = (
            "Les fonctions mathématiques associent à chaque valeur d'entrée une "
            "valeur de sortie unique. On distingue les fonctions affines, "
            "polynomiales, exponentielles et logarithmiques."
        )
        result = detect_rich_content_document(
            user_message="Génère un document sur les fonctions mathématiques",
            assistant_text=body,
        )
        assert result is not None
        assert result["kind"] == "document_draft"

    # ── Fix 2026-06-10 — Cas D (long + très structuré, sans intent) ─────

    def test_case_d_long_structured_no_intent_no_markers(self) -> None:
        # Réponse longue + très structurée (>= 2 titres markdown + >= 3 items de
        # liste + >= 1500 chars) SANS intent ni markers formels -> carte
        # confiance basse (title=None, recipient=None).
        body = (
            "## Le cycle de l'eau\n\n"
            "Le cycle de l'eau décrit le mouvement continu de l'eau sur Terre.\n\n"
            "## Les étapes principales\n\n"
            "- Évaporation : l'eau des océans se transforme en vapeur.\n"
            "- Condensation : la vapeur d'eau forme les nuages.\n"
            "- Précipitation : l'eau retombe sous forme de pluie ou de neige.\n\n"
            + ("Ce processus naturel est essentiel à la vie sur la planète. " * 40)
        )
        result = detect_rich_content_document(
            user_message="Explique-moi le cycle de l'eau",
            assistant_text=body,
        )
        assert result is not None
        assert result["kind"] == "document_draft"
        assert result["data"]["title"] is None
        assert result["data"]["recipient"] is None

    def test_case_d_long_unstructured_returns_none(self) -> None:
        # Réponse longue mais SANS structure (ni titres ni listes) -> pas de
        # carte (anti-faux-positif sur un chat normal verbeux).
        result = detect_rich_content_document(
            user_message="Parle-moi du Cameroun",
            assistant_text="Le Cameroun est un pays d'Afrique centrale très divers. " * 40,
        )
        assert result is None

    def test_case_d_structured_but_short_returns_none(self) -> None:
        # Structuré (titres + listes) mais court (< 1500 chars) -> pas de carte
        # (Cas D délibérément conservateur).
        body = (
            "## Titre\n\n"
            "Une intro un peu développée pour dépasser le seuil minimal de 120 "
            "caractères sans pour autant atteindre le seuil Cas D.\n\n"
            "## Section\n\n"
            "- item un\n- item deux\n- item trois\n\n" + ("Texte de remplissage modéré. " * 8)
        )
        result = detect_rich_content_document(
            user_message="Donne-moi un aperçu rapide",
            assistant_text=body,
        )
        assert result is None
