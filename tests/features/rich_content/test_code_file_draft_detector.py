"""Tests `code_file_draft_detector` (C4.6)."""

from __future__ import annotations

import pytest

from app.features.rich_content.code_file_draft_detector import (
    _extract_filename,
    detect_rich_content_code_file,
)


class TestExtractFilename:
    """4 stratégies fallback : ligne précédente / markdown bold / commentaire inline / fallback main.{ext}."""

    def test_strategy_a_filename_on_preceding_line(self) -> None:
        # Ligne juste avant le bloc → filename évident.
        preceding = "Voici le fichier :\nfibonacci.py"
        result = _extract_filename(
            block_content="def fib(n): pass",
            block_language="python",
            preceding_text=preceding,
        )
        assert result == "fibonacci.py"

    def test_strategy_a_with_subdir(self) -> None:
        preceding = "Voici :\nsrc/utils/helpers.py"
        result = _extract_filename(
            block_content="def helper(): pass",
            block_language="python",
            preceding_text=preceding,
        )
        assert result == "src/utils/helpers.py"

    def test_strategy_a_strips_markdown_bold(self) -> None:
        # `**main.py**` sur la ligne précédente.
        preceding = "Voici :\n**main.py**"
        result = _extract_filename(
            block_content="print('hi')",
            block_language="python",
            preceding_text=preceding,
        )
        assert result == "main.py"

    def test_strategy_a_strips_trailing_colon(self) -> None:
        # `main.py:` (Markdown header-like)
        preceding = "Fichier :\nmain.py:"
        result = _extract_filename(
            block_content="print('hi')",
            block_language="python",
            preceding_text=preceding,
        )
        assert result == "main.py"

    def test_strategy_b_markdown_bold_in_preceding_text(self) -> None:
        # Pas de ligne dédiée, mais bold `**foo.dart**` quelque part dans
        # les 200 chars précédents.
        preceding = "Voici le widget **App.dart** pour ton projet Flutter :"
        result = _extract_filename(
            block_content="class App {}",
            block_language="dart",
            preceding_text=preceding,
        )
        assert result == "App.dart"

    def test_strategy_b_takes_last_bold_match(self) -> None:
        # Plusieurs `**filename**` → prend le dernier (le plus proche du bloc).
        preceding = (
            "Voici **first.py** mais en fait on va utiliser **second.py** :"
        )
        result = _extract_filename(
            block_content="x = 1",
            block_language="python",
            preceding_text=preceding,
        )
        assert result == "second.py"

    def test_strategy_c_python_comment_at_top(self) -> None:
        # `# filename.py` en tête du bloc Python.
        block = "# fibonacci.py\ndef fib(n): pass"
        result = _extract_filename(
            block_content=block, block_language="python", preceding_text=""
        )
        assert result == "fibonacci.py"

    def test_strategy_c_js_comment_at_top(self) -> None:
        block = "// app.js\nconst app = {};"
        result = _extract_filename(
            block_content=block, block_language="javascript", preceding_text=""
        )
        assert result == "app.js"

    def test_strategy_c_html_comment_at_top(self) -> None:
        block = "<!-- index.html -->\n<html></html>"
        result = _extract_filename(
            block_content=block, block_language="html", preceding_text=""
        )
        assert result == "index.html"

    def test_strategy_c_css_comment_at_top(self) -> None:
        block = "/* style.css */\nbody { margin: 0; }"
        result = _extract_filename(
            block_content=block, block_language="css", preceding_text=""
        )
        assert result == "style.css"

    def test_strategy_d_fallback_python(self) -> None:
        # Aucun indice → fallback main.py.
        result = _extract_filename(
            block_content="print('hi')",
            block_language="python",
            preceding_text="",
        )
        assert result == "main.py"

    def test_strategy_d_fallback_dart(self) -> None:
        result = _extract_filename(
            block_content="void main() {}", block_language="dart", preceding_text=""
        )
        assert result == "main.dart"

    def test_strategy_d_fallback_typescript(self) -> None:
        result = _extract_filename(
            block_content="const x = 1;", block_language="typescript", preceding_text=""
        )
        assert result == "main.ts"

    def test_strategy_d_fallback_dockerfile_no_extension(self) -> None:
        # Dockerfile n'a PAS d'extension — nom littéral.
        result = _extract_filename(
            block_content="FROM python:3.12", block_language="dockerfile", preceding_text=""
        )
        assert result == "Dockerfile"

    def test_strategy_d_unknown_language_fallback(self) -> None:
        # Language non mappé → code-snippet.txt
        result = _extract_filename(
            block_content="something exotic",
            block_language="brainfuck",
            preceding_text="",
        )
        assert result == "code-snippet.txt"

    def test_strategy_d_empty_language_fallback(self) -> None:
        result = _extract_filename(
            block_content="some content", block_language="", preceding_text=""
        )
        assert result == "code-snippet.txt"


