"""
NEXYA — Classifieur d'intention léger pour le forçage des tools.

`detect_planning_intent(text)` répond à une question simple : le dernier
message utilisateur exprime-t-il **sans ambiguïté** une demande de
planification (créer un rappel, programmer une tâche) ?

Pourquoi : Gemini 2.5 Flash, en `tool_config=AUTO`, ignore parfois les
tools même quand l'utilisateur demande clairement un rappel (historique
Bug-010). Quand cette fonction retourne `True`, `streaming._run_link`
bascule le provider en mode forcé (`ANY` / `tool_choice="required"`) sur
le round 0 — le LLM DOIT alors émettre un function_call.

Discipline de calibrage (planner-from-chat LOT 5) :

- **Les faux positifs coûtent cher** : forcer un tool call sur une
  question qui n'en demande pas dégrade l'UX (carte de tâche non voulue).
- **Les faux négatifs sont bénins** : on retombe simplement en `AUTO`,
  où Gemini appelle quand même le tool la plupart du temps, aidé par le
  bloc `build_tools_guidance()`.

Donc : classifieur **conservateur**. On ne déclenche que sur des tournures
impératives non équivoques, et on bloque dès qu'un marqueur de question
explicative (« comment… », « c'est quoi… ») est présent. Heuristique
mot-clé pure — aucun appel LLM, aucune I/O, FR + EN.
"""

from __future__ import annotations

from typing import Final

# Tournures impératives non équivoques d'une demande de planification.
# Toutes en minuscules — la comparaison se fait sur `text.lower()`.
_PLANNING_PATTERNS: Final[frozenset[str]] = frozenset(
    {
        # FR — impératif « rappelle / préviens / alerte »
        "rappelle-moi",
        "rappelle moi",
        "rappelles-moi",
        "rappelles moi",
        "rappelez-moi",
        "rappelez moi",
        "rappeler de",
        "rappeler que",
        "préviens-moi",
        "previens-moi",
        "préviens moi",
        "previens moi",
        "prévenez-moi",
        "prevenez-moi",
        "alerte-moi",
        "alerte moi",
        "alertes-moi",
        "fais-moi penser",
        "fais moi penser",
        "n'oublie pas de me",
        "noublie pas de me",
        # FR — création / programmation explicite
        "crée un rappel",
        "cree un rappel",
        "crée-moi un rappel",
        "crée moi un rappel",
        "ajoute un rappel",
        "ajoute-moi un rappel",
        "ajoute moi un rappel",
        "mets un rappel",
        "mets-moi un rappel",
        "met un rappel",
        "programme un rappel",
        "programme-moi un rappel",
        "crée une tâche",
        "crée une tache",
        "cree une tache",
        "ajoute une tâche",
        "ajoute une tache",
        "planifie-moi",
        "planifie moi",
        "planifie un",
        "planifie une",
        "programme une tâche",
        "programme une tache",
        # EN
        "remind me",
        "set a reminder",
        "set me a reminder",
        "set up a reminder",
        "create a reminder",
        "add a reminder",
        "schedule a",
        "schedule me",
        "alert me",
        "make me remember",
        "notify me to",
    }
)

# Marqueurs de question explicative / how-to. Leur présence ANNULE la
# détection, même si une tournure de planification matche : « comment
# créer un rappel ? » est une question, pas une demande à exécuter.
# On reste volontairement large — un faux négatif (retour en AUTO) est
# bénin, alors qu'un faux positif force un tool call non voulu.
_META_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "comment ",
        "c'est quoi",
        "cest quoi",
        "qu'est-ce",
        "quest-ce",
        "qu est-ce",
        "pourquoi",
        "à quoi sert",
        "a quoi sert",
        "explique",
        "how do",
        "how to",
        "how can",
        "how does",
        "what is",
        "what's a",
        "whats a",
    }
)


def detect_planning_intent(text: str) -> bool:
    """Indique si `text` exprime sans ambiguïté une demande de planification.

    Args:
        text: contenu du dernier message utilisateur.

    Returns:
        `True` uniquement si une tournure impérative de planification est
        présente ET qu'aucun marqueur de question explicative ne l'est.
        `False` dans le doute (le caller retombe alors sur `AUTO`).
    """
    if not text or not text.strip():
        return False
    lowered = text.lower()
    if any(marker in lowered for marker in _META_MARKERS):
        return False
    return any(pattern in lowered for pattern in _PLANNING_PATTERNS)
