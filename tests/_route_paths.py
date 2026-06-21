"""Introspection des routes FastAPI, robuste aux versions de Starlette.

Les versions recentes de Starlette imbriquent les routers inclus via
``app.include_router(...)`` dans des objets internes (``_IncludedRouter``) qui
n'exposent pas ``.path`` directement : les sous-routes vivent dans ``.routes``.
Iterer naivement ``{route.path for route in app.routes}`` leve alors
``AttributeError: '_IncludedRouter' object has no attribute 'path'``.

Ce collecteur recurse sur tout objet portant un attribut ``.routes`` et ramasse
chaque ``.path`` rencontre : compatible avec la structure plate (anciennes
versions) ET imbriquee (recentes). A utiliser dans les smoke tests
"endpoints montes" a la place de la comprehension fragile.
"""

from __future__ import annotations

from typing import Any


def all_route_paths(app: Any) -> set[str]:
    """Ensemble des chemins de routes montes sur ``app``.

    Descend recursivement dans les routers inclus / sous-applications pour
    rester insensible a la maniere dont Starlette structure ``app.routes``
    (plate ou imbriquee selon la version).
    """
    return _collect(app.routes)


def _collect(routes: Any) -> set[str]:
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", None)
        if isinstance(path, str):
            paths.add(path)
        nested = getattr(route, "routes", None)
        if nested:
            paths |= _collect(nested)
    return paths
