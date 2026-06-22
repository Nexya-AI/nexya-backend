"""Phase 2 — Render-smoke de TOUS les templates email premium.

Filet de sécurité critique : le renderer utilise `StrictUndefined`, donc une
variable référencée mais non passée par un call-site fait crasher l'envoi EN
PROD. Ce test rend chaque template (HTML + TXT) avec :
  - un contexte « full » (toutes les variables avec valeurs → chemin on),
  - un contexte « minimal » (optionnels `{% if %}` à None → chemin off,
    optionnels `| default` omis → teste le filtre).

Couvre aussi : tous extends `_base_email` (masthead NEXYA présent), autoescape
HTML (body malveillant échappé), texte brut non échappé, macros (button/callout/
meta_table) sans crash.
"""

from __future__ import annotations

import pytest

from app.core.email.renderer import TemplateRenderer

# ── Contexte du dispatcher (superset task/security/payment) ────────
# Réplique app/features/notifications/service.py::_try_email (lignes ~692-712).
_DISPATCHER_FULL = {
    "user_name": "Ivan",
    "title": "Titre de l'alerte",
    "body": "Le corps du message de notification.",
    "data": {},
    "task_deep_link": "nexya://task/123",
    "unsubscribe_url": "https://app.nexyalabs.com/unsubscribe?token=abc",
    "task_title": "Prendre mes médicaments",
    "result_preview": "Voici le résultat généré par NEXYA.",
    "scheduled_at_human_readable": "2026-06-22 08:00 UTC",
    "plan_name": "NEXYA Pro",
    "amount_formatted": "3 500 FCFA",
    "invoice_url": "https://app.nexyalabs.com/invoice/1.pdf",
    "event_type": "new_device_login",
    "event_ip": "41.202.0.1",
    "event_user_agent_truncated": "NEXYA-App/1.1.6 Android",
    "event_time_utc": "2026-06-22 14:30 UTC",
    "password_reset_url": "https://app.nexyalabs.com/reset?token=z",
}

# Variante « optionnels off » : tous les champs if-guarded à None.
_DISPATCHER_MINIMAL = {
    **{k: None for k in _DISPATCHER_FULL},
    "user_name": "",
    "title": "Titre",
    "body": "Corps.",
    "data": {},
    "unsubscribe_url": None,
}


# (template_name, contexte) — chaque tuple sera rendu HTML + TXT.
_CASES: list[tuple[str, dict]] = [
    # welcome
    ("welcome", {"user_name": "Ivan", "unsubscribe_url": None}),
    ("welcome", {"user_name": "", "unsubscribe_url": None}),
    # password_reset
    (
        "password_reset",
        {
            "user_name": "Ivan",
            "reset_url": "https://app.nexyalabs.com/reset?token=abc",
            "expires_minutes": 15,
            "unsubscribe_url": None,
        },
    ),
    (
        "password_reset",
        {
            "user_name": "",
            "reset_url": "https://app.nexyalabs.com/reset?token=abc",
            "expires_minutes": 15,
            "unsubscribe_url": None,
        },
    ),
    # dispatcher-driven (4 templates × full + minimal)
    ("task_completed", _DISPATCHER_FULL),
    ("task_completed", _DISPATCHER_MINIMAL),
    ("task_reminder", _DISPATCHER_FULL),
    ("task_reminder", _DISPATCHER_MINIMAL),
    ("payment_confirmed", _DISPATCHER_FULL),
    ("payment_confirmed", _DISPATCHER_MINIMAL),
    ("account_security_alert", _DISPATCHER_FULL),
    ("account_security_alert", _DISPATCHER_MINIMAL),
    # account_deletion_scheduled — cancel_url if-guarded (toujours passé), grace default
    (
        "account_deletion_scheduled",
        {
            "user_name": "Ivan",
            "scheduled_purge_at": "2026-07-22 00:00 UTC",
            "grace_period_days": 30,
            "cancel_url": "https://app.nexyalabs.com/cancel",
            "unsubscribe_url": None,
        },
    ),
    (
        "account_deletion_scheduled",
        # grace_period_days OMIS (teste `| default(30)`), cancel_url=None (off-path)
        {
            "user_name": "",
            "scheduled_purge_at": "2026-07-22 00:00 UTC",
            "cancel_url": None,
            "unsubscribe_url": None,
        },
    ),
    # data_export_ready — download_url if-guarded, expires/blob defaults
    (
        "data_export_ready",
        {
            "user_name": "Ivan",
            "download_url": "https://app.nexyalabs.com/export.zip",
            "expires_in_days": 7,
            "blob_ttl_days": 7,
            "unsubscribe_url": None,
        },
    ),
    (
        "data_export_ready",
        # download_url=None (off-path), expires/blob OMIS (teste `| default(7)`)
        {"user_name": "", "download_url": None, "unsubscribe_url": None},
    ),
    # onboarding lifecycle (J+1 / J+3 / J+7)
    ("onboarding_d1", {"user_name": "Ivan", "unsubscribe_url": "https://x/unsub?token=t"}),
    ("onboarding_d1", {"user_name": "", "unsubscribe_url": None}),
    ("onboarding_d3", {"user_name": "Ivan", "unsubscribe_url": "https://x/unsub?token=t"}),
    ("onboarding_d3", {"user_name": "", "unsubscribe_url": None}),
    ("onboarding_d7", {"user_name": "Ivan", "unsubscribe_url": "https://x/unsub?token=t"}),
    ("onboarding_d7", {"user_name": "", "unsubscribe_url": None}),
    # suggestion_received (interne)
    (
        "suggestion_received",
        {
            "suggestion_type": "feature",
            "body": "Pouvez-vous ajouter un mode sombre dynamique ?",
            "user_email": "user@nexya.ai",
            "user_id": "11111111-1111-1111-1111-111111111111",
            "ip_anonymized": "1.2.3.0/24",
            "created_at": "2026-06-22T14:30:00+00:00",
            "unsubscribe_url": None,
        },
    ),
]


