"""Catalogue voix NEXYA branded — Session N1.

Constante Python plutôt qu'une table DB pour 6 entrées stables :
- Pas de migration à maintenir.
- Modification du nom/personality = 1 redéploiement (acceptable pour
  des données aussi stables qu'un branding).
- Si on doit ajouter une voix V2/V3 (par ex. tier Pro), il suffit
  d'ajouter une entrée dans `_NEXYA_VOICES` ou de migrer vers une
  table à ce moment-là.

Les 6 IDs (`aurora`, `memora`, `soleil`, `sagesse`, `eron`, `nyanga`)
matchent **exactement** ceux du Flutter
`nexya_front_end/lib/features/settings/models/voice_model.dart`.
Les couleurs UI restent côté Flutter (V1).
"""

from __future__ import annotations

from app.features.voice.schemas import VoiceCatalogueItem

# ── Catalogue figé (6 voix branded NEXYA) ─────────────────────────
_NEXYA_VOICES: list[VoiceCatalogueItem] = [
    VoiceCatalogueItem(
        id="aurora",
        name="Aurora",
        personality="Moderne et affirmée — claire, dynamique, naturelle.",
        tone="medium",
        language="fr",
    ),
    VoiceCatalogueItem(
        id="memora",
        name="Memora",
        personality="Sympathique et cool — chaleureuse, attentionnée.",
        tone="medium",
        language="fr",
    ),
    VoiceCatalogueItem(
        id="soleil",
        name="Soleil",
        personality="Rayonnante et énergique — joyeuse, motivante.",
        tone="medium",
        language="fr",
    ),
    VoiceCatalogueItem(
        id="sagesse",
        name="Sagesse",
        personality="Profonde et bienveillante — posée, rassurante.",
        tone="high",
        language="fr",
    ),
    VoiceCatalogueItem(
        id="eron",
        name="Eron",
        personality="Posé et précis — voix masculine analytique.",
        tone="deep",
        language="fr",
    ),
    VoiceCatalogueItem(
        id="nyanga",
        name="N'yanga",
        personality="Traditionnelle et culturelle — ancrée dans le terroir africain.",
        tone="deep",
        language="fr",
    ),
]


def get_voice_catalogue() -> list[VoiceCatalogueItem]:
    """Retourne la liste figée des 6 voix NEXYA branded.

    Sortie immutable côté API — l'ordre est stable (aurora → memora →
    soleil → sagesse → eron → nyanga) pour cohérence avec le picker
    Flutter qui peut afficher dans l'ordre du catalogue.
    """
    return list(_NEXYA_VOICES)
