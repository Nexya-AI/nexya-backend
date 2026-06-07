"""C4.11 — Module Account : dashboard quotas user pour Settings > Mon compte.

Livre :
- `GET /user/quotas` (router.py) : agrège docs ce mois + voice minutes today
  + library storage cumulé + reset_at calculé.
- `QuotasService` (service.py) : orchestre les 3 sources (PostgreSQL pour
  docs+storage, Redis pour voice minutes via BudgetTracker E1).
- `UserQuotasResponse` (schemas.py) : contrat Pydantic strict aligné
  Flutter `UserQuotas` domain Equatable.

Pattern aligné `app/features/library/` (Service + Router + Schemas) sans
ORM dédié (le module agrège des données existantes — pas de table propre).
"""