@pytest.fixture(scope="module")
def renderer() -> TemplateRenderer:
    return TemplateRenderer()


@pytest.mark.parametrize(("template_name", "ctx"), _CASES)
def test_template_renders_without_strictundefined(renderer, template_name, ctx):
    """Aucun StrictUndefined / TemplateNotFound, et masthead NEXYA présent."""
    html, text = renderer.render(template_name, **ctx)
    assert html.strip(), f"{template_name}.html vide"
    assert text.strip(), f"{template_name}.txt vide"
    # Le masthead du layout master est toujours là.
    assert "NEXYA" in html
    assert "NEXYA" in text
    # Le footer Nexyalabs est hérité du base.
    assert "Nexyalabs" in html
    assert "© 2026" in text


def test_all_user_templates_extend_base_gradient(renderer):
    """Sanity : les templates user portent le masthead gradient + liseré or."""
    html, _ = renderer.render("welcome", user_name="Ivan", unsubscribe_url=None)
    assert "linear-gradient" in html  # masthead premium
    assert "#E8A020" in html  # liseré or signature


def test_unsubscribe_url_rendered_when_present(renderer):
    html, text = renderer.render(
        "task_completed",
        **{**_DISPATCHER_FULL, "unsubscribe_url": "https://x/unsub?token=t"},
    )
    assert "https://x/unsub?token=t" in html
    assert "Se désinscrire" in html
    assert "https://x/unsub?token=t" in text


def test_unsubscribe_url_absent_when_none(renderer):
    html, text = renderer.render(
        "account_security_alert",
        **{**_DISPATCHER_FULL, "unsubscribe_url": None},
    )
    assert "Se désinscrire" not in html
    assert "Se désinscrire" not in text


def test_security_body_is_html_escaped(renderer):
    """Le `body` (potentiellement contrôlé) doit être échappé en HTML."""
    malicious = '<script>alert("xss")</script>'
    html, text = renderer.render(
        "account_security_alert",
        **{**_DISPATCHER_FULL, "body": malicious},
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    # TXT : brut (pas de rendu, pas de risque)
    assert malicious in text


def test_button_macro_rendered_in_password_reset(renderer):
    html, _ = renderer.render(
        "password_reset",
        user_name="Ivan",
        reset_url="https://x/reset?token=k",
        expires_minutes=15,
        unsubscribe_url=None,
    )
    # bouton bulletproof : <a> avec le lien + bgcolor table
    assert 'href="https://x/reset?token=k"' in html
    assert "Réinitialiser mon mot de passe" in html
