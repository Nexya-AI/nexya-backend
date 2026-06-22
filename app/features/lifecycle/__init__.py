"""Feature lifecycle — emails proactifs (onboarding, réengagement, digest).

Distincts des emails transactionnels (réaction à une action) : ces emails sont
envoyés par cron selon des critères temporels (J+N après inscription, inactivité,
semaine écoulée). Idempotents via la table `lifecycle_emails`.
"""
