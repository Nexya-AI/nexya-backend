"""
Détection du `output_kind` d'une tâche planifiée (Planner divin — LOT B2).

Une tâche produit l'un de 3 livrables, auto-détecté du `prompt` à la création
(et re-détecté si le prompt change). NEXYA s'adapte tout seul :

- ``reminder``    — un rappel court et chaleureux (« rappelle-moi d'étudier »).
- ``generation``  — du contenu riche en Markdown à lire (« résume-moi l'actu »).
- ``document``    — un vrai fichier PDF/DOCX (« corrige cet exercice en PDF »).

Le ``output_kind`` est stocké dans ``ScheduledTask.metadata_json["output_kind"]``
(colonne JSONB nullable déjà présente → aucune migration). Le worker
``execute_scheduled_task`` le relit pour choisir le pipeline de production, et
les schémas ``TaskResponse`` / ``TaskResultResponse`` l'exposent au frontend
(badge sur la carte tâche + carte de résultat adaptative).

Stratégie de détection (priorité descendante) :
1. **document** si ``detect_document_intent`` (rich_content) — le détecteur le
   plus conservateur (verbe de création + type de document + souvent un mot de
   format). Quand il tire, l'utilisateur veut vraiment un fichier. Philosophie
   « NEXYA fait le travail » : même « rappelle-moi de rédiger un rapport » est
   traité comme un document à produire, pas un simple nudge.
2. **reminder** sinon si ``detect_planning_intent`` (« rappelle-moi… »,
   « remind me… ») — tournure impérative de rappel sans verbe de production.
3. **generation** par défaut — tout le reste produit du Markdown riche.

Les deux détecteurs réutilisés sont fail-safe (retournent ``False`` sur input
vide/None), et cette fonction retourne TOUJOURS un kind valide (jamais None).
"""

from __future__ import annotations

from typing import Any, Final, Literal

from app.ai.intent_classifier import detect_planning_intent
from app.features.rich_content.document_draft_detector import detect_document_intent

# ── Type + constantes ──────────────────────────────────────────────
OutputKind = Literal["reminder", "generation", "document"]

OUTPUT_KIND_REMINDER: Final[str] = "reminder"
OUTPUT_KIND_GENERATION: Final[str] = "generation"
OUTPUT_KIND_DOCUMENT: Final[str] = "document"

# Valeurs valides — utilisé par `extract_output_kind` pour rejeter une valeur
# corrompue/inconnue dans `metadata_json` (drift, écriture manuelle) et retomber
# sur le défaut neutre.
_VALID_KINDS: Final[frozenset[str]] = frozenset(
    {OUTPUT_KIND_REMINDER, OUTPUT_KIND_GENERATION, OUTPUT_KIND_DOCUMENT}
)

_DEFAULT_KIND: Final[str] = OUTPUT_KIND_GENERATION


def detect_task_output_kind(prompt: str) -> OutputKind:
    """Auto-détecte le `output_kind` d'une tâche depuis son prompt.

    Voir docstring du module pour la stratégie de priorité.

    Returns:
        L'un de ``"reminder"`` / ``"generation"`` / ``"document"``. Jamais None.
        Prompt vide/None → ``"generation"`` (défaut neutre).
    """
    if not isinstance(prompt, str) or not prompt.strip():
        return OUTPUT_KIND_GENERATION

    if detect_document_intent(prompt):
        return OUTPUT_KIND_DOCUMENT
    if detect_planning_intent(prompt):
        return OUTPUT_KIND_REMINDER
    return OUTPUT_KIND_GENERATION


def extract_output_kind(metadata_json: dict[str, Any] | None) -> str:
    """Lit le `output_kind` depuis un `metadata_json` (fail-safe).

    Utilisé par les sérialiseurs `TaskResponse` / `TaskResultResponse` et par
    le worker. Toute valeur absente, None, ou hors `_VALID_KINDS` (tâche créée
    avant la feature, drift, corruption) retombe sur ``"generation"`` — le
    frontend rend alors une carte « contenu riche » neutre, jamais un crash.
    """
    if not isinstance(metadata_json, dict):
        return _DEFAULT_KIND
    raw = metadata_json.get("output_kind")
    if isinstance(raw, str) and raw in _VALID_KINDS:
        return raw
    return _DEFAULT_KIND
