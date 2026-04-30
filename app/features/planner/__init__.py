"""Feature Planner — tâches IA planifiées (Session F1).

L'utilisateur planifie des prompts qui s'exécutent automatiquement
selon un schedule (once / interval / daily / weekly). 2 workers arq
(`dispatch_due_tasks` + `execute_scheduled_task`) orchestrent
l'exécution. Les résultats sont stockés dans `scheduled_task_results`
et purgés après 30 jours.

Hors scope F1 : notifications FCM post-exécution (F2), expressions
cron complètes, timezone user-spécifique, streaming SSE des résultats.
"""
