"""Tests `RichContentPayload` Pydantic schemas (C4.4 + C4.5)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.features.rich_content.schemas import (
    CodeFileDraftData,
    CodeProjectDraftData,
    CodeProjectFileItem,
    DocumentDraftData,
    EmailDraftData,
    LinkedInPostDraftData,
    RichContentPayload,
    SmsDraftData,
    TweetDraftData,
    WhatsAppDraftData,
    _validate_filename_path_safe,
)


class TestEmailDraftData:
    def test_minimal_payload(self) -> None:
        data = EmailDraftData(body="Bonjour, ceci est un email.")
        assert data.subject is None
        assert data.to is None
        assert data.body == "Bonjour, ceci est un email."

    def test_full_payload(self) -> None:
        data = EmailDraftData(
            subject="Relance livraison",
            body="Bonjour Marie, ...",
            to="marie@example.com",
        )
        assert data.subject == "Relance livraison"
        assert data.to == "marie@example.com"

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EmailDraftData(body="")

    def test_body_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EmailDraftData(body="x" * 10_001)

    def test_subject_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EmailDraftData(subject="x" * 301, body="hi")

    def test_to_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            EmailDraftData(to="x" * 321, body="hi")

    def test_subject_whitespace_normalized_to_none(self) -> None:
        data = EmailDraftData(subject="   ", body="hi")
        assert data.subject is None

    def test_to_whitespace_normalized_to_none(self) -> None:
        data = EmailDraftData(to="   ", body="hi")
        assert data.to is None


class TestWhatsAppDraftData:
    def test_minimal_payload(self) -> None:
        data = WhatsAppDraftData(body="Salut Marie !")
        assert data.phone is None
        assert data.body == "Salut Marie !"

    def test_full_payload(self) -> None:
        data = WhatsAppDraftData(phone="+237698765432", body="Salut !")
        assert data.phone == "+237698765432"

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WhatsAppDraftData(body="")

    def test_body_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WhatsAppDraftData(body="x" * 10_001)

    def test_phone_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            WhatsAppDraftData(phone="x" * 21, body="hi")

    def test_phone_whitespace_normalized_to_none(self) -> None:
        data = WhatsAppDraftData(phone="   ", body="hi")
        assert data.phone is None


class TestRichContentPayload:
    def test_email_factory(self) -> None:
        payload = RichContentPayload.email(
            subject="Test",
            body="Hello",
            to="test@example.com",
        )
        assert payload.kind == "email_draft"
        assert payload.data["subject"] == "Test"
        assert payload.data["body"] == "Hello"
        assert payload.data["to"] == "test@example.com"

    def test_email_factory_minimal(self) -> None:
        payload = RichContentPayload.email(subject=None, body="Hello")
        assert payload.kind == "email_draft"
        assert payload.data["subject"] is None
        assert payload.data["to"] is None

    def test_whatsapp_factory(self) -> None:
        payload = RichContentPayload.whatsapp(phone="+237698765432", body="Hi")
        assert payload.kind == "whatsapp_draft"
        assert payload.data["phone"] == "+237698765432"
        assert payload.data["body"] == "Hi"

    def test_whatsapp_factory_minimal(self) -> None:
        payload = RichContentPayload.whatsapp(phone=None, body="Hi")
        assert payload.kind == "whatsapp_draft"
        assert payload.data["phone"] is None

    def test_email_factory_validates_caps(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.email(subject=None, body="x" * 10_001)

    def test_whatsapp_factory_validates_caps(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.whatsapp(phone=None, body="x" * 10_001)

    def test_model_dump_serialization(self) -> None:
        payload = RichContentPayload.email(subject="Hi", body="Hello")
        dumped = payload.model_dump()
        assert dumped["kind"] == "email_draft"
        assert dumped["data"]["subject"] == "Hi"
        assert dumped["data"]["body"] == "Hello"


# ──────────────────────────────────────────────────────────────────────
# C4.5 — Nouveaux schémas (SMS / LinkedIn / Tweet / Document)
# ──────────────────────────────────────────────────────────────────────


class TestSmsDraftData:
    def test_minimal_payload(self) -> None:
        data = SmsDraftData(body="Salut Marie !")
        assert data.phone is None
        assert data.body == "Salut Marie !"

    def test_full_payload(self) -> None:
        data = SmsDraftData(phone="+237698765432", body="Salut !")
        assert data.phone == "+237698765432"

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SmsDraftData(body="")

    def test_body_too_long_rejected(self) -> None:
        # SMS cap = 1600 chars
        with pytest.raises(ValidationError):
            SmsDraftData(body="x" * 1_601)

    def test_body_at_cap_accepted(self) -> None:
        data = SmsDraftData(body="x" * 1_600)
        assert len(data.body) == 1_600

    def test_phone_whitespace_normalized_to_none(self) -> None:
        data = SmsDraftData(phone="   ", body="hi")
        assert data.phone is None


class TestLinkedInPostDraftData:
    def test_minimal_payload(self) -> None:
        data = LinkedInPostDraftData(body="Hello LinkedIn !")
        assert data.body == "Hello LinkedIn !"

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            LinkedInPostDraftData(body="")

    def test_body_too_long_rejected(self) -> None:
        # LinkedIn cap = 3000 chars
        with pytest.raises(ValidationError):
            LinkedInPostDraftData(body="x" * 3_001)

    def test_body_at_cap_accepted(self) -> None:
        data = LinkedInPostDraftData(body="x" * 3_000)
        assert len(data.body) == 3_000


class TestTweetDraftData:
    def test_minimal_payload(self) -> None:
        data = TweetDraftData(body="Hello world !")
        assert data.body == "Hello world !"

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            TweetDraftData(body="")

    def test_body_too_long_rejected(self) -> None:
        # Tweet cap = 280 chars (limite officielle Twitter/X)
        with pytest.raises(ValidationError):
            TweetDraftData(body="x" * 281)

    def test_body_at_cap_accepted(self) -> None:
        data = TweetDraftData(body="x" * 280)
        assert len(data.body) == 280


class TestDocumentDraftData:
    def test_minimal_payload(self) -> None:
        data = DocumentDraftData(body="Contenu du document.")
        assert data.title is None
        assert data.recipient is None
        assert data.body == "Contenu du document."

    def test_full_payload(self) -> None:
        data = DocumentDraftData(
            title="Demande d'acte de naissance",
            body="Madame la Maire, ...",
            recipient="Madame la Maire de Yaoundé",
        )
        assert data.title == "Demande d'acte de naissance"
        assert data.recipient == "Madame la Maire de Yaoundé"

    def test_empty_body_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DocumentDraftData(body="")

    def test_body_too_long_rejected(self) -> None:
        # Document cap = 50000 chars
        with pytest.raises(ValidationError):
            DocumentDraftData(body="x" * 50_001)

    def test_body_at_cap_accepted(self) -> None:
        data = DocumentDraftData(body="x" * 50_000)
        assert len(data.body) == 50_000

    def test_title_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DocumentDraftData(title="x" * 301, body="hi")

    def test_recipient_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            DocumentDraftData(recipient="x" * 201, body="hi")

    def test_title_whitespace_normalized_to_none(self) -> None:
        data = DocumentDraftData(title="   ", body="hi")
        assert data.title is None

    def test_recipient_whitespace_normalized_to_none(self) -> None:
        data = DocumentDraftData(recipient="   ", body="hi")
        assert data.recipient is None


class TestRichContentPayloadFactoriesC45:
    """Factories C4.5 (sms / linkedin_post / tweet / document)."""

    def test_sms_factory(self) -> None:
        payload = RichContentPayload.sms(phone="+237698765432", body="Hi")
        assert payload.kind == "sms_draft"
        assert payload.data["phone"] == "+237698765432"
        assert payload.data["body"] == "Hi"

    def test_sms_factory_minimal(self) -> None:
        payload = RichContentPayload.sms(phone=None, body="Hi")
        assert payload.kind == "sms_draft"
        assert payload.data["phone"] is None

    def test_linkedin_factory(self) -> None:
        payload = RichContentPayload.linkedin_post(body="Hello LinkedIn !")
        assert payload.kind == "linkedin_post_draft"
        assert payload.data["body"] == "Hello LinkedIn !"

    def test_tweet_factory(self) -> None:
        payload = RichContentPayload.tweet(body="Hello world !")
        assert payload.kind == "tweet_draft"
        assert payload.data["body"] == "Hello world !"

    def test_document_factory(self) -> None:
        payload = RichContentPayload.document(
            title="Demande d'acte",
            body="Madame la Maire, ...",
            recipient="Madame la Maire",
        )
        assert payload.kind == "document_draft"
        assert payload.data["title"] == "Demande d'acte"
        assert payload.data["recipient"] == "Madame la Maire"
        assert payload.data["body"] == "Madame la Maire, ..."

    def test_document_factory_minimal(self) -> None:
        payload = RichContentPayload.document(title=None, body="Contenu")
        assert payload.kind == "document_draft"
        assert payload.data["title"] is None
        assert payload.data["recipient"] is None

    def test_sms_factory_validates_caps(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.sms(phone=None, body="x" * 1_601)

    def test_linkedin_factory_validates_caps(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.linkedin_post(body="x" * 3_001)

    def test_tweet_factory_validates_caps(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.tweet(body="x" * 281)

    def test_document_factory_validates_caps(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.document(title=None, body="x" * 50_001)


# ══════════════════════════════════════════════════════════════════════
# C4.6 — Tests Code File + Code Project + helper path-safe
# ══════════════════════════════════════════════════════════════════════


class TestValidateFilenamePathSafe:
    """Helper sécurité partagé Code File + Code Project."""

    @pytest.mark.parametrize(
        "valid_filename",
        [
            "main.py",
            "fibonacci.py",
            "routes/users.py",
            "tests/test_main.py",
            "src/components/Button.tsx",
            "src/components/Button.dart",
            "deeply/nested/folder/file.go",
            "fichier-français.py",  # Unicode autorisé
            "  main.py  ",  # whitespace strippé
        ],
    )
    def test_valid_filenames_pass(self, valid_filename: str) -> None:
        result = _validate_filename_path_safe(valid_filename)
        assert result == valid_filename.strip()

    @pytest.mark.parametrize(
        "windows_filename, expected",
        [
            ("src\\components\\Button.dart", "src/components/Button.dart"),
            ("tests\\unit\\foo.py", "tests/unit/foo.py"),
        ],
    )
    def test_windows_separators_normalized_to_unix(
        self, windows_filename: str, expected: str
    ) -> None:
        result = _validate_filename_path_safe(windows_filename)
        assert result == expected

    @pytest.mark.parametrize(
        "malicious",
        [
            "../etc/passwd",
            "../../etc/passwd",
            "..\\..\\Windows\\System32\\foo.txt",
            "src/../../../etc/passwd",
            "foo/..",
            "foo/../bar",
        ],
    )
    def test_path_traversal_rejected(self, malicious: str) -> None:
        with pytest.raises(ValueError, match="motif interdit"):
            _validate_filename_path_safe(malicious)

    @pytest.mark.parametrize(
        "absolute",
        [
            "/etc/passwd",
            "/foo/bar.py",
            "\\foo\\bar.py",
            "C:\\Windows\\System32\\foo.txt",
            "D:/projects/foo.py",
        ],
    )
    def test_absolute_paths_rejected(self, absolute: str) -> None:
        with pytest.raises(ValueError, match="motif interdit"):
            _validate_filename_path_safe(absolute)

    @pytest.mark.parametrize(
        "home_expansion",
        [
            "~/foo.py",
            "~root/.ssh/id_rsa",
        ],
    )
    def test_home_expansion_rejected(self, home_expansion: str) -> None:
        with pytest.raises(ValueError, match="motif interdit"):
            _validate_filename_path_safe(home_expansion)

    @pytest.mark.parametrize(
        "control_char",
        [
            "foo\x00.py",  # null byte
            "foo\x01.py",
            "foo\nbar.py",  # newline
            "foo\rbar.py",  # carriage return
            "foo\tbar.py",  # tab
        ],
    )
    def test_control_chars_rejected(self, control_char: str) -> None:
        with pytest.raises(ValueError, match="motif interdit"):
            _validate_filename_path_safe(control_char)

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="vide"):
            _validate_filename_path_safe("")
        with pytest.raises(ValueError, match="vide"):
            _validate_filename_path_safe("   ")


class TestCodeFileDraftData:
    """Schéma Pydantic Code File (C4.6)."""

    def test_minimal_payload(self) -> None:
        data = CodeFileDraftData(
            filename="main.py",
            content="print('hello')",
            language="python",
        )
        assert data.filename == "main.py"
        assert data.content == "print('hello')"
        assert data.language == "python"
        assert data.description is None

    def test_full_payload(self) -> None:
        data = CodeFileDraftData(
            filename="fibonacci.py",
            content="def fib(n):\n    return n if n < 2 else fib(n-1) + fib(n-2)",
            language="Python",  # case insensitive normalization
            description="Implémentation récursive",
        )
        assert data.language == "python"  # normalisé lowercase
        assert data.description == "Implémentation récursive"

    def test_language_normalized_lowercase(self) -> None:
        data = CodeFileDraftData(
            filename="App.tsx", content="export const App = () => null;", language="TypeScript"
        )
        assert data.language == "typescript"

    def test_filename_path_traversal_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CodeFileDraftData(
                filename="../../../etc/passwd",
                content="evil",
                language="bash",
            )

    def test_filename_absolute_path_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CodeFileDraftData(filename="/etc/passwd", content="evil", language="bash")

    def test_empty_content_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CodeFileDraftData(filename="empty.py", content="", language="python")

    def test_content_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CodeFileDraftData(
                filename="big.py", content="x" * 100_001, language="python"
            )

    def test_filename_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CodeFileDraftData(
                filename="x" * 201 + ".py", content="hi", language="python"
            )

    def test_language_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CodeFileDraftData(
                filename="foo.py", content="hi", language="x" * 33
            )

    def test_language_invalid_chars_rejected(self) -> None:
        # Espace au milieu interdit
        with pytest.raises(ValidationError):
            CodeFileDraftData(filename="foo.py", content="hi", language="python script")
        # Caractère spécial interdit
        with pytest.raises(ValidationError):
            CodeFileDraftData(filename="foo.py", content="hi", language="py@thon")

    def test_language_special_alphanumeric_accepted(self) -> None:
        # `c++`, `objective-c`, `f#` (sauf `#` interdit), test cas réels
        data = CodeFileDraftData(filename="main.cpp", content="int main(){}", language="c++")
        assert data.language == "c++"
        data = CodeFileDraftData(
            filename="foo.m", content="int main(){}", language="objective-c"
        )
        assert data.language == "objective-c"

    def test_description_whitespace_normalized_to_none(self) -> None:
        data = CodeFileDraftData(
            filename="foo.py", content="hi", language="python", description="   "
        )
        assert data.description is None

    def test_description_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CodeFileDraftData(
                filename="foo.py", content="hi", language="python", description="x" * 501
            )


class TestCodeProjectFileItem:
    """Item fichier dans un Code Project (C4.6)."""

    def test_minimal_payload(self) -> None:
        item = CodeProjectFileItem(
            filename="main.py", content="print('hi')", language="python"
        )
        assert item.filename == "main.py"
        assert item.language == "python"

    def test_filename_path_traversal_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CodeProjectFileItem(
                filename="../etc/passwd", content="evil", language="bash"
            )

    def test_content_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CodeProjectFileItem(
                filename="foo.py", content="x" * 100_001, language="python"
            )

    def test_language_normalized_lowercase(self) -> None:
        item = CodeProjectFileItem(
            filename="App.tsx", content="export {};", language="TypeScript"
        )
        assert item.language == "typescript"


class TestCodeProjectDraftData:
    """Schéma Pydantic Code Project multi-fichiers (C4.6)."""

    def _make_file(
        self, filename: str = "main.py", content: str = "print('hi')", language: str = "python"
    ) -> CodeProjectFileItem:
        return CodeProjectFileItem(filename=filename, content=content, language=language)

    def test_minimal_payload_2_files(self) -> None:
        data = CodeProjectDraftData(
            project_name="My API",
            files=[
                self._make_file("main.py", "from fastapi import FastAPI\napp = FastAPI()"),
                self._make_file("requirements.txt", "fastapi==0.100.0", "text"),
            ],
        )
        assert data.project_name == "My API"
        assert len(data.files) == 2
        assert data.description is None
        assert data.project_type is None

    def test_full_payload(self) -> None:
        data = CodeProjectDraftData(
            project_name="FastAPI Tasks",
            description="API simple pour gérer des tâches",
            files=[
                self._make_file("main.py", "app = FastAPI()", "python"),
                self._make_file("routes.py", "router = APIRouter()", "python"),
                self._make_file("models.py", "class Task(BaseModel): pass", "python"),
                self._make_file("requirements.txt", "fastapi==0.100.0", "text"),
            ],
            project_type="python",
        )
        assert data.project_type == "python"
        assert len(data.files) == 4

    def test_max_50_files_accepted(self) -> None:
        files = [self._make_file(f"file_{i}.py", f"# file {i}", "python") for i in range(50)]
        data = CodeProjectDraftData(project_name="Big Project", files=files)
        assert len(data.files) == 50

    def test_51_files_rejected(self) -> None:
        files = [self._make_file(f"file_{i}.py", f"# file {i}", "python") for i in range(51)]
        with pytest.raises(ValidationError):
            CodeProjectDraftData(project_name="Too Big", files=files)

    def test_single_file_rejected(self) -> None:
        # Cap min 2 — un seul fichier devrait être un code_file_draft.
        with pytest.raises(ValidationError):
            CodeProjectDraftData(project_name="Solo", files=[self._make_file()])

    def test_empty_files_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CodeProjectDraftData(project_name="Empty", files=[])

    def test_total_size_max_legal_accepted(self) -> None:
        # Cas max légal : 50 fichiers × 100k chars chacun = 5 MB exact.
        # Avec les caps actuels (50 fichiers max + 100k chars/fichier max),
        # c'est mathématiquement le maximum possible — le validator
        # total-size reste comme défense en profondeur si on élargit
        # un jour les caps individuels.
        big_content = "x" * 100_000  # cap individuel max
        files = [
            self._make_file(f"f_{i}.py", big_content, "python") for i in range(50)
        ]
        data = CodeProjectDraftData(project_name="Max Legal", files=files)
        assert len(data.files) == 50
        # Total = 5 MB exact, donc <= cap 5_000_000.
        assert sum(len(f.content) for f in data.files) == 5_000_000

    def test_duplicate_filenames_rejected(self) -> None:
        with pytest.raises(ValidationError, match="dupliqué"):
            CodeProjectDraftData(
                project_name="Dup",
                files=[
                    self._make_file("main.py", "print(1)", "python"),
                    self._make_file("main.py", "print(2)", "python"),
                ],
            )

    def test_duplicate_filenames_after_normalization_rejected(self) -> None:
        # Windows backslash normalisé en `/` — donc `src\\foo.py` et
        # `src/foo.py` collisionnent.
        with pytest.raises(ValidationError, match="dupliqué"):
            CodeProjectDraftData(
                project_name="Dup Normalized",
                files=[
                    self._make_file("src/foo.py", "print(1)", "python"),
                    self._make_file("src\\foo.py", "print(2)", "python"),
                ],
            )

    def test_empty_project_name_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CodeProjectDraftData(
                project_name="   ",
                files=[self._make_file(), self._make_file("b.py", "print(2)", "python")],
            )

    def test_project_name_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CodeProjectDraftData(
                project_name="x" * 101,
                files=[self._make_file(), self._make_file("b.py", "print(2)", "python")],
            )

    def test_description_too_long_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CodeProjectDraftData(
                project_name="OK",
                description="x" * 1_001,
                files=[self._make_file(), self._make_file("b.py", "print(2)", "python")],
            )

    def test_project_type_normalized_to_none_if_empty(self) -> None:
        data = CodeProjectDraftData(
            project_name="OK",
            project_type="   ",
            files=[self._make_file(), self._make_file("b.py", "print(2)", "python")],
        )
        assert data.project_type is None


class TestRichContentPayloadCodeFactories:
    """Factories `RichContentPayload.code_file()` + `code_project()`."""

    def test_code_file_factory(self) -> None:
        payload = RichContentPayload.code_file(
            filename="fibonacci.py",
            content="def fib(n): pass",
            language="python",
            description="Implémentation récursive",
        )
        assert payload.kind == "code_file_draft"
        assert payload.data["filename"] == "fibonacci.py"
        assert payload.data["language"] == "python"
        assert payload.data["description"] == "Implémentation récursive"

    def test_code_file_factory_minimal(self) -> None:
        payload = RichContentPayload.code_file(
            filename="main.py", content="print('hi')", language="python"
        )
        assert payload.kind == "code_file_draft"
        assert payload.data["description"] is None

    def test_code_file_factory_validates_path_safe(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.code_file(
                filename="../etc/passwd", content="evil", language="bash"
            )

    def test_code_file_factory_validates_content_cap(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.code_file(
                filename="big.py", content="x" * 100_001, language="python"
            )

    def test_code_project_factory_with_dicts(self) -> None:
        payload = RichContentPayload.code_project(
            project_name="My API",
            files=[
                {"filename": "main.py", "content": "app = FastAPI()", "language": "python"},
                {"filename": "models.py", "content": "class T: pass", "language": "python"},
            ],
        )
        assert payload.kind == "code_project_draft"
        assert payload.data["project_name"] == "My API"
        assert len(payload.data["files"]) == 2

    def test_code_project_factory_with_items(self) -> None:
        files = [
            CodeProjectFileItem(filename="a.py", content="print(1)", language="python"),
            CodeProjectFileItem(filename="b.py", content="print(2)", language="python"),
        ]
        payload = RichContentPayload.code_project(
            project_name="Two Files",
            files=files,
            description="Description",
            project_type="python",
        )
        assert payload.kind == "code_project_draft"
        assert payload.data["description"] == "Description"
        assert payload.data["project_type"] == "python"

    def test_code_project_factory_validates_cap_min_2(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.code_project(
                project_name="Solo",
                files=[{"filename": "main.py", "content": "hi", "language": "python"}],
            )

    def test_code_project_factory_validates_cap_max_50(self) -> None:
        files = [
            {"filename": f"f_{i}.py", "content": f"# {i}", "language": "python"}
            for i in range(51)
        ]
        with pytest.raises(ValidationError):
            RichContentPayload.code_project(project_name="Too Big", files=files)

    def test_code_project_factory_validates_path_safe(self) -> None:
        with pytest.raises(ValidationError):
            RichContentPayload.code_project(
                project_name="Evil",
                files=[
                    {"filename": "main.py", "content": "ok", "language": "python"},
                    {"filename": "../evil.sh", "content": "rm -rf /", "language": "bash"},
                ],
            )

    def test_code_project_factory_serializes_files_to_dict(self) -> None:
        # Vérifie que CodeProjectFileItem est bien sérialisé en dict
        # dans le payload.data (sinon JSON storage casserait).
        payload = RichContentPayload.code_project(
            project_name="Serialization Check",
            files=[
                CodeProjectFileItem(filename="a.py", content="hi", language="python"),
                CodeProjectFileItem(filename="b.py", content="hi2", language="python"),
            ],
        )
        # Les items doivent être des dicts, pas des CodeProjectFileItem.
        assert isinstance(payload.data["files"], list)
        assert all(isinstance(f, dict) for f in payload.data["files"])
        assert payload.data["files"][0]["filename"] == "a.py"
