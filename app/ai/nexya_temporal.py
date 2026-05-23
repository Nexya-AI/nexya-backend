"""
NEXYA — Blocs de contexte injectés à la runtime dans le system prompt.

Contrairement aux prompts experts (`app/ai/expert_prompts/`) qui sont
**statiques** (assemblés une fois au boot), ce module produit des blocs
**recalculés à chaque requête** :

- `build_temporal_context()` — date et heure UTC courantes. Sans ce bloc,
  le LLM n'a aucune notion du « maintenant » : impossible de transformer
  « rappelle-moi demain à 8h » en une date ISO absolue. C'est le défaut
  corrigé par le LOT 2 de la session planner-from-chat.

  **2026-05-23 — bug timezone fixé** : si le client (Flutter) fournit son
  offset UTC (`client_timezone="+01:00"`), on enrichit le bloc avec l'heure
  LOCALE de l'utilisateur + l'offset ISO, et on instruit le LLM de
  produire ses ISO datetimes AVEC l'offset (ex: `2026-05-23T20:00:00+01:00`).
  Sans ce fix, le LLM interprétait « 20h » comme 20h UTC → tâche programmée
  1h-12h trop tard selon le fuseau de l'utilisateur.

- `build_tools_guidance()` — doctrine d'usage des tools Planner. Source
  UNIQUE (avant le LOT 2, une copie vivait dans `expert_prompts/general.py`
  uniquement → les experts `medicine`/`legal` outillés au LOT 4 n'auraient
  eu aucune consigne). Injectée à la volée par `streaming._stream_link`
  pour tout expert dont `ctx.tools` est peuplé.

Les deux fonctions sont **pures** (aucune I/O, aucune dépendance DB) et
prennent des paramètres optionnels pour la testabilité.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Final

# Noms français — `datetime` ne fournit pas de localisation fiable
# cross-plateforme (le module `locale` est fragile sur Windows/Alpine).
# On fige les noms ici : déterministe, testable, zéro dépendance.
_WEEKDAYS_FR: Final[tuple[str, ...]] = (
    "lundi",
    "mardi",
    "mercredi",
    "jeudi",
    "vendredi",
    "samedi",
    "dimanche",
)
_MONTHS_FR: Final[tuple[str, ...]] = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)


# Format strict d'offset ISO accepté par `client_timezone` : `+HH:MM`,
# `-HH:MM`, ou `Z`. Un format laxiste ouvrirait la porte à des bizarreries
# (`+1`, `01:00`, fuseaux IANA mal supportés par stdlib `datetime`). On reste
# strict, et on tombe en mode UTC-only si l'input ne match pas — fail-safe.
_CLIENT_TZ_OFFSET_RE: Final = re.compile(r"^([+-])(\d{2}):(\d{2})$")


def _parse_client_timezone(raw: str | None) -> timezone | None:
    """Parse un offset ISO `+HH:MM` / `-HH:MM` / `Z` en `datetime.timezone`.

    Retourne `None` si l'input est absent, invalide, ou hors borne
    `[-14:00, +14:00]` (les fuseaux réels du monde tiennent dans cette
    plage). Fail-safe absolu : on ne lève jamais, on laisse le caller
    tomber sur le mode UTC-only.
    """
    if raw is None:
        return None
    raw = raw.strip()
    if not raw:
        return None
    if raw.upper() == "Z":
        return UTC
    match = _CLIENT_TZ_OFFSET_RE.match(raw)
    if match is None:
        return None
    sign_str, hh_str, mm_str = match.groups()
    try:
        hh = int(hh_str)
        mm = int(mm_str)
    except ValueError:
        return None
    if not (0 <= hh <= 14 and 0 <= mm < 60):
        return None
    total_minutes = hh * 60 + mm
    if sign_str == "-":
        total_minutes = -total_minutes
    return timezone(timedelta(minutes=total_minutes))


def _format_offset_iso(tz: timezone) -> str:
    """Formate un `timezone` en `+HH:MM` / `-HH:MM` (jamais `Z`).

    On préfère `+00:00` à `Z` côté instruction LLM car les ISO datetimes
    que l'utilisateur va recevoir back porteront tous le même format
    consistant (`2026-05-23T20:00:00+01:00`).
    """
    offset = tz.utcoffset(None)
    if offset is None:
        return "+00:00"
    total = int(offset.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    hours = total // 3600
    minutes = (total % 3600) // 60
    return f"{sign}{hours:02d}:{minutes:02d}"


def build_temporal_context(
    *,
    now: datetime | None = None,
    client_timezone: str | None = None,
) -> str:
    """Construit le bloc « contexte temporel » injecté en tête du system
    prompt (juste après le préambule NEXYA).

    Donne au LLM l'ancre `maintenant` nécessaire pour résoudre toute
    demande de planification relative (« demain », « ce soir », « dans
    3 jours », « lundi prochain ») en date absolue.

    **Bug timezone (2026-05-23)** : sans `client_timezone`, le LLM
    interprétait « 20h » comme 20h UTC, ce qui produisait des tâches
    programmées 1h-12h trop tard selon le fuseau réel de l'utilisateur.
    Avec `client_timezone="+01:00"` (envoyé par le Flutter via
    `DateTime.now().timeZoneOffset`), on injecte l'heure LOCALE de
    l'utilisateur dans le bloc + une instruction explicite : « quand
    l'utilisateur dit "20h", c'est 20h LOCALE ; produis l'ISO avec
    l'offset (`2026-05-23T20:00:00+01:00`) ».

    Le scheduler Pydantic `OnceConfig.at` (et tous les `compute_next_run`)
    convertit l'ISO offset-aware en UTC automatiquement — pas besoin de
    toucher au scheduler ni à la DB.

    Args:
        now: instant de référence (testabilité). Défaut : `datetime.now(UTC)`.
            Une valeur naïve est interprétée comme UTC ; une valeur aware
            est convertie en UTC.
        client_timezone: offset ISO du client (`+HH:MM`, `-HH:MM`, `Z`).
            Si `None` ou invalide → fallback UTC-only (comportement
            historique pré-fix). Format strict, fail-safe.

    Returns:
        Bloc markdown prêt à concaténer dans le system prompt.
    """
    moment_utc = now or datetime.now(UTC)
    if moment_utc.tzinfo is None:
        moment_utc = moment_utc.replace(tzinfo=UTC)
    else:
        moment_utc = moment_utc.astimezone(UTC)

    client_tz = _parse_client_timezone(client_timezone)

    # Si le client n'a pas envoyé son offset (ou un offset invalide), on
    # retombe sur le bloc UTC-only historique. Le LLM produira ses ISO en
    # UTC. C'est imparfait (cf. bug timezone) mais aligné sur le contrat
    # API legacy — le frontend doit migrer pour bénéficier du fix.
    if client_tz is None:
        tomorrow = moment_utc + timedelta(days=1)
        day_after = moment_utc + timedelta(days=2)
        weekday_fr = _WEEKDAYS_FR[moment_utc.weekday()]
        month_fr = _MONTHS_FR[moment_utc.month - 1]
        return (
            "[Contexte temporel — horloge serveur NEXYA]\n"
            f"Nous sommes le {weekday_fr} {moment_utc.day} {month_fr} "
            f"{moment_utc.year}, {moment_utc:%H:%M} UTC.\n"
            f"- Aujourd'hui : {moment_utc:%Y-%m-%d} ({weekday_fr}).\n"
            f"- Demain : {tomorrow:%Y-%m-%d} ({_WEEKDAYS_FR[tomorrow.weekday()]}).\n"
            f"- Après-demain : {day_after:%Y-%m-%d} "
            f"({_WEEKDAYS_FR[day_after.weekday()]}).\n"
            "- Toutes les dates et heures que tu produis pour planifier une "
            "tâche sont exprimées en UTC.\n"
            "- Pour une demande relative (« demain », « ce soir », « dans "
            "3 jours », « lundi prochain »), calcule toujours la date absolue "
            "à partir d'aujourd'hui AVANT de planifier.\n"
            "- Convention des jours de semaine pour les tools de planification : "
            "0=lundi, 1=mardi, 2=mercredi, 3=jeudi, 4=vendredi, 5=samedi, "
            "6=dimanche."
        )

    # Mode timezone-aware (fix 2026-05-23).
    moment_local = moment_utc.astimezone(client_tz)
    tomorrow_local = moment_local + timedelta(days=1)
    day_after_local = moment_local + timedelta(days=2)
    offset_iso = _format_offset_iso(client_tz)
    weekday_fr = _WEEKDAYS_FR[moment_local.weekday()]
    month_fr = _MONTHS_FR[moment_local.month - 1]

    return (
        "[Contexte temporel — horloge utilisateur]\n"
        f"L'utilisateur est dans le fuseau horaire UTC{offset_iso}.\n"
        f"Heure LOCALE de l'utilisateur (référence) : {weekday_fr} "
        f"{moment_local.day} {month_fr} {moment_local.year}, "
        f"{moment_local:%H:%M}.\n"
        f"Heure UTC équivalente (info) : {moment_utc:%Y-%m-%d %H:%M} UTC.\n"
        f"- Aujourd'hui (heure locale) : {moment_local:%Y-%m-%d} ({weekday_fr}).\n"
        f"- Demain (heure locale) : {tomorrow_local:%Y-%m-%d} "
        f"({_WEEKDAYS_FR[tomorrow_local.weekday()]}).\n"
        f"- Après-demain (heure locale) : {day_after_local:%Y-%m-%d} "
        f"({_WEEKDAYS_FR[day_after_local.weekday()]}).\n"
        "- **Règle absolue** : quand l'utilisateur dit une heure (« 20h », "
        "« 8 heures du matin », « demain à midi »), c'est TOUJOURS son "
        "heure LOCALE, jamais l'UTC. Tu produis tes datetimes ISO **avec "
        f"l'offset de son fuseau** (ex: `2026-05-23T20:00:00{offset_iso}`). "
        "Le backend les convertira en UTC tout seul.\n"
        "- Pour une demande relative (« demain », « ce soir », « dans "
        "3 jours », « lundi prochain »), calcule à partir d'**aujourd'hui "
        "en heure locale** AVANT de produire l'ISO datetime.\n"
        "- Convention des jours de semaine pour les tools de planification : "
        "0=lundi, 1=mardi, 2=mercredi, 3=jeudi, 4=vendredi, 5=samedi, "
        "6=dimanche."
    )


def build_tools_guidance() -> str:
    """Construit le bloc « doctrine d'usage des tools Planner ».

    Injecté en DERNIER dans le system prompt (effet de récence) et
    UNIQUEMENT pour les experts dont des tools sont actifs. Source unique
    de la doctrine — voir docstring du module.

    Returns:
        Bloc markdown prêt à concaténer dans le system prompt.
    """
    return (
        "[Outils de planification — function calling]\n"
        "Tu disposes de 4 tools pour gérer les tâches planifiées de "
        "l'utilisateur. Dès que l'intention est claire, APPELLE le tool "
        "approprié AU LIEU de répondre en texte :\n"
        "\n"
        "- `create_task` — créer un rappel ou une action planifiée. "
        "Déclencheurs : « rappelle-moi… », « crée un rappel… », "
        "« préviens-moi… », « note que… », « tous les jours/lundis à… », "
        "« le 25 à 9h… », « dans 2 heures… », « toutes les N minutes… ».\n"
        "- `list_tasks` — afficher les tâches planifiées de l'utilisateur "
        "(« mes rappels », « ce que j'ai programmé », « ma liste de "
        "tâches »).\n"
        "- `update_task` — modifier une tâche existante (titre, prompt ou "
        "horaire). Nécessite son `task_id`.\n"
        "- `pause_task` — suspendre une tâche existante sans la supprimer. "
        "Nécessite son `task_id`.\n"
        "\n"
        "Règles d'or :\n"
        "1. **Agis, ne demande pas** — pour un cas simple et non ambigu, "
        "appelle `create_task` directement, sans demander de confirmation "
        "préalable. L'utilisateur pourra toujours modifier ou supprimer.\n"
        "2. **Clarifie seulement si vraiment ambigu** — si l'heure ou la "
        "récurrence sont réellement indéterminables (« rappelle-moi de "
        "temps en temps »), pose UNE question courte avant d'appeler.\n"
        "3. **`prompt` auto-suffisant** — le champ `prompt` de "
        "`create_task` sera exécuté seul, hors de cette conversation. "
        "Rédige-le complet et autonome (ex: « Rappel : prendre les "
        "médicaments du matin. »), jamais une référence vague au contexte "
        "courant (« comme on a dit »).\n"
        "4. **`title` court et reconnaissable** — un libellé que "
        "l'utilisateur identifie d'un coup d'œil dans sa liste (ex: "
        "« Prendre médicaments »).\n"
        "5. **Choisis le bon `schedule`** parmi les 10 types décrits dans "
        "le schéma du tool — demande ponctuelle → `once`, récurrence → "
        "`daily` / `weekly` / `monthly` / `interval_minutes` / etc.\n"
        "6. **Confirme après coup** — une fois le tool exécuté, le système "
        "affiche une carte récapitulative à l'utilisateur. Enchaîne avec "
        "une phrase de confirmation chaleureuse et concrète (« C'est "
        "noté — je te rappellerai demain à 8h de prendre tes "
        "médicaments. »), jamais un simple « Fait. »."
    )
