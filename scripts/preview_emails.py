"""Génère un aperçu navigable de tous les emails NEXYA.

Usage :
    python -m scripts.preview_emails

Rend chaque template avec des données d'exemple réalistes dans
`email_previews/` (gitignored). Ouvre ensuite les .html dans un navigateur
pour juger le rendu premium. Outil de dev pur — aucun envoi réel.
"""

from __future__ import annotations

from pathlib import Path

from app.core.email.renderer import TemplateRenderer

_OUT = Path(__file__).resolve().parent.parent / "email_previews"

# Données d'exemple par template (les optionnels sont remplis pour montrer
# le rendu « riche » — chemin on des `{% if %}`).
_SAMPLES: dict[str, dict] = {
    "welcome": {"user_name": "Ivan", "unsubscribe_url": None},
    "password_reset": {
        "user_name": "Ivan",
        "reset_url": "https://app.nexyalabs.com/reset-password?token=eyJhbGci",
        "expires_minutes": 15,
        "unsubscribe_url": None,
    },
    "account_security_alert": {
        "user_name": "Ivan",
        "title": "Nouvelle connexion à ton compte NEXYA",
        "body": (
            "Une connexion à ton compte vient d'être détectée depuis un appareil "
            "que nous ne connaissions pas encore. Si c'est bien toi, tu peux ignorer "
            "ce message. Sinon, change ton mot de passe immédiatement."
        ),
        "event_type": "new_device_login",
        "event_ip": "41.202.0.42",
        "event_user_agent_truncated": "NEXYA-App/1.1.6 (Android 14; Samsung Galaxy)",
        "event_time_utc": "2026-06-22 14:30 UTC",
        "password_reset_url": "https://app.nexyalabs.com/reset-password?token=x",
        "unsubscribe_url": None,
    },
    "task_completed": {
        "user_name": "Ivan",
        "title": "Tâche terminée",
        "task_title": "Résumé quotidien de l'actualité tech",
        "result_preview": (
            "Aujourd'hui : OpenAI annonce GPT-5, l'UE finalise l'AI Act, "
            "et NEXYA atteint 10 000 utilisateurs. Détails dans l'app."
        ),
        "task_deep_link": "nexya://task/abc-123",
        "unsubscribe_url": "https://app.nexyalabs.com/unsubscribe?token=t",
    },
    "task_reminder": {
        "user_name": "Ivan",
        "title": "Rappel",
        "task_title": "Prendre mes médicaments",
        "scheduled_at_human_readable": "aujourd'hui à 20h00",
        "task_deep_link": "nexya://task/def-456",
        "unsubscribe_url": "https://app.nexyalabs.com/unsubscribe?token=t",
    },
    "payment_confirmed": {
        "user_name": "Ivan",
        "title": "Paiement confirmé",
        "plan_name": "NEXYA Pro (mensuel)",
        "amount_formatted": "3 500 FCFA",
        "invoice_url": "https://app.nexyalabs.com/invoices/2026-06.pdf",
        "unsubscribe_url": "https://app.nexyalabs.com/unsubscribe?token=t",
    },
    "account_deletion_scheduled": {
        "user_name": "Ivan",
        "scheduled_purge_at": "22 juillet 2026",
        "grace_period_days": 30,
        "cancel_url": "https://app.nexyalabs.com/account/cancel-deletion",
        "unsubscribe_url": None,
    },
    "data_export_ready": {
        "user_name": "Ivan",
        "download_url": "https://app.nexyalabs.com/exports/ivan-2026-06.zip",
        "expires_in_days": 7,
        "blob_ttl_days": 7,
        "unsubscribe_url": None,
    },
    "suggestion_received": {
        "suggestion_type": "feature",
        "body": "Pouvez-vous ajouter un mode hors-ligne complet ?",
        "user_email": "user@example.com",
        "user_id": "11111111-1111-1111-1111-111111111111",
        "ip_anonymized": "41.202.0.0/24",
        "created_at": "22 juin 2026, 14:30 UTC",
        "unsubscribe_url": None,
    },
    "onboarding_d1": {
        "user_name": "Ivan",
        "unsubscribe_url": "https://app.nexyalabs.com/unsubscribe?token=t",
    },
    "onboarding_d3": {
        "user_name": "Ivan",
        "unsubscribe_url": "https://app.nexyalabs.com/unsubscribe?token=t",
    },
    "onboarding_d7": {
        "user_name": "Ivan",
        "unsubscribe_url": "https://app.nexyalabs.com/unsubscribe?token=t",
    },
}


def main() -> None:
    _OUT.mkdir(exist_ok=True)
    renderer = TemplateRenderer()
    index_links = []
    for name, ctx in _SAMPLES.items():
        html, text = renderer.render(name, **ctx)
        (_OUT / f"{name}.html").write_text(html, encoding="utf-8")
        (_OUT / f"{name}.txt").write_text(text, encoding="utf-8")
        index_links.append(
            f'<li><a href="{name}.html">{name}</a> &middot; <a href="{name}.txt">.txt</a></li>'
        )
        print(f"  [ok] {name}.html + {name}.txt")

    index = (
        "<!DOCTYPE html><html lang='fr'><head><meta charset='utf-8'>"
        "<title>NEXYA — Aperçu emails</title>"
        "<style>body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;"
        "max-width:560px;margin:48px auto;padding:0 16px;color:#16161d;}"
        "h1{color:#2E9BF0;}li{margin:10px 0;font-size:17px;}"
        "a{color:#2E9BF0;}</style></head><body>"
        "<h1>NEXYA — Aperçu des emails</h1>"
        "<p>Clique pour ouvrir chaque rendu (active le dark mode de ton OS "
        "pour tester l'adaptation).</p><ul>" + "".join(index_links) + "</ul></body></html>"
    )
    (_OUT / "index.html").write_text(index, encoding="utf-8")
    print(f"\nAperçus générés dans : {_OUT}")
    print(f"Ouvre : {_OUT / 'index.html'}")


if __name__ == "__main__":
    main()
