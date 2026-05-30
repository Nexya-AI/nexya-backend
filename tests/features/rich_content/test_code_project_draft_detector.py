"""Tests `code_project_draft_detector` (C4.6)."""

from __future__ import annotations

import pytest

from app.features.rich_content.code_project_draft_detector import (
    _infer_project_name,
    _infer_project_type,
    detect_code_project_intent,
    detect_rich_content_code_project,
)


class TestDetectCodeProjectIntent:
    """Intent classifier — projet complet (FR + EN)."""

    @pytest.mark.parametrize(
        "user_message",
        [
            "écris-moi une API FastAPI complète",
            "code-moi un projet Flutter complet pour gérer des tâches",
            "développe une application web full-stack",
            "construis un projet complet en Python",
            "génère une API complète",
            "monte un backend complet pour mon e-commerce",
            "fais-moi un microservice complet",
            "API complète pour gérer des recettes",
            "build me a full-stack application",
            "create a complete API for managing tasks",
            "write a full project in Python",
            "build a microservice for user auth",
            "complete API for the recipe management",
            "develop a full-stack web app",
        ],
    )
    def test_intent_detected(self, user_message: str) -> None:
        assert detect_code_project_intent(user_message) is True

    @pytest.mark.parametrize(
        "user_message",
        [
            "écris-moi une fonction Python",
            "code-moi un script qui calcule fibonacci",
            "génère un widget Flutter",
            "explique-moi async/await en Python",
            "comment fonctionne FastAPI ?",
            "show me a simple function",
            "explain how Python async works",
            "",
            "   ",
        ],
    )
    def test_no_intent(self, user_message: str) -> None:
        assert detect_code_project_intent(user_message) is False

    def test_none_input(self) -> None:
        assert detect_code_project_intent(None) is False  # type: ignore[arg-type]


class TestInferProjectType:
    """Heuristique project_type depuis les filenames."""

    def test_python_via_requirements_txt(self) -> None:
        files = ["main.py", "requirements.txt", "routes.py"]
        assert _infer_project_type(files) == "python"

    def test_python_via_pyproject_toml(self) -> None:
        files = ["pyproject.toml", "src/main.py"]
        assert _infer_project_type(files) == "python"

    def test_nodejs_via_package_json(self) -> None:
        files = ["package.json", "index.js", "src/app.js"]
        assert _infer_project_type(files) == "nodejs"

    def test_flutter_via_pubspec_yaml(self) -> None:
        files = ["pubspec.yaml", "lib/main.dart"]
        assert _infer_project_type(files) == "flutter"

    def test_rust_via_cargo_toml(self) -> None:
        files = ["Cargo.toml", "src/main.rs"]
        assert _infer_project_type(files) == "rust"

    def test_go_via_go_mod(self) -> None:
        files = ["go.mod", "main.go"]
        assert _infer_project_type(files) == "go"

    def test_java_via_pom_xml(self) -> None:
        files = ["pom.xml", "src/main/java/App.java"]
        assert _infer_project_type(files) == "java"

    def test_java_via_build_gradle(self) -> None:
        files = ["build.gradle", "src/App.java"]
        assert _infer_project_type(files) == "java"

    def test_ruby_via_gemfile(self) -> None:
        files = ["Gemfile", "app.rb"]
        assert _infer_project_type(files) == "ruby"

    def test_php_via_composer_json(self) -> None:
        files = ["composer.json", "index.php"]
        assert _infer_project_type(files) == "php"

    def test_python_priority_over_nodejs_fullstack(self) -> None:
        # Cas full-stack Django + React : `requirements.txt` + `package.json`
        # → priorité Python (backend = source de vérité).
        files = ["requirements.txt", "package.json", "main.py", "src/App.jsx"]
        assert _infer_project_type(files) == "python"

    def test_basename_extraction_from_subdir(self) -> None:
        # `src/server/package.json` → reconnu comme `package.json`.
        files = ["src/server/package.json", "src/server/index.js"]
        assert _infer_project_type(files) == "nodejs"

    def test_case_insensitive(self) -> None:
        # `PACKAGE.JSON` → reconnu (peu probable mais robuste).
        files = ["PACKAGE.JSON", "INDEX.JS"]
        assert _infer_project_type(files) == "nodejs"

    def test_no_marker_returns_none(self) -> None:
        files = ["main.py", "utils.py"]  # pas de manifest
        assert _infer_project_type(files) is None

    def test_empty_returns_none(self) -> None:
        assert _infer_project_type([]) is None


