"""
Rendu Jinja2 des templates d'emails.

Les templates vivent dans `app/core/email/templates/<name>.html|.txt`.
Le renderer charge les deux variantes (HTML + texte brut) — chaque
email transactionnel doit avoir une version texte pour les clients
mail qui bloquent le HTML (accessibilité + anti-spam).
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined, select_autoescape

_TEMPLATES_DIR = Path(__file__).parent / "templates"


class TemplateRenderer:
    """Rend les variantes HTML + texte d'un même template."""

    def __init__(self, templates_dir: Path = _TEMPLATES_DIR) -> None:
        self._env = Environment(
            loader=FileSystemLoader(str(templates_dir)),
            autoescape=select_autoescape(["html", "xml"]),
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
        )

    def render(self, template_name: str, **context: object) -> tuple[str, str]:
        """Rend `<name>.html` et `<name>.txt` avec le même contexte.

        Retourne `(html_body, text_body)`.
        """
        html = self._env.get_template(f"{template_name}.html").render(**context)
        text = self._env.get_template(f"{template_name}.txt").render(**context)
        return html, text


_renderer_singleton: TemplateRenderer | None = None


def get_template_renderer() -> TemplateRenderer:
    global _renderer_singleton
    if _renderer_singleton is None:
        _renderer_singleton = TemplateRenderer()
    return _renderer_singleton
