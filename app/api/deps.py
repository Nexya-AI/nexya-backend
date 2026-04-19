"""
Dépendances FastAPI partagées par tous les endpoints `/v1/*`.

Ce module réexporte (et n'implémente pas) les dépendances déjà définies
ailleurs dans le code afin de fournir un point d'import stable et unique :

    from app.api.deps import get_db, get_current_user, get_pagination

Sera peuplé en PR 3 (découpage `features/auth/`) et PR 4 (versioning `/v1/`).
Pour l'instant : volontairement vide — pas de side-effect à l'import.
"""
