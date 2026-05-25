"""Feature Rendering (Session C4.3 — 2026-05-24).

Mermaid diagrams server-side via Kroki.io : endpoint `POST /render/mermaid
{source}` qui délègue à `https://kroki.io/mermaid/svg`, cache Redis 7j sur
sha256(source), retourne SVG inline (taille typique < 50 KB).

Le frontend Flutter consomme ce endpoint via `NxMermaidCard` qui détecte
les blocs ` ```mermaid ` du markdown chat et les rend via `flutter_svg`.
"""
