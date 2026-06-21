"""Introspection des routes FastAPI via le schéma OpenAPI public et STABLE.

Les smoke tests "endpoints montés" doivent vérifier qu'un router n'a pas été
oublié dans `app.include_router(...)`. Historiquement ils itéraient
``{route.path for route in app.routes}`` — fragile :

  - Les versions récentes de Starlette imbriquent les routers inclus dans des
    objets internes (``_IncludedRouter``) qui n'exposent PAS ``.path`` (->
    ``AttributeError``) et dont les sous-routes ne sont pas accessibles via un
    attribut stable d'une version à l'autre. Itérer ``app.routes`` rate alors
    toutes les routes incluses (/auth/*, /chat/*, /tasks/*, ...).

La solution robuste et insensible aux versions : lire le schéma OpenAPI généré
par FastAPI (``app.openapi()["paths"]``), qui liste TOUS les chemins APIRoute
montés (chemins complets ``/auth/register``, ``/tasks/{task_id}``, ...). C'est
l'API publique de FastAPI, stable à travers les versions, et le résultat est
mis en cache par FastAPI après le premier appel.
"""

from __future__ import annotations

from typing import Any


def all_route_paths(app: Any) -> set[str]:
    """Ensemble des chemins de routes montés sur ``app``.

    S'appuie sur ``app.openapi()["paths"]`` (chemins APIRoute publics). En
    dernier recours (si la génération OpenAPI échoue), retombe sur une
    introspection plate de ``app.routes`` — potentiellement incomplète mais
    jamais bloquante.
    """
    try:
        schema = app.openapi()
        return set(schema.get("paths", {}).keys())
    except Exception:  # pragma: no cover - filet ultra-défensif
        paths: set[str] = set()
        for route in getattr(app, "routes", ()):
            path = getattr(route, "path", None)
            if isinstance(path, str):
                paths.add(path)
        return paths
