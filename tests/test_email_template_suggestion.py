"""N1 — Validation du template email `suggestion_received`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from app.core.email.renderer import TemplateRenderer


def _ctx(**overrides) -> dict:
    base = {
        "suggestion_type": "feature",
        "body": "Pouvez-vous ajouter un mode sombre dynamique ?",
        "user_email": "user@nexya.ai",
        "user_id": str(uuid.uuid4()),
        "ip_anonymized": "1.2.3.0/24",
        "created_at": datetime.now(UTC).isoformat(),
        "unsubscribe_url": None,
    }
    base.update(overrides)
    return base


def test_render_html_and_txt_non_empty():
    renderer = TemplateRenderer()
    html, txt = renderer.render("suggestion_received", **_ctx())
    assert html.strip()
    assert txt.strip()
    assert "Nouvelle suggestion" in html or "Nouvelle suggestion" in txt


def test_render_includes_layout_footer_partial():
    renderer = TemplateRenderer()
    html, txt = renderer.render("suggestion_received", **_ctx())
    # Le partial _layout_footer (F3) contient toujours « NEXYA »
    # ou le branding équipe — sanity check.
    assert "NEXYA" in html or "Nexya" in html
    assert "NEXYA" in txt or "Nexya" in txt


def test_render_escapes_html_in_body():
    """Si le body contient du HTML malveillant, Jinja2 doit l'escape
    (autoescape activé sur .html, off sur .txt)."""
    renderer = TemplateRenderer()
    malicious = '<script>alert("xss")</script>'
    html, txt = renderer.render("suggestion_received", **_ctx(body=malicious))
    # HTML : escape strict
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    # TXT : pas d'escape (texte brut, pas de risque rendu)
    assert malicious in txt