class TestDetectRichContentCodeFile:
    """Point d'entrée — détection 1 bloc de code."""

    def test_python_single_block_with_filename_preceding(self) -> None:
        assistant_text = (
            "Voici une implémentation récursive :\n\n"
            "fibonacci.py\n"
            "```python\n"
            "def fib(n):\n"
            "    return n if n < 2 else fib(n-1) + fib(n-2)\n"
            "```\n\n"
            "Tu peux l'utiliser comme ça : `fib(10)`."
        )
        result = detect_rich_content_code_file(
            user_message="écris-moi un script Python qui calcule fibonacci",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["kind"] == "code_file_draft"
        assert result["data"]["filename"] == "fibonacci.py"
        assert result["data"]["language"] == "python"
        assert "def fib" in result["data"]["content"]
        assert result["data"]["description"] is None

    def test_dart_single_block_with_markdown_bold_filename(self) -> None:
        assistant_text = (
            "Voici un widget Flutter **login_screen.dart** prêt à l'emploi :\n\n"
            "```dart\n"
            "import 'package:flutter/material.dart';\n\n"
            "class LoginScreen extends StatelessWidget {\n"
            "  @override\n"
            "  Widget build(BuildContext context) {\n"
            "    return Scaffold(body: Center(child: Text('Login')));\n"
            "  }\n"
            "}\n"
            "```"
        )
        result = detect_rich_content_code_file(
            user_message="génère-moi un écran login Flutter",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["data"]["filename"] == "login_screen.dart"
        assert result["data"]["language"] == "dart"

    def test_no_block_returns_none(self) -> None:
        result = detect_rich_content_code_file(
            user_message="comment ça marche ?",
            assistant_text="Voici une explication détaillée sans code. " * 10,
        )
        assert result is None

    def test_two_blocks_returns_none_for_code_project_to_handle(self) -> None:
        # 2 blocs → Code File skip, Code Project tentera ensuite.
        assistant_text = (
            "```python\nprint('a')\n```\n"
            "```python\nprint('b')\n```"
        )
        result = detect_rich_content_code_file(
            user_message="give me code",
            assistant_text=assistant_text,
        )
        assert result is None

    def test_three_blocks_returns_none(self) -> None:
        assistant_text = (
            "```python\nprint('a')\n```\n"
            "```python\nprint('b')\n```\n"
            "```python\nprint('c')\n```"
        )
        result = detect_rich_content_code_file(
            user_message="code",
            assistant_text=assistant_text,
        )
        assert result is None

    def test_block_too_short_returns_none(self) -> None:
        # Cap min 30 chars.
        assistant_text = "```python\nx = 1\n```"
        result = detect_rich_content_code_file(
            user_message="code",
            assistant_text=assistant_text,
        )
        assert result is None

    def test_block_at_min_threshold_accepted(self) -> None:
        # Exactement 30 chars de content.
        content = "x" * 30
        assistant_text = f"```python\n{content}\n```"
        result = detect_rich_content_code_file(
            user_message="code",
            assistant_text=assistant_text,
        )
        # Cap min strict ≥ 30 chars → 30 passe.
        assert result is not None
        assert result["data"]["filename"] == "main.py"

    def test_block_too_long_returns_none(self) -> None:
        # > 100k chars → cap Pydantic, refuse.
        content = "x" * 100_001
        assistant_text = f"```python\n{content}\n```"
        result = detect_rich_content_code_file(
            user_message="code",
            assistant_text=assistant_text,
        )
        assert result is None

    def test_block_without_language_annotation_fallback_plaintext(self) -> None:
        # ``` sans annotation → language="plaintext" auto.
        assistant_text = "```\nthis is some plain text content here\n```"
        result = detect_rich_content_code_file(
            user_message="show me a poem",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["data"]["language"] == "plaintext"
        assert result["data"]["filename"] == "code-snippet.txt"

    def test_language_normalized_lowercase(self) -> None:
        # ```PYTHON → "python" lowercase.
        assistant_text = "```PYTHON\ndef hello():\n    print('hello world')\n```"
        result = detect_rich_content_code_file(
            user_message="code",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["data"]["language"] == "python"

    def test_filename_with_subdir_extracted(self) -> None:
        assistant_text = (
            "src/utils/parser.py\n"
            "```python\n"
            "def parse(s): return s.upper()\n"
            "```"
        )
        result = detect_rich_content_code_file(
            user_message="code",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["data"]["filename"] == "src/utils/parser.py"

    def test_filename_from_inline_comment(self) -> None:
        # Pas de ligne avant, mais commentaire en tête du bloc.
        assistant_text = (
            "Voici une fonction utile :\n"
            "```python\n"
            "# utils.py\n"
            "def normalize(s):\n"
            "    return s.strip().lower()\n"
            "```"
        )
        result = detect_rich_content_code_file(
            user_message="code",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["data"]["filename"] == "utils.py"

    def test_filename_fallback_main_when_no_hint(self) -> None:
        # Pas de filename indiqué nulle part → fallback main.{ext}.
        assistant_text = (
            "Voici une fonction simple :\n"
            "```python\n"
            "def hello():\n"
            "    print('hello world')\n"
            "```"
        )
        result = detect_rich_content_code_file(
            user_message="code",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["data"]["filename"] == "main.py"

    def test_typescript_tsx_extension(self) -> None:
        assistant_text = (
            "```tsx\n"
            "import React from 'react';\n"
            "export const App = () => <div>Hello</div>;\n"
            "```"
        )
        result = detect_rich_content_code_file(
            user_message="code",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["data"]["filename"] == "main.tsx"
        assert result["data"]["language"] == "tsx"

    def test_unknown_language_accepted_with_snippet_fallback(self) -> None:
        # Language exotique → fallback code-snippet.txt.
        assistant_text = (
            "```brainfuck\n"
            "++++++++[>++++[>++>+++>+++>+<<<<-]>+>+>->>+[<]<-]>>.>>--.+++++++..+++.\n"
            "```"
        )
        result = detect_rich_content_code_file(
            user_message="code",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["data"]["filename"] == "code-snippet.txt"
        assert result["data"]["language"] == "brainfuck"

    def test_empty_inputs_return_none(self) -> None:
        assert detect_rich_content_code_file("", "") is None
        assert detect_rich_content_code_file("hello", "") is None
        assert detect_rich_content_code_file("", "   ") is None

    def test_none_inputs_return_none(self) -> None:
        # Defensive: les types stricts protègent mais on teste quand même.
        assert detect_rich_content_code_file("user", None) is None  # type: ignore[arg-type]
        assert detect_rich_content_code_file(None, "text") is None  # type: ignore[arg-type]

    def test_filename_path_traversal_rejected_via_pydantic(self) -> None:
        # Si l'IA met `../../etc/passwd` sur la ligne précédente,
        # le détecteur strat (a) tente le regex `[\w/.\-]+\.\w+` qui
        # ne match PAS `..` (le `..` n'est pas un caractère "word").
        # Donc on tombe en stratégie (d) → main.sh (fallback bash).
        # Le filename `main.sh` est path-safe → Pydantic accepte →
        # payload retourné (sécurité par construction du détecteur).
        # On utilise un content >= 30 chars pour passer le cap min.
        assistant_text = (
            "../../etc/passwd\n"
            "```bash\n"
            "echo 'something very interesting here for the cap min check'\n"
            "```"
        )
        result = detect_rich_content_code_file(
            user_message="code",
            assistant_text=assistant_text,
        )
        assert result is not None
        assert result["data"]["filename"] == "main.sh"

    def test_payload_dict_structure_complete(self) -> None:
        # Vérifie la shape exacte du payload retourné pour le contrat
        # Flutter `DraftPayload.tryFromMetadata`. Content >= 30 chars.
        assistant_text = (
            "```python\n"
            "# main.py\n"
            "def hello():\n"
            "    print('hello world from NEXYA')\n"
            "```"
        )
        result = detect_rich_content_code_file(
            user_message="code",
            assistant_text=assistant_text,
        )
        assert result is not None
        # Shape attendue : { kind, data: { filename, content, language, description } }
        assert set(result.keys()) == {"kind", "data"}
        assert result["kind"] == "code_file_draft"
        assert set(result["data"].keys()) == {
            "filename",
            "content",
            "language",
            "description",
        }

    def test_content_preserves_whitespace_and_newlines(self) -> None:
        # Le content du bloc doit être préservé EXACTEMENT (indentation
        # significative pour Python, retours à la ligne pour la lisibilité).
        # Note : la regex `(.*?)```` non-greedy match jusqu'au fence
        # fermant, et le `\n` AVANT `\`\`\`` est INCLUS dans le content
        # capturé. Donc le content extrait = source + "\n" final.
        source = "def fib(n):\n    if n < 2:\n        return n\n    return fib(n-1) + fib(n-2)"
        assistant_text = f"```python\n{source}\n```"
        result = detect_rich_content_code_file(
            user_message="code",
            assistant_text=assistant_text,
        )
        assert result is not None
        # Content extrait par le regex = source + "\n" (saut de ligne
        # avant le fence fermant). Comportement attendu et préservé
        # pour la fidélité du code original.
        assert result["data"]["content"] == source + "\n"

    def test_multiple_blocks_intent_does_not_force_single_file(self) -> None:
        # Même avec un intent FORT « code-moi un script » mais l'IA
        # produit 2 blocs → Code File skip (intent NE force pas
        # l'extraction du 1er bloc, le détecteur est body-driven strict).
        assistant_text = (
            "```python\nprint(1)\n```\n"
            "```python\nprint(2)\n```"
        )
        result = detect_rich_content_code_file(
            user_message="code-moi un script Python",
            assistant_text=assistant_text,
        )
        assert result is None

    def test_block_with_only_whitespace_content_returns_none(self) -> None:
        # Bloc ```python\n\n``` avec content vide ou whitespace → cap min.
        # (Le regex `(.*?)` matchera "" ou whitespace, qui fait <30 chars.)
        assistant_text = "```python\n  \n```"
        result = detect_rich_content_code_file(
            user_message="code",
            assistant_text=assistant_text,
        )
        assert result is None
