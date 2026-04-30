"""Feature Projects — conteneurs logiques qui regroupent conversations, fichiers
et instructions système spécifiques.

Aligné avec le modèle Flutter `ProjectModel` côté front :
    - name, icon_index (0..24), color_index (0..17)
    - instructions (system prompt dédié, facultatif)
    - files (métadonnées — upload physique déféré à E3)
    - conversations (relation via FK `conversations.project_id ON DELETE SET NULL`)

Les 9 endpoints publics sont exposés sous le préfixe `/projects` via `router.py`.
"""