class TestInferProjectName:
    """Inference du nom de projet depuis user_message + project_type."""

    def test_extracts_from_user_message_fr(self) -> None:
        result = _infer_project_name(
            "écris-moi une API FastAPI complète pour gérer des tâches",
            project_type="python",
        )
        assert "Api Fastapi" in result or "Api" in result

    def test_extracts_from_user_message_en(self) -> None:
        result = _infer_project_name(
            "build me a complete FastAPI app to manage tasks",
            project_type="python",
        )
        # Patterns FR/EN extraient le nom du projet en title case.
        assert result and len(result) <= 100

    def test_fallback_to_project_type(self) -> None:
        result = _infer_project_name("génère du code", project_type="python")
        assert result == "Python Project"

    def test_fallback_to_generic_when_no_type(self) -> None:
        result = _infer_project_name("génère du code", project_type=None)
        assert result == "Code Project"

    def test_fallback_when_empty_message(self) -> None:
        result = _infer_project_name("", project_type="flutter")
        assert result == "Flutter Project"

    def test_fallback_when_none_message(self) -> None:
        result = _infer_project_name(None, project_type=None)  # type: ignore[arg-type]
        assert result == "Code Project"


class TestDetectRichContentCodeProject:
    """Point d'entrée — détection multi-fichiers."""

    def _build_assistant_text(self, files: list[tuple[str, str, str]]) -> str:
        """Helper pour construire un assistant_text avec N blocs nommés.

        Args:
            files: liste de (filename, content, language).
        """
        parts = []
        for filename, content, language in files:
            parts.append(f"**{filename}**\n```{language}\n{content}\n```\n")
        return "\n".join(parts)

    def test_python_project_3_files_with_explicit_names(self) -> None:
        assistant_text = self._build_assistant_text([
            ("main.py", "from fastapi import FastAPI\napp = FastAPI()", "python"),
            ("routes.py", "from fastapi import APIRouter\nrouter = APIRouter()", "python"),
            ("requirements.txt", "fastapi==0.100.0\nuvicorn==0.20.0", "text"),
        ])
        result = detect_rich_content_code_project(
            user_message="écris-moi une API FastAPI complète pour gérer des tâches",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["kind"] == "code_project_draft"
        assert len(result["data"]["files"]) == 3
        assert result["data"]["project_type"] == "python"
        assert all(
            f["filename"] in {"main.py", "routes.py", "requirements.txt"}
            for f in result["data"]["files"]
        )

    def test_flutter_project_with_pubspec(self) -> None:
        assistant_text = self._build_assistant_text([
            ("pubspec.yaml", "name: my_app\nversion: 1.0.0\ndependencies:\n  flutter:\n    sdk: flutter", "yaml"),
            ("lib/main.dart", "import 'package:flutter/material.dart';\nvoid main() => runApp(MyApp());", "dart"),
            ("lib/widgets/login.dart", "class LoginScreen extends StatelessWidget {}", "dart"),
        ])
        result = detect_rich_content_code_project(
            user_message="génère un projet Flutter complet avec login",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["data"]["project_type"] == "flutter"

    def test_nodejs_project_with_package_json(self) -> None:
        assistant_text = self._build_assistant_text([
            ("package.json", '{"name": "my-app", "version": "1.0.0"}', "json"),
            ("index.js", "const express = require('express');\nconst app = express();", "javascript"),
            ("src/routes.js", "module.exports = (app) => { app.get('/', (req, res) => res.send('hi')); };", "javascript"),
        ])
        result = detect_rich_content_code_project(
            user_message="build me a full-stack Node.js app",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["data"]["project_type"] == "nodejs"

    def test_two_files_minimum_accepted(self) -> None:
        # Cap min 2 fichiers.
        assistant_text = self._build_assistant_text([
            ("main.py", "from fastapi import FastAPI\napp = FastAPI()", "python"),
            ("requirements.txt", "fastapi==0.100.0", "text"),
        ])
        result = detect_rich_content_code_project(
            user_message="écris une API Python complète",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert len(result["data"]["files"]) == 2

    def test_single_block_returns_none_for_code_file_to_handle(self) -> None:
        assistant_text = "```python\nprint('hello world from NEXYA')\n```"
        result = detect_rich_content_code_project(
            user_message="code-moi un script",
            assistant_text=assistant_text,
        )
        assert result is None

    def test_zero_blocks_returns_none(self) -> None:
        result = detect_rich_content_code_project(
            user_message="code complet",
            assistant_text="Voici une explication sans code. " * 10,
        )
        assert result is None

    def test_two_blocks_without_filenames_without_intent_returns_none(self) -> None:
        # 2 blocs sans filenames explicites + sans intent → false positive
        # potentiel (snippets orphelins ≠ projet). Skip.
        assistant_text = (
            "```python\nprint('one example')\n```\n\n"
            "```python\nprint('another example')\n```"
        )
        result = detect_rich_content_code_project(
            user_message="give me some Python examples",
            assistant_text=assistant_text,
        )
        # Skip car pas d'intent fort + pas de filenames explicites.
        assert result is None

    def test_two_blocks_without_filenames_with_strong_intent_accepted(self) -> None:
        # 2 blocs sans filenames explicites MAIS intent fort → accepté
        # (fallback main.{ext} pour les deux, MAIS le 2ème va se dédup
        # car même filename main.py — donc on retombe à 1 fichier → None).
        # Pour tester ce cas, on utilise 2 langages différents → 2
        # filenames distincts (main.py + main.dart).
        assistant_text = (
            "```python\nprint('python side here for the cap min check')\n```\n\n"
            "```dart\nvoid main() => print('dart side here for the cap min check');\n```"
        )
        result = detect_rich_content_code_project(
            user_message="build me a complete full-stack application",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert len(result["data"]["files"]) == 2

    def test_dedup_same_filename_returns_one_file_skip(self) -> None:
        # 2 blocs avec le même filename `main.py` → dédup post-extraction
        # → 1 seul fichier → Code File capture.
        assistant_text = (
            "**main.py**\n```python\nprint('version 1 for the cap min check')\n```\n\n"
            "**main.py**\n```python\nprint('version 2 for the cap min check')\n```"
        )
        result = detect_rich_content_code_project(
            user_message="écris une API complète",
            assistant_text=assistant_text,
        )
        # Post-dédup → 1 fichier seulement → Code Project skip.
        assert result is None

    def test_cap_max_50_files_truncated(self) -> None:
        # 51 fichiers → tronqué à 50.
        files = [
            (f"file_{i}.py", f"# file {i}\nprint({i})", "python") for i in range(51)
        ]
        assistant_text = self._build_assistant_text(files)
        result = detect_rich_content_code_project(
            user_message="écris un projet Python complet",
            assistant_text=assistant_text,
        )
        assert result is not None
        # Cap à 50.
        assert len(result["data"]["files"]) == 50

    def test_file_content_too_short_skipped(self) -> None:
        # Cap min 10 chars par fichier. Un bloc `\`\`\`\nx\n\`\`\`` (1 char)
        # sera skip individuellement → si on tombe en dessous de 2 fichiers
        # valides, le détecteur retourne None.
        assistant_text = (
            "**main.py**\n```python\nprint('something interesting here')\n```\n\n"
            "**tiny.py**\n```python\nx\n```"  # < 10 chars, skip
        )
        result = detect_rich_content_code_project(
            user_message="écris une API complète",
            assistant_text=assistant_text,
        )
        # 1 fichier valide seulement → Code Project skip.
        assert result is None

    def test_filename_in_inline_comment(self) -> None:
        # Filename en commentaire dans le bloc (pas sur ligne précédente).
        assistant_text = (
            "Voici une API simple :\n\n"
            "```python\n"
            "# main.py\n"
            "from fastapi import FastAPI\n"
            "app = FastAPI()\n"
            "```\n\n"
            "```python\n"
            "# routes.py\n"
            "from fastapi import APIRouter\n"
            "router = APIRouter()\n"
            "```"
        )
        result = detect_rich_content_code_project(
            user_message="écris une API FastAPI complète",
            assistant_text=assistant_text,
        )
        assert result is not None
        filenames = {f["filename"] for f in result["data"]["files"]}
        assert "main.py" in filenames
        assert "routes.py" in filenames

    def test_filename_with_subdir(self) -> None:
        assistant_text = self._build_assistant_text([
            ("src/main.py", "from app import app\napp.run()", "python"),
            ("src/app.py", "from flask import Flask\napp = Flask(__name__)", "python"),
            ("requirements.txt", "flask==3.0.0", "text"),
        ])
        result = detect_rich_content_code_project(
            user_message="écris une API Flask complète",
            assistant_text=assistant_text,
        )
        assert result is not None
        filenames = {f["filename"] for f in result["data"]["files"]}
        assert "src/main.py" in filenames

    def test_project_type_none_when_no_manifest(self) -> None:
        assistant_text = self._build_assistant_text([
            ("file1.py", "print('hello')", "python"),
            ("file2.py", "print('world')", "python"),
        ])
        result = detect_rich_content_code_project(
            user_message="écris un projet Python complet",
            assistant_text=assistant_text,
        )
        assert result is not None
        # Pas de pyproject/requirements → project_type None.
        assert result["data"]["project_type"] is None

    def test_path_traversal_rejected_via_pydantic(self) -> None:
        # Si l'IA met `**../malicious.sh**` (avec extension) comme
        # filename d'un fichier, strat (b) regex match (extension `.sh`
        # présente), `_extract_filename` retourne `../malicious.sh`,
        # PUIS Pydantic `_validate_filename_path_safe` rejette via
        # CodeProjectFileItem → ValidationError → tout le payload
        # CodeProjectDraftData explose → return None.
        # Note 1 : c'est le comportement attendu (sécurité par
        # construction côté Pydantic).
        # Note 2 : `../etc/passwd` (sans extension) ne match PAS le
        # regex strat (b) → fallback (d) → `main.sh` → safe par
        # accident. C'est aussi sécurisé mais moins testable.
        assistant_text = (
            "**../malicious.sh**\n```bash\necho 'something malicious here for cap min check'\n```\n\n"
            "**main.py**\n```python\nprint('something interesting here for cap min')\n```"
        )
        result = detect_rich_content_code_project(
            user_message="build me a complete app",
            assistant_text=assistant_text,
        )
        assert result is None

    def test_project_name_inferred_from_user_message(self) -> None:
        assistant_text = self._build_assistant_text([
            ("main.py", "from fastapi import FastAPI\napp = FastAPI()", "python"),
            ("models.py", "class Task: pass", "python"),
        ])
        result = detect_rich_content_code_project(
            user_message="écris-moi une API tâches complète",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["data"]["project_name"]  # non-vide

    def test_project_name_fallback_when_no_hint(self) -> None:
        assistant_text = self._build_assistant_text([
            ("main.py", "from fastapi import FastAPI\napp = FastAPI()", "python"),
            ("requirements.txt", "fastapi==0.100.0", "text"),
        ])
        result = detect_rich_content_code_project(
            user_message="",  # pas d'indication mais cap min 2 fichiers
            assistant_text=assistant_text,
        )
        # 2 fichiers explicites (**main.py**, **requirements.txt**)
        # → ratio explicit = 2/2 = 100 % → passe sans intent.
        assert result is not None
        # Fallback : "Python Project" via project_type.
        assert result["data"]["project_name"] == "Python Project"

    def test_payload_dict_structure_complete(self) -> None:
        assistant_text = self._build_assistant_text([
            ("a.py", "print('a')\n# some content", "python"),
            ("b.py", "print('b')\n# some other content", "python"),
        ])
        result = detect_rich_content_code_project(
            user_message="écris-moi un projet Python complet",
            assistant_text=assistant_text,
        )
        assert result is not None
        # Shape attendue : { kind, data: { project_name, description,
        #   files, project_type } }
        assert set(result.keys()) == {"kind", "data"}
        assert result["kind"] == "code_project_draft"
        assert set(result["data"].keys()) == {
            "project_name",
            "description",
            "files",
            "project_type",
        }
        # Files : liste de dicts avec {filename, content, language}.
        assert isinstance(result["data"]["files"], list)
        assert all(
            set(f.keys()) == {"filename", "content", "language"}
            for f in result["data"]["files"]
        )

    def test_empty_inputs_return_none(self) -> None:
        assert detect_rich_content_code_project("", "") is None
        assert detect_rich_content_code_project("code", "") is None
        assert detect_rich_content_code_project("", "   ") is None

    def test_explicit_filename_ratio_50pct_passes(self) -> None:
        # 2 fichiers : 1 explicite (**main.py**), 1 fallback dart (no
        # filename indiqué). Le 2e bloc utilise `dart` pour que le
        # fallback (d) produise `main.dart` (différent de `main.py`),
        # sinon dédup post-extraction les fusionnerait.
        # Ratio = 1/2 = 50 % = seuil min → passe SANS intent fort.
        assistant_text = (
            "**main.py**\n```python\nfrom fastapi import FastAPI\napp = FastAPI()\n```\n\n"
            "```dart\nvoid main() => print('orphan dart here for cap min check');\n```"
        )
        result = detect_rich_content_code_project(
            user_message="give me 2 files",
            assistant_text=assistant_text,
        )
        # 1 explicite (main.py) + 1 fallback (main.dart) = ratio 50 %.
        # → passe sans intent fort.
        assert result is not None
        assert len(result["data"]["files"]) == 2

    def test_explicit_filename_ratio_below_50pct_no_intent_skip(self) -> None:
        # 3 fichiers : 1 explicite, 2 fallback. Ratio = 1/3 = 33 % < 50 %.
        # Pas d'intent → skip.
        assistant_text = (
            "**main.py**\n```python\nfrom fastapi import FastAPI\napp = FastAPI()\n```\n\n"
            "```python\nprint('orphan one for the cap min check here')\n```\n\n"
            "```javascript\nconsole.log('orphan two for the cap min check here');\n```"
        )
        result = detect_rich_content_code_project(
            user_message="give me code",  # pas d'intent fort
            assistant_text=assistant_text,
        )
        assert result is None
