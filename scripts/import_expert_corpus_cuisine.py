"""
Pipeline d'ingestion du corpus Expert Cuisine (G2) — recettes camerounaises propriétaires.

Source : 2 livres PDF de l'auteur (Loth Ivan Ngassa Yimga / Nexyalabs)
extraits en Markdown via pymupdf, dans
`<NEXYA_AI>/DATAS SETS/Expert Cuisine & Vie Quotidienne/extracted cuisines/`.

Usage typique en 2 phases :

    # Phase 1 — parsing pur (génère JSON canoniques + rapport de validation,
    # sans embed ni INSERT DB) :
    python scripts/import_expert_corpus_cuisine.py --dry-run

    # Phase 2 — ingestion réelle (embed Gemini Vertex + INSERT pgvector) :
    python scripts/import_expert_corpus_cuisine.py --ingest

    # Maintenance — re-ingestion complète (switch de modèle) :
    python scripts/import_expert_corpus_cuisine.py --ingest --force-reembed

Le pipeline est **idempotent** :
- Le parsing JSON canonique est déterministe (même input → même output).
- L'INSERT pgvector utilise `ON CONFLICT DO NOTHING` sur
  `(expert_slug, content_sha256)` — un re-run n'insère aucun doublon.

Stratégie d'extraction (validée après audit du dataset Ivan) :
- On parse les **2 master .md** (`RECETTES DETAILLEES 1. 2.md` et
  `RECETTES DETAILLEES 3.md`), pas les 178 sections individuelles.
- Le master a des `## Title` propres ; les sections individuelles ont
  des titres décoratifs cassés (~70 fichiers nommés
  `incontournable_de_la_cuisine_camerounaise.md` parce que l'extracteur
  pymupdf a saisi le H1 décoratif au lieu du vrai nom de recette).

Pipeline 7 étapes par bloc de recette candidat :
1. Strip footers `Recettes camerounaises\nPage X` (regex multiline).
2. Détection sommaire (> 10 lignes pattern « NOM PAGE_NUM ») → REJET.
3. Détection titre décoratif → recherche du vrai titre dans le body.
4. Vérification structure (INGREDIENTS + PREPARATION/METHODE).
5. Extraction structurée (ingredients[], steps[], description, region, category).
6. Validation Pydantic stricte (RecipeCanonical).
7. Sortie : `_canonical/{slug}.json` ou `_rejected/{key}.json`.

Coût estimé (dim 768, Gemini `gemini-embedding-001`, Vertex AI free tier) :
- ~120 recettes × ~2000 chars = ~240k tokens → ~$0.005
- Stockage DB : 120 rows × (768 × 4 B + ~1 KB metadata) ≈ 470 KB
- Durée ingestion : ~30 secondes sur connexion correcte
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import re
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import delete, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.ai.embeddings import (
    EmbeddingsError,
    EmbeddingsRateLimitError,
    get_embeddings_provider,
)
from app.config import settings
from app.core.database.postgres import AsyncSessionLocal
from app.features.experts.models import ExpertCorpusChunk

log = structlog.get_logger()


# ══════════════════════════════════════════════════════════════
# Constantes
# ══════════════════════════════════════════════════════════════

EXPERT_SLUG = "cooking"
SOURCE_OWNER = "Loth Ivan Ngassa Yimga / Nexyalabs"
SOURCE_TAG = "nexyalabs-recipe-book"

# Path par défaut vers le dataset Ivan (HORS du repo Git, propriété intellectuelle).
_DEFAULT_REPO_ROOT = Path(__file__).resolve().parent.parent
_DEFAULT_DATASET_ROOT = (
    _DEFAULT_REPO_ROOT.parent / "DATAS SETS" / "Expert Cuisine & Vie Quotidienne"
)
DEFAULT_SOURCE_DIR = _DEFAULT_DATASET_ROOT / "extracted cuisines"
DEFAULT_CANONICAL_DIR = _DEFAULT_DATASET_ROOT / "_canonical"
DEFAULT_REJECTED_DIR = _DEFAULT_DATASET_ROOT / "_rejected"
DEFAULT_REPORT_PATH = _DEFAULT_DATASET_ROOT / "_validation_report.md"

# Constantes algorithmes
MAX_RETRIES = 5
INITIAL_BACKOFF = 2.0
PROGRESS_EVERY = 50

# Catégories Literal (alignées sur le sommaire du livre)
RecipeCategory = str  # "entree" | "plat_principal" | "accompagnement" | "patisserie" | "boisson" | "sauce" | "epice" | "astuce"
_VALID_CATEGORIES = frozenset(
    {
        "entree",
        "plat_principal",
        "accompagnement",
        "patisserie",
        "boisson",
        "sauce",
        "epice",
        "astuce",
    }
)

# Régions / ethnies camerounaises canoniques (auto-détection regex).
# Ordre = priorité : ethnie spécifique > région administrative.
_REGION_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\b(Bassa)\b", "Bassa"),
    (r"\b(Douala)\b", "Douala"),
    (r"\b(Bamil[ée]k[ée])\b", "Bamileke"),
    (r"\b(Bamoun)\b", "Bamoun"),
    (r"\b(B[ée]ti|Ewondo|Bulu|Fang)\b", "Beti"),
    (r"\b(Foulb[ée]|Peul|Fulani)\b", "Foulbe"),
    (r"\b(Tikar)\b", "Tikar"),
    (r"\b(Bafia)\b", "Bafia"),
    (r"\b(Anglophone)\b", "Anglophone"),
    (r"\b(Buea)\b", "Sud-Ouest"),
    (r"\b(Bamenda)\b", "Nord-Ouest"),
    (r"\b(Adamaoua)\b", "Adamaoua"),
    (r"\b(Littoral)\b", "Littoral"),
    (r"\b(Centre)\b", "Centre"),
    (r"\b(Ouest)\b", "Ouest"),
    (r"\b(Cameroun(?:ais)?e?)\b", "Cameroun"),  # fallback générique
)

# Mots-clés de catégorie (auto-détection sur titre).
#
# ⚠️ PRIORITÉ HIÉRARCHISÉE (ordre crucial) — refonte G2 V8 :
#
# 1. **`astuce`** d'abord (titre « Comment X », « Astuce X ») — un titre
#    qui commence par "Comment" est forcément une astuce, pas une recette.
# 2. **`plat_principal`** explicite — protéines (porc, poulet, poisson,
#    bœuf, mouton, gibier, crevettes, etc.) en mode "plat principal". Ce
#    pattern doit matcher AVANT `epice` pour éviter que "Mbongo Tchobi
#    (Poisson)" soit classé "épice" parce que "mbongo" est dans la liste.
# 3. **`entree`** — salade, macédoine, entrée.
# 4. **`patisserie`** — desserts, gâteaux, beignets, koki (gâteau cornille).
# 5. **`boisson`** — boissons STRICTES (jus, vin, bissap, matango, folere).
#    "lait" RETIRÉ car ambigu (Lait de Coco = ingrédient, pas boisson).
# 6. **`sauce`** — UNIQUEMENT si "sauce" est le PREMIER MOT du titre,
#    sinon "X Sauce Y" (ex: "Crevettes Sauce Tomate") doit rester plat.
# 7. **`accompagnement`** — féculents (foufou, plantain, manioc, igname,
#    macabo, riz, patate, gari, tapioca seul) y compris bouillis.
# 8. **`epice`** — ÉPICES SEULES (titre = 1-2 mots maximum identifiés
#    comme épice). Le pattern strict évite les faux positifs sur des
#    plats composés.
# 9. **Default `plat_principal`** si rien ne match.

_CATEGORY_PATTERNS: tuple[tuple[str, str], ...] = (
    # 1. Astuce — priorité absolue (titre méta)
    (r"^(comment|astuce|conseil|truc|secret)\b", "astuce"),
    # 2. Plat principal — protéines + plats festifs
    # NB : `sauce arachide` retiré de la liste car « Sauce Arachide » seul
    # est ambigu (sauce vs plat) — on laisse `^sauce\b` (pattern 6) gagner.
    (
        r"\b(porc|poulet|poisson|b[œo]euf|viande|mouton|gibier|crevettes?|"
        r"sardine|escargot|chenille|crabe|herisson|vipere|ndomba|"
        r"poulet dg|dg|jollof|riz jollof|sanga|brais[ée]|r[ôo]ti|"
        r"au gibier|aux crevettes|aux \w+)\b",
        "plat_principal",
    ),
    # 3. Entrée — salades + entrées légères
    (
        r"\b(salade|entr[ée]e|spaghetti sauce avocat|mac[ée]doine)\b",
        "entree",
    ),
    # 4. Pâtisserie — desserts, gâteaux, beignets
    (
        r"\b(g[âa]teau|gateau|bonbon|cr[êe]pe|pancake|madeleine|sabl[ée]|"
        r"biscotte|croquette|caramel|cr[èe]me p[âa]tissi[èe]re|beignet|"
        r"beignets|koki|met de pistache|assok bitetam)\b",
        "patisserie",
    ),
    # 5. Boisson — boissons strictes uniquement
    (
        r"\b(jus|vin|bissap|matango|folere|kossam|tisane|infusion|"
        r"bi[èe]re|smoothie)\b",
        "boisson",
    ),
    # 6. Sauce — UNIQUEMENT si "sauce" est en début de titre (pas "X Sauce Y")
    (r"^sauce\b", "sauce"),
    # 7. Accompagnement — féculents
    (
        r"\b(foufou|b[âa]ton de manioc|kwacoco|miondos|water fufu|plantain|"
        r"patate|igname|tappe|tape de plantain|chips|frites|chikwangue|"
        r"mbon lep|ntouba|manioc bouilli|pommes de terre bouillies|"
        r"pile de plantains?|pile de pommes|tapioca saut[ée]|gari|macabo)\b",
        "accompagnement",
    ),
    # 8. Épice — épices seules (titre court 1-3 mots)
    (
        r"^(p[èe]b[èe]|djansang|d?jansang|mbongo|hiomi|aneth|anis|persil|"
        r"thym|odjom|kwa ni ndong)$",
        "epice",
    ),
)

# Titres décoratifs reconnus (à ne PAS prendre comme nom de recette).
# Cassé en frozenset pour lookup O(1).
_DECORATIVE_TITLES = frozenset(
    {
        "INCONTOURNABLE DE LA CUISINE CAMEROUNAISE",
        "ENTREES",
        "ENTREE",
        "PLATS",
        "COMPLEMENTS",
        "PATISSERIES ET VIENOISERIES",
        "PATISSERIES",
        "JUICES",
        "EPICES",
        "ASTUCES DE CUISINE",
        "ASTUCES",
        "TENUE MILLITAIRE",  # typo dans le sommaire vol 1.2
        "SOMMAIRE",
        "SAUCES",
    }
)

# Sections explicites à reconnaître pour ingredients / preparation
_INGREDIENT_SECTION_PATTERNS = (
    r"INGR[ÉE]DIENTS\s*:?",
    r"INGREDIENTS\s*:?",
    r"Ingr[ée]dients\s*(?:beignets|sauce tomate)?\s*:?",
    r"Ingrédients\s*:?",
    r"Ingredients\s*:?",
)
_PREPARATION_SECTION_PATTERNS = (
    r"PR[ÉE]PARATION\s*:?",
    r"PREPARATION\s*:?",
    r"Pr[ée]paration\s*(?:des beignets|de la sauce tomates)?\s*:?",
    r"Préparation\s*:?",
    r"Preparation\s*:?",
    r"M[ÉE]THODE\s*:?",
    r"METHODE\s*:?",
    r"M[ée]thode\s*:?",
    r"[ÉE]TAPES\s*(?:de pr[ée]paration)?\s*:?",
    r"ETAPES\s*:?",
    r"[ÉE]tapes\s*(?:de pr[ée]paration)?\s*:?",
    r"Étapes\s*:?",
    r"Etapes\s*:?",
)


# ══════════════════════════════════════════════════════════════
# Modèles Pydantic
# ══════════════════════════════════════════════════════════════


class SourceMetadata(BaseModel):
    """Source d'une recette — traçabilité juridique + AI Act Article 13."""

    owner: str = Field(..., min_length=3, max_length=200)
    book: str = Field(..., min_length=3, max_length=100)
    section_index: int = Field(..., ge=0)


class RecipeCanonical(BaseModel):
    """Représentation structurée d'une recette extraite et nettoyée.

    Source de vérité humain-relisable — peut être éditée à la main par
    Ivan dans `_canonical/` avant l'ingestion pgvector.
    """

    id_slug: str = Field(..., min_length=3, max_length=80, pattern=r"^[a-z0-9-]+$")
    name: str = Field(..., min_length=3, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    region: str | None = Field(default=None, max_length=50)
    category: str = Field(default="plat_principal")
    # Min 2 ingrédients exigé pour les vraies recettes, mais relaxé à 0
    # pour `category="astuce"` (conseils, descriptions d'épices, tutoriels
    # qui sont ingérés dans le corpus sans liste d'ingrédients formelle).
    # Cf. `_validate_ingredients_minimum` ci-dessous + Ivan G2 2026-05-16.
    ingredients: list[str] = Field(default_factory=list)
    steps: list[str] = Field(..., min_length=1)
    source: SourceMetadata

    @field_validator("description", mode="before")
    @classmethod
    def _truncate_description(cls, v: str | None) -> str | None:
        """Tronque la description à 2000 chars (cap field) au lieu de
        raise — les recettes avec une intro culturelle longue sont
        légitimes, on garde juste les premiers 2000 chars."""
        if v is None:
            return None
        v = v.strip() if isinstance(v, str) else v
        if isinstance(v, str) and len(v) > 2000:
            return v[:1997] + "..."
        return v

    @field_validator("ingredients", mode="after")
    @classmethod
    def _validate_ingredients(cls, v: list[str]) -> list[str]:
        """Strip + cap par item. Le check de quantité minimale est délégué
        au `model_validator` après que `category` soit connu — une astuce
        peut légitimement avoir 0 ingrédient (cf. Foufou Gari, Odjom)."""
        cleaned = [i.strip() for i in v if i and i.strip()]
        for i, item in enumerate(cleaned):
            if len(item) > 500:
                cleaned[i] = item[:497] + "..."
        return cleaned

    @field_validator("steps", mode="after")
    @classmethod
    def _validate_steps(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s and s.strip()]
        if not cleaned:
            raise ValueError("au moins 1 étape non vide requise")
        for i, item in enumerate(cleaned):
            if len(item) > 2000:
                cleaned[i] = item[:1997] + "..."
        return cleaned

    @field_validator("category", mode="after")
    @classmethod
    def _validate_category(cls, v: str) -> str:
        if v not in _VALID_CATEGORIES:
            return "plat_principal"
        return v

    @model_validator(mode="after")
    def _require_ingredients_unless_astuce(self) -> RecipeCanonical:
        """Au moins 2 ingrédients exigés SAUF si `category="astuce"`.

        Les astuces (conseils, descriptions d'ingrédient, tutoriels rapides)
        peuvent légitimement n'avoir aucun ingrédient structuré — elles
        valent quand même la peine d'être ingérées dans le corpus RAG.
        """
        if self.category != "astuce" and len(self.ingredients) < 2:
            raise ValueError(
                "au moins 2 ingrédients non vides requis (sauf catégorie 'astuce')"
            )
        return self


@dataclass(frozen=True, slots=True)
class RejectionReport:
    """Rapport d'une section rejetée par le parser."""

    source_book: str
    section_index: int
    raw_title: str
    reason: str  # 'sommaire' | 'decorative_no_recipe' | 'no_ingredients' | 'no_steps' | 'too_short' | 'duplicate' | 'validation_error'
    detail: str = ""
    raw_excerpt: str = ""  # premiers 200 chars du body

    def to_dict(self) -> dict[str, Any]:
        return {
            "source_book": self.source_book,
            "section_index": self.section_index,
            "raw_title": self.raw_title,
            "reason": self.reason,
            "detail": self.detail,
            "raw_excerpt": self.raw_excerpt,
        }


@dataclass(slots=True)
class ParseResult:
    """Résultat agrégé du parsing d'un master .md."""

    accepted: list[RecipeCanonical] = field(default_factory=list)
    rejected: list[RejectionReport] = field(default_factory=list)


# ══════════════════════════════════════════════════════════════
# Helpers de pré-traitement
# ══════════════════════════════════════════════════════════════


_FOOTER_RE = re.compile(
    r"^Recettes camerounaises\s*\nPage \d+\s*$",
    flags=re.MULTILINE | re.IGNORECASE,
)

# Métadonnée éditeur récurrente (vol 3) — pollue les descriptions sans
# valeur culinaire. Strip avant tout downstream.
_EDITION_LINE_RE = re.compile(
    r"^\s*Une recette des [ÉE]ditions\s*\d{4}\s*$",
    flags=re.MULTILINE | re.IGNORECASE,
)


def _strip_footers(text: str) -> str:
    """Retire les footers PDF (« Recettes camerounaises\\nPage N ») et
    les mentions d'éditeur (« Une recette des Editions 2015 »)."""
    if not text:
        return ""
    cleaned = _FOOTER_RE.sub("", text)
    cleaned = _EDITION_LINE_RE.sub("", cleaned)
    # Collapse triple newlines en double
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned


def _normalize_bullets(text: str) -> str:
    """Normalise les bullets unicode en `-` standard + collapse espaces.

    Pipeline en 3 passes :
    1. Map des bullets unicode classiques + PUA Word (`\\uf0b7`, utilisé
       par Word/Times New Roman dans le vol 3) -> `-`.
    2. **Bullet orphelin** : si une ligne contient juste `-` et la
       suivante a un contenu, on joint les deux (`-\\n500g de riz` ->
       `- 500g de riz`). pymupdf vol 3 sépare bullets et contenus.
    3. Collapse espaces horizontaux multiples.
    """
    if not text:
        return ""
    # Bullets unicode courants → `-`
    text = re.sub(r"[•‣◦⁃⁌⁍\uf0a7\uf0b7]", "-", text)
    # 2. Bullet orphelin sur sa propre ligne -> joindre avec la suivante
    text = re.sub(
        r"^[ \t]*-[ \t]*\n[ \t]*(\S)",
        r"- \1",
        text,
        flags=re.MULTILINE,
    )
    # 3. Espaces multiples horizontaux
    text = re.sub(r"[ \t]{2,}", " ", text)
    return text


# ══════════════════════════════════════════════════════════════
# Découpe du master en blocs (titre, body)
# ══════════════════════════════════════════════════════════════


_HEADING_RE = re.compile(r"^##\s+(.+?)\s*$", flags=re.MULTILINE)

# Sous-sections internes aux recettes (toujours fusionner avec le bloc
# précédent — ce sont des en-têtes de paragraphe, pas des titres de recette).
# Découvert post-dry-run : pymupdf a saisi ces H2 quand le PDF d'origine
# avait une page break entre INGREDIENTS et PREPARATION.
_ALWAYS_ORPHAN_TITLES = frozenset(
    {
        "INGREDIENTS",
        "INGRÉDIENTS",
        "INGRÉDIENT",
        "INGREDIENT",
        "PREPARATION",
        "PRÉPARATION",
        "METHODE",
        "MÉTHODE",
        "ETAPES",
        "ÉTAPES",
        "ÉTAPES DE PRÉPARATION",
        "ETAPES DE PREPARATION",
    }
)

# Titres décoratifs du livre (chapitres, intercalaires) — fusionner avec
# le bloc précédent UNIQUEMENT si le body NE contient PAS de structure
# de recette (INGREDIENTS + PREPARATION). Dans le master vol 1.2, ~90
# blocs sont nommés `Incontournable De La Cuisine Camerounaise` mais
# contiennent en réalité des recettes différentes (le pymupdf a saisi
# le H1 décoratif au lieu du vrai nom). On veut PRÉSERVER ces blocs et
# laisser `_extract_real_title_from_body` retrouver leur vrai nom.
_DECORATIVE_TITLES_CONDITIONAL = frozenset(
    {
        "INCONTOURNABLE DE LA CUISINE CAMEROUNAISE",
        "INCONTOURNABLES DE LA CUISINE CAMEROUNAISE",
        "ENTREES",
        "ENTREE",
        "ENTRÉE",
        "ENTRÉES",
        "PLATS",
        "PLAT",
        "COMPLEMENTS",
        "COMPLÉMENTS",
        "PATISSERIES ET VIENOISERIES",
        "PATISSERIES ET VIENNOISERIES",
        "PATISSERIES",
        "PÂTISSERIES",
        "JUICES",
        "JUS",
        "BOISSONS",
        "EPICES",
        "ÉPICES",
        "ASTUCES DE CUISINE",
        "ASTUCES",
        "TENUE MILLITAIRE",  # typo dans le sommaire vol 1.2
        "SAUCES",
        "SAUCE",
    }
)


def _body_has_recipe_structure(body: str) -> bool:
    """Détecte si un body contient INGREDIENTS + PREPARATION (donc une
    vraie recette, même mal-titrée). Utilisé pour décider si un titre
    « décoratif conditionnel » doit être fusionné ou préservé.
    """
    if not body:
        return False
    has_ing = bool(
        re.search(
            r"(?:^|\n)\s*(?:INGR[ÉE]DIENTS?|Ingr[ée]dients?)\b",
            body,
            flags=re.IGNORECASE,
        )
    )
    has_prep = bool(
        re.search(
            r"(?:^|\n)\s*(?:PR[ÉE]PARATION|M[ÉE]THODE|[ÉE]TAPES|Pr[ée]paration|M[ée]thode|[ÉE]tapes)\b",
            body,
            flags=re.IGNORECASE,
        )
    )
    # Cas pratique vol 1.2 : INGREDIENTS détectés + au moins quelques bullets
    # ou numérotations dans le body (un vrai contenu de recette).
    has_bullets = bool(re.search(r"(?:^|\n)\s*[-*•]\s+\S", body))
    return has_ing and (has_prep or has_bullets)


def _is_orphan_heading(title: str, body: str = "") -> bool:
    """True si le titre est en réalité une sous-section/décoration sans
    contenu de recette propre → doit fusionner avec le bloc précédent.

    Stratégie en 2 niveaux :

    1. **Toujours orphelin** : INGREDIENTS / PREPARATION / METHODE /
       ETAPES → sous-section interne, fusion systématique.
    2. **Décoratif conditionnel** : INCONTOURNABLE..., ENTREES, PLATS,
       etc. → fusion UNIQUEMENT si le body ne contient pas de structure
       de recette (INGREDIENTS + PREPARATION/bullets). Sinon on préserve
       (le vrai titre sera retrouvé via `_extract_real_title_from_body`).
    """
    upper = title.strip().upper().rstrip(":").rstrip(".").strip()
    # Niveau 1 : toujours orphelin
    if upper in _ALWAYS_ORPHAN_TITLES:
        return True
    # Heuristique : titres pseudo-numéroté type « 4 Cotes » résidus de sommaire
    if re.match(r"^\d{1,2}\s+[A-Za-zÉÀÂÎÔÛÇéàâîôûç]+\s*$", upper):
        return True
    # Niveau 2 : décoratif conditionnel
    if upper in _DECORATIVE_TITLES_CONDITIONAL:
        return not _body_has_recipe_structure(body)
    return False


def _split_master_into_blocks(md: str) -> list[tuple[str, str]]:
    """Split sur les `## ` headers du master, retourne `[(title, body), ...]`.

    Pipeline en 2 passes :
    1. **Split brut** sur regex `^## TITRE$` multiline.
    2. **Post-merge** : tout bloc dont le titre est orphelin
       (`_is_orphan_heading`) est fusionné avec le bloc précédent — son
       contenu rejoint le body du bloc parent. Cela répare le découpage
       artificiel introduit par pymupdf quand une recette s'étale sur
       plusieurs pages PDF.

    Le titre H1 (`# `) du master est ignoré (juste le nom du livre).
    Tout ce qui se trouve avant le premier `## ` est ignoré.
    """
    if not md:
        return []
    matches = list(_HEADING_RE.finditer(md))
    if not matches:
        return []

    raw_blocks: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(md)
        body = md[body_start:body_end].strip()
        raw_blocks.append((title, body))

    # Post-merge des blocs orphelins (passe le body pour la décision
    # conditionnelle des titres décoratifs).
    #
    # Cas critique : un orphelin sans précédent (`merged` vide) ou dont
    # le titre est manifestement orphelin (INGREDIENTS, PREPARATION) ne
    # doit JAMAIS être promu en bloc-recette autonome — il causerait un
    # slug `ingredients` qui dédup-écraserait toutes les autres recettes
    # mal-titrées identiquement. On SKIP silencieusement dans ce cas.
    merged: list[tuple[str, str]] = []
    for title, body in raw_blocks:
        is_orphan = _is_orphan_heading(title, body)
        if is_orphan and merged:
            prev_title, prev_body = merged[-1]
            merged[-1] = (prev_title, f"{prev_body}\n\n{title}\n{body}".strip())
        elif is_orphan:
            # Orphelin sans précédent — skip (jamais une recette autonome).
            continue
        else:
            merged.append((title, body))
    return merged


# ══════════════════════════════════════════════════════════════
# Détection sommaire / décoratif / vrai titre
# ══════════════════════════════════════════════════════════════


_SUMMARY_LINE_RE = re.compile(r"^[A-ZÉÀÂÎÔÛÇa-zéàâîôûç][\w\séàâîôûç']*\s+\d{1,4}\s*$")


def _is_summary_block(body: str) -> bool:
    """Détecte un bloc de sommaire (liste de plats avec numéros de page).

    Heuristique : > 10 lignes correspondant au pattern « NOM PAGE_NUM »
    OU les lignes sommaire représentent > 50 % des lignes non-vides.
    """
    if not body:
        return False
    lines = [line.strip() for line in body.splitlines() if line.strip()]
    if len(lines) < 5:
        return False
    summary_lines = sum(1 for line in lines if _SUMMARY_LINE_RE.match(line))
    if summary_lines >= 10:
        return True
    return summary_lines / len(lines) > 0.5


def _is_decorative_title(title: str) -> bool:
    """Détecte un titre décoratif (chapitre, section générique du livre)."""
    upper = title.strip().upper()
    if upper in _DECORATIVE_TITLES:
        return True
    # Sommaires intermédiaires (ex: « Bonbon Sesame 330 », « Pebe » mais
    # contenant juste une liste d'épices) — on les détectera via le body
    # plutôt que le titre seul.
    return False


_INLINE_TITLE_RE = re.compile(r"^([A-ZÉÀÂÎÔÛÇ][A-ZÉÀÂÎÔÛÇ0-9 \-'()]{2,80})\s*$")


_INGREDIENTS_POUR_RE = re.compile(
    r"\bINGR[ÉE]DIENTS?\s+POUR\s+([A-ZÉÀÂÎÔÛÇ][\w '\-éàâîôûç&]{2,60})\s*[:\.]?",
    flags=re.IGNORECASE,
)
_BON_A_SAVOIR_RE = re.compile(
    r"\bBON\s+[ÀA]\s+SAVOIR\s*:?\s*([A-Z][^.\n]{10,200})",
    flags=re.IGNORECASE,
)


def _extract_real_title_from_body(body: str) -> str | None:
    """Cherche le vrai titre de la recette dans le body (pour titres décoratifs).

    Stratégie en 4 passes (du plus précis au plus général) :

    1. **Pattern `INGREDIENTS POUR XXX`** — vol 1.2 : « INGREDIENTS POUR
       NJAMA NJAMA » → titre `NJAMA NJAMA`. Capture le nom du plat
       directement depuis l'en-tête typé.
    2. **Première ligne en MAJUSCULES non-décorative** (longueur 3-80).
       Cas standard : le pymupdf a saisi `## Incontournable...` mais
       le vrai nom est en MAJUSCULES juste après.
    3. **Mot avant INGREDIENTS/INGRÉDIENTS standalone** : remontée
       depuis l'en-tête de section.
    4. **Pattern `BON À SAVOIR`** : extraction sémantique via la
       phrase d'introduction (vol 1.2 : `BON A SAVOIR : KWANMKWALA est
       un plat... à base de...`).
    """
    if not body:
        return None

    # Passe 1 : INGREDIENTS POUR XXX
    m_pour = _INGREDIENTS_POUR_RE.search(body)
    if m_pour:
        candidate = m_pour.group(1).strip(" :.")
        # Skip si c'est juste « LA SAUCE » ou autre générique
        if candidate.upper() not in {"LA SAUCE", "LE PLAT", "LA RECETTE", "LES BEIGNETS"}:
            return candidate

    # Passe 2 + 3 : scan des 40 premières lignes
    lines = body.splitlines()
    for i, raw in enumerate(lines[:40]):
        line = raw.strip()
        if not line:
            continue
        m = _INLINE_TITLE_RE.match(line)
        if m and line.upper() not in _DECORATIVE_TITLES:
            # Skip si on est dans une liste d'ingrédients (commence par
            # bullet ou chiffre + mesure)
            if re.match(r"^[-*•]|^\d+\s*(?:kg|g|cl|ml|l)\b", line, flags=re.IGNORECASE):
                continue
            return line
        # Si on tombe sur INGREDIENTS standalone, on remonte chercher le titre.
        if re.match(r"^(INGR[ÉE]DIENTS?|Ingr[ée]dients?)\s*:?\s*$", line):
            for back in range(i - 1, max(-1, i - 8), -1):
                back_line = lines[back].strip()
                if back_line and back_line.upper() not in _DECORATIVE_TITLES:
                    if _INLINE_TITLE_RE.match(back_line) or len(back_line) <= 80:
                        return back_line
            break

    # Passe 4 : BON À SAVOIR (extraction sémantique)
    m_bon = _BON_A_SAVOIR_RE.search(body)
    if m_bon:
        sentence = m_bon.group(1).strip()
        # Heuristique : capture les 1-3 premiers mots en MAJUSCULES qui
        # ressemblent à un nom de plat (ex: « KWANMKWALA est un plat... »)
        m_first_word = re.match(r"^([A-ZÉÀÂÎÔÛÇ][\w'\-]{2,30}(?:\s+&?\s+[A-ZÉÀÂÎÔÛÇ][\w'\-]{2,30})?)", sentence)
        if m_first_word:
            return m_first_word.group(1).strip()

    return None


# ══════════════════════════════════════════════════════════════
# Détection région / catégorie
# ══════════════════════════════════════════════════════════════


def _detect_region(text: str) -> str | None:
    """Auto-détection région/ethnie camerounaise depuis le texte."""
    if not text:
        return None
    for pattern, region in _REGION_PATTERNS:
        if re.search(pattern, text, flags=re.IGNORECASE):
            return region
    return None


def _detect_category(name: str, body: str) -> str:
    """Auto-détection catégorie depuis le titre (priorité absolue) puis
    le body (fallback uniquement si le titre n'a rien matché).

    Refonte G2 V8 : la priorité TITRE est désormais stricte pour éviter
    qu'un mot ambigu dans le body (« poulet » dans une description
    culturelle) déclasse une recette correctement nommée.
    """
    name_lower = (name or "").lower()
    # Passe 1 — TITRE en priorité absolue (haut signal)
    for pattern, category in _CATEGORY_PATTERNS:
        if re.search(pattern, name_lower, flags=re.IGNORECASE):
            return category
    # Passe 2 — BODY en fallback (uniquement si rien dans titre)
    body_head = (body or "")[:500].lower()
    for pattern, category in _CATEGORY_PATTERNS:
        if re.search(pattern, body_head, flags=re.IGNORECASE):
            return category
    return "plat_principal"


# ══════════════════════════════════════════════════════════════
# Extraction structurée (description, ingredients, steps)
# ══════════════════════════════════════════════════════════════


def _split_sections(body: str) -> tuple[str, str, str]:
    """Split le body en (description, ingredients_block, steps_block).

    Trouve les en-têtes INGREDIENTS et PREPARATION/METHODE et coupe le
    body en 3 zones. Si une section manque, retourne la chaîne vide.

    Stratégie tolérante en 2 passes :
    1. **Standalone** (cas standard) : `(?:^|\\n)\\s*INGREDIENTS\\s*\\n`
       — l'en-tête est sur sa propre ligne (pattern propre PDF).
    2. **Inline** (fallback) : `\\bINGREDIENTS\\b` — l'en-tête est
       juste un mot-clé dans le texte (cas vol 1.2 FOUFOU RIZ où
       footers strip ont collé `INGREDIENTS  500g de riz  PREPARATION
       Trempez...` sur une seule grande ligne).
    """
    if not body:
        return "", "", ""

    ingredients_pattern = "|".join(_INGREDIENT_SECTION_PATTERNS)
    preparation_pattern = "|".join(_PREPARATION_SECTION_PATTERNS)

    # Passe 1 — match standalone (newline avant + après)
    ing_match = re.search(
        rf"(?:^|\n)\s*(?:{ingredients_pattern})\s*\n",
        body,
        flags=re.IGNORECASE,
    )
    inline_ing = False
    if not ing_match:
        # Passe 2 — fallback inline (juste un mot-clé)
        ing_match = re.search(
            rf"\b(?:{ingredients_pattern})",
            body,
            flags=re.IGNORECASE,
        )
        inline_ing = True

    if not ing_match:
        return body.strip(), "", ""

    prep_search_start = ing_match.end() if ing_match else 0
    # Passe 1 — match standalone PREPARATION
    prep_match = re.search(
        rf"(?:^|\n)\s*(?:{preparation_pattern})\s*\n",
        body[prep_search_start:],
        flags=re.IGNORECASE,
    )
    inline_prep = False
    if not prep_match:
        # Passe 2 — fallback inline
        prep_match = re.search(
            rf"\b(?:{preparation_pattern})",
            body[prep_search_start:],
            flags=re.IGNORECASE,
        )
        inline_prep = True
    prep_abs_start = prep_search_start + prep_match.start() if prep_match else None

    description = body[: ing_match.start()].strip()
    ingredients_start = ing_match.end()
    if prep_abs_start is not None:
        ingredients_block = body[ingredients_start:prep_abs_start].strip()
        steps_block = body[prep_search_start + prep_match.end() :].strip()
    else:
        ingredients_block = body[ingredients_start:].strip()
        steps_block = ""

    return description, ingredients_block, steps_block


_BULLET_LINE_RE = re.compile(r"^\s*[-*•]\s+(.+?)\s*$")
_NUMBERED_LINE_RE = re.compile(r"^\s*\d+[.)°]\s+(.+?)\s*$")


def _extract_ingredients(block: str) -> list[str]:
    """Extrait la liste d'ingrédients depuis le bloc INGREDIENTS.

    Stratégie en 2 passes :

    **Passe 1 — split par ligne** (cas standard, 80 % des recettes) :
    accepte les bullets (`-`, `*`, `•`), les lignes numérotées, ou une
    ligne par ingrédient. Concatène les lignes orphelines à l'item
    précédent (continuation visuelle après word-wrap PDF).

    **Passe 2 — split inline** (fallback) : si la passe 1 retourne
    < 2 items, on essaie de splitter le bloc entier sur les patterns
    inline `\\s*-\\s+` ou `,\\s*` (cas vol 1.2 : `INGREDIENTS - tapioca,
    - tomate, - cube` ou `INGREDIENTS : - tapioca - tomate - cube`).
    Récupère les recettes courtes type FRITES D'IGNAME, FOUFOU RIZ.
    """
    if not block:
        return []

    # ── Passe 1 : split par ligne ─────────────────────────────────
    items: list[str] = []
    current_buffer: list[str] = []

    def _flush_buffer() -> None:
        if current_buffer:
            joined = " ".join(current_buffer).strip()
            if joined and len(joined) >= 2:
                items.append(joined)
            current_buffer.clear()

    for raw in block.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            _flush_buffer()
            continue
        # Stop si on retombe sur un en-tête PREPARATION (sécurité)
        if re.match(
            r"^(PR[ÉE]PARATION|PREPARATION|Pr[ée]paration|M[ÉE]THODE|METHODE|M[ée]thode|[ÉE]TAPES|ETAPES|[ÉE]tapes)\s*:?\s*$",
            stripped,
            flags=re.IGNORECASE,
        ):
            break
        # Bullet ou numéroté → nouvel item
        m_bullet = _BULLET_LINE_RE.match(line)
        m_num = _NUMBERED_LINE_RE.match(line)
        if m_bullet or m_num:
            _flush_buffer()
            text = (m_bullet or m_num).group(1).strip()
            current_buffer.append(text)
            continue
        # Continuation de l'item précédent OU nouvel item sans bullet
        first_char = stripped[0]
        if current_buffer and (
            first_char.islower() or len(stripped) < 15 and not stripped[0].isdigit()
        ):
            current_buffer.append(stripped)
        else:
            _flush_buffer()
            current_buffer.append(stripped)
    _flush_buffer()

    # ── Passe 2 : fallback split inline si trop peu d'items ───────
    if len(items) < 2:
        items = _extract_ingredients_inline(block)

    return items


_INLINE_BULLET_SPLIT_RE = re.compile(r"\s+-\s+|\s*,\s+")


def _extract_ingredients_inline(block: str) -> list[str]:
    """Fallback : split inline du bloc INGREDIENTS sur `- ` ou `, `.

    Cas pratique vol 1.2 : recettes courtes (FRITES D'IGNAME, FOUFOU
    RIZ, IGNAMES BOUILLIES) où tous les ingrédients sont sur une seule
    ligne après `INGREDIENTS - ` ou `INGREDIENTS : ` :

        INGREDIENTS - 1 igname - huile de friture - sel
        → ['1 igname', 'huile de friture', 'sel']

    On strip aussi les éventuels en-têtes PREPARATION qui auraient pu
    fuiter dans le bloc.
    """
    if not block:
        return []
    # On retire les en-têtes PREPARATION éventuels en fin
    truncated = re.split(
        r"\b(?:PR[ÉE]PARATION|Pr[ée]paration|M[ÉE]THODE|M[ée]thode)\b",
        block,
        maxsplit=1,
        flags=re.IGNORECASE,
    )[0]
    # Strip header INGREDIENTS éventuel en début
    truncated = re.sub(
        r"^\s*(?:INGR[ÉE]DIENTS?|Ingr[ée]dients?)\s*[:.-]*\s*",
        "",
        truncated,
        flags=re.IGNORECASE,
    )
    # Concat sur une seule ligne
    flat = re.sub(r"\s+", " ", truncated).strip()
    if not flat:
        return []
    # Split sur ` - ` ou `, ` ou newlines
    raw_parts = _INLINE_BULLET_SPLIT_RE.split(flat)
    items: list[str] = []
    for p in raw_parts:
        p = p.strip(" -,;:")
        if p and len(p) >= 2 and len(p) <= 500:
            items.append(p)
    # Garde-fou : si on a > 30 items, c'est sans doute du texte mal
    # split (paragraphe entier) → rejet de la passe inline.
    if len(items) > 30:
        return []
    return items


def _extract_steps(block: str) -> list[str]:
    """Extrait les étapes depuis le bloc PREPARATION/METHODE.

    Stratégie : un paragraphe = une étape. Bullets et numérotation
    explicites sont des séparateurs forts. Les paragraphes (séparés par
    ligne vide) sont aussi des séparateurs.
    """
    if not block:
        return []
    steps: list[str] = []
    current_buffer: list[str] = []

    def _flush() -> None:
        if current_buffer:
            joined = " ".join(s.strip() for s in current_buffer if s.strip())
            joined = re.sub(r"\s+", " ", joined).strip()
            if joined and len(joined) >= 5:
                steps.append(joined)
            current_buffer.clear()

    for raw in block.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped:
            _flush()
            continue
        m_bullet = _BULLET_LINE_RE.match(line)
        m_num = _NUMBERED_LINE_RE.match(line)
        if m_bullet or m_num:
            _flush()
            text = (m_bullet or m_num).group(1).strip()
            current_buffer.append(text)
            continue
        # Sinon continuation du paragraphe en cours
        current_buffer.append(stripped)
    _flush()
    return steps


# ══════════════════════════════════════════════════════════════
# Slugification
# ══════════════════════════════════════════════════════════════


# ══════════════════════════════════════════════════════════════
# Détection astuce + retitling de titres tronqués
# ══════════════════════════════════════════════════════════════


# Mots clés titre qui signalent un conseil / tutoriel plutôt qu'une recette.
_ADVICE_TITLE_RE = re.compile(
    r"^\s*(comment|astuce[s]?|conseil[s]?|truc[s]?|secret[s]?)\b",
    flags=re.IGNORECASE,
)


def _is_advice_title(title: str) -> bool:
    """True si le titre commence par « Comment », « Astuce », etc."""
    if not title:
        return False
    return bool(_ADVICE_TITLE_RE.search(title))


# Déterminants en fin de titre qui révèlent une troncature pymupdf
# (le titre PDF d'origine est multi-ligne, l'extracteur n'a saisi qu'une
# partie). Ex : « Comment Reconnaitre Une » manque « BONNE VIANDE ».
_TRUNCATED_TITLE_TAIL_RE = re.compile(
    r"\b(une?|la|le|des?|les?|aux?|du|de|d'|l'|à)\s*$",
    flags=re.IGNORECASE,
)

# Lettre seule majuscule isolée en fin de titre — signe d'une coupure
# au milieu d'un mot par pymupdf. Ex: « Preparatio N » = « Preparation »
# où le « N » est resté orphelin sur la ligne suivante du PDF.
_TRUNCATED_LETTER_TAIL_RE = re.compile(r"\s+[A-Z]\s*$")


# ══════════════════════════════════════════════════════════════
# Filtre déchets — titres méta / chapitres / fragments
# ══════════════════════════════════════════════════════════════
#
# Refonte G2 V8 : 10-12 chunks "déchets" étaient acceptés à tort (titres
# de chapitre, fragments minuscules, titres orphelins type "Ingredients"
# ou "Owondo)"). On les filtre EN AMONT de l'extraction structurée pour
# garder un corpus propre.

# Titres méta connus (chapitres, sommaires, références internes du livre).
_TRASH_KNOWN_TITLES = frozenset(
    {
        # Sous-sections orphelines (au cas où l'orphan-merge n'aurait pas
        # joué — défense en profondeur)
        "INGREDIENTS",
        "INGRÉDIENTS",
        "PREPARATION",
        "PRÉPARATION",
        "METHODE",
        "MÉTHODE",
        "ETAPES",
        "ÉTAPES",
        # Titres de chapitres
        "LES COMPLEMENTS",
        "LES COMPLÉMENTS",
        "TENUE MILLITAIRE",  # typo sommaire vol 1.2
        "TENUE MILITAIRE",
        "PÂTISSERIES ET VIENOISERIES",  # typo livre
        "PATISSERIES ET VIENOISERIES",
        "PATISSERIES ET VIENNOISERIES",
        # Références internes
        "VOIR RECETTE DU MINTUMBA",
    }
)

# Fragment commençant par caractère minuscule (phrase incomplète, pas un titre).
_TRASH_LOWERCASE_START_RE = re.compile(r"^[a-zà-ÿ]")

# Titre dupliqué pymupdf — exige >= 2 MOTS dans le motif dupliqué pour
# éviter les faux positifs sur les noms typiques camerounais à
# répétition intentionnelle (« Njama Njama », « Kati Kati », « Kelen
# Kelen », « Pili Pili », « Eru Eru »). Vrai cas pathologique :
# « SAUCE TOMATE AUX Sauce Tomate Aux » (motif = 3 mots).
_TRASH_DUPLICATED_TITLE_RE = re.compile(
    r"^(\S+\s+\S+(?:\s+\S+)*?)\s+\1\s*$", flags=re.IGNORECASE
)


def _is_trash_title(title: str) -> bool:
    """True si le titre est un déchet à filtrer avant l'étape extraction.

    Stratégie en 5 passes :
    1. Vide ou < 3 chars (3 chars min pour préserver les recettes
       à nom court typiques africaines comme « ERU »).
    2. Titre méta connu (chapitre, sommaire, référence interne).
    3. Fragment commençant par minuscule (« la pâte », « est assaisonné… »).
    4. Parenthèses déséquilibrées (« Owondo) » seul).
    5. Titre dupliqué avec motif >= 2 mots (« SAUCE TOMATE AUX Sauce
       Tomate Aux ») — exclut les vraies recettes à nom répété
       (« Njama Njama »).
    """
    if not title:
        return True
    t = title.strip()
    if len(t) < 3:
        return True
    if t.upper() in _TRASH_KNOWN_TITLES:
        return True
    if _TRASH_LOWERCASE_START_RE.match(t):
        return True
    if t.count("(") != t.count(")"):
        return True
    if _TRASH_DUPLICATED_TITLE_RE.match(t):
        return True
    return False


def _strip_retitled_header_from_body(body: str, appended_part: str) -> str:
    """Si le retitling a joint `appended_part` (ex: « BONNE VIANDE ») au
    titre, on retire cette ligne du body pour éviter qu'elle réapparaisse
    en `step[0]` ou pollue la description.

    Retourne le body sans la ligne supprimée (1ère occurrence uniquement).
    """
    if not body or not appended_part:
        return body
    needle = appended_part.strip().upper()
    if not needle:
        return body
    new_lines: list[str] = []
    removed = False
    for line in body.splitlines():
        if not removed and line.strip().upper() == needle:
            removed = True
            continue
        new_lines.append(line)
    return "\n".join(new_lines)


def _maybe_retitle_truncated(title: str, body: str) -> str:
    """Tente de compléter un titre tronqué par pymupdf en joignant la
    première ligne MAJUSCULES du body.

    Déclenché par 2 signaux :
    1. **Déterminant orphelin en fin** (« Une », « Le », « Des », « À »…) :
       cas « Comment Reconnaitre Une » + body[0]=« BONNE VIANDE »
       → « Comment Reconnaitre Une Bonne Viande »
    2. **Titre commençant par « Comment »** : cas tronqués sans déterminant
       en queue mais où le body commence par un complément en MAJUSCULES :
       « Comment Faire Un Bon Piment » + body[0]=« DE TABLE »
       → « Comment Faire Un Bon Piment De Table »

    Renvoie le titre inchangé si aucun candidat ne match.
    """
    if not title:
        return title
    has_truncated_tail = bool(_TRUNCATED_TITLE_TAIL_RE.search(title))
    has_letter_tail = bool(_TRUNCATED_LETTER_TAIL_RE.search(title))
    starts_with_comment = bool(re.match(r"^\s*comment\b", title, flags=re.IGNORECASE))
    if not has_truncated_tail and not has_letter_tail and not starts_with_comment:
        return title
    for raw in body.splitlines()[:5]:
        line = raw.strip()
        if not line:
            continue
        # Ligne en MAJUSCULES raisonnable (3-60 chars)
        if line.isupper() and 3 <= len(line) <= 60:
            # Évite de coller un en-tête type INGREDIENTS / PREPARATION
            if line.upper() in {"INGREDIENTS", "INGRÉDIENTS", "PREPARATION", "PRÉPARATION", "METHODE", "MÉTHODE", "ETAPES", "ÉTAPES"}:
                return title
            return f"{title} {line.title()}".strip()
        # Ne sonde QUE la première ligne non-vide
        break
    return title


# ══════════════════════════════════════════════════════════════
# Slugification
# ══════════════════════════════════════════════════════════════


def _slugify(name: str) -> str:
    """Convertit un nom de recette en slug ASCII kebab-case (id_slug)."""
    if not name:
        return "recette-anonyme"
    # Strip accents / unicode → ASCII
    normalized = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    # Lowercase + replace non-alphanum par tiret
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", normalized.lower()).strip("-")
    # Cap longueur (80 chars max — match RecipeCanonical.id_slug)
    if len(slug) > 80:
        slug = slug[:80].rstrip("-")
    return slug or "recette-anonyme"


# ══════════════════════════════════════════════════════════════
# Parser orchestrateur
# ══════════════════════════════════════════════════════════════


def parse_master_file(path: Path, source_book: str) -> ParseResult:
    """Parse un master `.md` et retourne `ParseResult(accepted, rejected)`.

    Pipeline complet pour chaque bloc `## TITRE` du master :
    1. Strip footers + normalize bullets.
    2. Détection sommaire → REJET.
    3. Si titre décoratif → recherche du vrai titre dans body.
    4. Vérification structure (INGREDIENTS + PREPARATION).
    5. Extraction structurée + Pydantic validation.
    6. Dédup par slug (garde la version avec le plus long contenu).
    """
    if not path.exists():
        log.error("cuisine.parse.master_missing", path=str(path))
        return ParseResult()

    raw_md = path.read_text(encoding="utf-8")
    cleaned_md = _normalize_bullets(_strip_footers(raw_md))
    blocks = _split_master_into_blocks(cleaned_md)
    log.info("cuisine.parse.master_loaded", path=path.name, blocks=len(blocks))

    result = ParseResult()
    seen_slugs: dict[str, int] = {}  # slug → index dans accepted

    for idx, (raw_title, body) in enumerate(blocks):
        excerpt = body[:200].replace("\n", " ").strip()

        # Étape 2 — Sommaire ?
        if _is_summary_block(body):
            result.rejected.append(
                RejectionReport(
                    source_book=source_book,
                    section_index=idx,
                    raw_title=raw_title,
                    reason="sommaire",
                    raw_excerpt=excerpt,
                )
            )
            continue

        # Étape 3 — Titre décoratif ?
        effective_title = raw_title
        if _is_decorative_title(raw_title):
            real_title = _extract_real_title_from_body(body)
            if real_title:
                effective_title = real_title
            else:
                result.rejected.append(
                    RejectionReport(
                        source_book=source_book,
                        section_index=idx,
                        raw_title=raw_title,
                        reason="decorative_no_recipe",
                        detail="Titre décoratif, aucun vrai titre détecté dans le body",
                        raw_excerpt=excerpt,
                    )
                )
                continue

        # Étape 3.bis — Retitling de titre tronqué (« Comment Reconnaitre
        # Une » → « Comment Reconnaitre Une Bonne Viande »). Joint la
        # première ligne MAJUSCULES du body si le titre actuel finit par
        # un déterminant orphelin OU une lettre seule isolée (« Preparatio N »).
        # Si on a joint « BONNE VIANDE » au titre, on retire cette ligne
        # du body pour éviter qu'elle ne réapparaisse en `step[0]` (P2.1).
        _old_title = effective_title
        effective_title = _maybe_retitle_truncated(effective_title, body)
        if effective_title != _old_title:
            _appended = effective_title[len(_old_title):].strip()
            body = _strip_retitled_header_from_body(body, _appended)

        # Garde-fou : titre vraiment trop court / vide
        if not effective_title or len(effective_title.strip()) < 3:
            result.rejected.append(
                RejectionReport(
                    source_book=source_book,
                    section_index=idx,
                    raw_title=raw_title,
                    reason="empty_title",
                    raw_excerpt=excerpt,
                )
            )
            continue

        # Étape 3.ter — Filtre déchets (titres méta, fragments, doublons)
        # introduit G2 V8 après audit qualité des 117 chunks ingérés qui
        # a révélé ~10-12 entrées "déchets" (chapitres, fragments minuscules,
        # parenthèses déséquilibrées, titres dupliqués pymupdf).
        if _is_trash_title(effective_title):
            result.rejected.append(
                RejectionReport(
                    source_book=source_book,
                    section_index=idx,
                    raw_title=effective_title,
                    reason="trash_title",
                    detail="Titre déchet : chapitre méta / fragment / doublon",
                    raw_excerpt=excerpt,
                )
            )
            continue

        # Étape 4-5 — Extraction structurée
        description, ingredients_block, steps_block = _split_sections(body)
        ingredients = _extract_ingredients(ingredients_block)
        steps = _extract_steps(steps_block)

        # Étape 5.bis — Détection astuce (relaxe les exigences structurelles).
        # Une astuce peut n'avoir aucun ingrédient formel (conseil, description
        # d'ingrédient, tutoriel rapide). On la détecte via 3 signaux :
        # 1. Le titre commence par « Comment », « Astuce », « Conseil »…
        # 2. La catégorie détectée par `_detect_category` est `astuce`.
        # 3. Le body ne contient AUCUNE structure formelle INGREDIENTS +
        #    PREPARATION/bullets (heuristique `_body_has_recipe_structure`).
        #    Ce signal récupère les descriptions d'ingrédient pures (Odjom,
        #    Pebe) qui sont du texte libre sans listing structuré.
        category = _detect_category(effective_title, body)
        is_advice = (
            _is_advice_title(effective_title)
            or category == "astuce"
            or not _body_has_recipe_structure(body)
        )

        if not is_advice:
            # Mode recette stricte : exigences classiques
            if len(ingredients) < 2:
                result.rejected.append(
                    RejectionReport(
                        source_book=source_book,
                        section_index=idx,
                        raw_title=effective_title,
                        reason="no_ingredients",
                        detail=f"Seulement {len(ingredients)} ingrédient(s) extrait(s)",
                        raw_excerpt=excerpt,
                    )
                )
                continue
            if len(steps) < 1:
                result.rejected.append(
                    RejectionReport(
                        source_book=source_book,
                        section_index=idx,
                        raw_title=effective_title,
                        reason="no_steps",
                        detail="Aucune étape de préparation extraite",
                        raw_excerpt=excerpt,
                    )
                )
                continue
        else:
            # Mode astuce : exigences relaxées. Si la coupe `_split_sections`
            # n'a rien donné (pas de section INGREDIENTS formelle), on
            # extrait les steps depuis le body entier — un conseil de
            # type « Comment Reconnaître Une Bonne Viande » a son contenu
            # entièrement dans le body sans en-tête PREPARATION.
            if len(steps) < 1:
                steps = _extract_steps(body)
            if len(steps) < 1 and len(ingredients) < 1:
                # Dernier recours : compact le body brut en 1 step utile.
                body_compact = re.sub(r"\s+", " ", body).strip()
                if len(body_compact) >= 20:
                    # Cap à 1800 chars (sous le cap field steps=2000) pour
                    # garder la marge de troncature Pydantic.
                    steps = [body_compact[:1800]]
            # Garde-fou final : si toujours rien, rejet.
            if len(steps) < 1 and len(ingredients) < 1:
                result.rejected.append(
                    RejectionReport(
                        source_book=source_book,
                        section_index=idx,
                        raw_title=effective_title,
                        reason="empty_advice",
                        detail="Astuce sans contenu après tous fallbacks",
                        raw_excerpt=excerpt,
                    )
                )
                continue
            # Force la catégorie + accepte 0 ingrédient
            category = "astuce"

        # Étape 6 — Validation Pydantic
        try:
            full_text_for_region = f"{description}\n{body[:1000]}"
            # En mode astuce sans steps, on déplace les ingrédients
            # extraits vers `description` pour ne pas perdre l'info,
            # et on génère un step synthétique « Voir description ».
            final_steps = steps if steps else ["Voir description."]
            recipe = RecipeCanonical(
                id_slug=_slugify(effective_title),
                name=_titleize(effective_title),
                description=description if description else None,
                region=_detect_region(full_text_for_region),
                category=category,
                ingredients=ingredients,
                steps=final_steps,
                source=SourceMetadata(
                    owner=SOURCE_OWNER,
                    book=source_book,
                    section_index=idx,
                ),
            )
        except Exception as exc:  # noqa: BLE001 — Pydantic ValidationError tolérée
            result.rejected.append(
                RejectionReport(
                    source_book=source_book,
                    section_index=idx,
                    raw_title=effective_title,
                    reason="validation_error",
                    detail=str(exc)[:300],
                    raw_excerpt=excerpt,
                )
            )
            continue

        # Étape 7 — Dédup par slug
        if recipe.id_slug in seen_slugs:
            existing_idx = seen_slugs[recipe.id_slug]
            existing = result.accepted[existing_idx]
            existing_size = len(existing.description or "") + sum(
                len(s) for s in existing.steps
            )
            new_size = len(recipe.description or "") + sum(len(s) for s in recipe.steps)
            if new_size > existing_size:
                # Nouvelle version plus complète → on remplace, on rejette l'ancienne
                result.rejected.append(
                    RejectionReport(
                        source_book=existing.source.book,
                        section_index=existing.source.section_index,
                        raw_title=existing.name,
                        reason="duplicate",
                        detail=f"Version moins complète remplacée par section #{idx}",
                        raw_excerpt="",
                    )
                )
                result.accepted[existing_idx] = recipe
            else:
                # Ancienne version plus complète → on rejette la nouvelle
                result.rejected.append(
                    RejectionReport(
                        source_book=source_book,
                        section_index=idx,
                        raw_title=effective_title,
                        reason="duplicate",
                        detail=f"Doublon de slug `{recipe.id_slug}` (déjà section #{existing.source.section_index})",
                        raw_excerpt=excerpt,
                    )
                )
            continue

        seen_slugs[recipe.id_slug] = len(result.accepted)
        result.accepted.append(recipe)

    log.info(
        "cuisine.parse.master_done",
        path=path.name,
        accepted=len(result.accepted),
        rejected=len(result.rejected),
    )
    return result


def _titleize(name: str) -> str:
    """Met en forme le nom de recette pour l'affichage (capitalize words)."""
    name = name.strip()
    # Si tout en MAJUSCULES, on capitalize chaque mot ; sinon on garde tel quel
    if name == name.upper():
        return " ".join(w.capitalize() for w in name.split())
    return name


# ══════════════════════════════════════════════════════════════
# Génération du contenu RAG framé
# ══════════════════════════════════════════════════════════════


def _chunk_rag_content(content: str, max_chars: int = 1800) -> list[str]:
    """Découpe un content RAG en N chunks autonomes de <= max_chars chars.

    G2 V8 : remplace la troncature silencieuse pour les 34 recettes
    longues (Lait De Coco 24k chars, Pâtisseries 20k, Gâteau Yaourt 7k,
    etc.). Chaque chunk garde le **header recette** (Recette/Astuce +
    Région + Catégorie) pour rester sémantiquement complet du point de
    vue du retrieval pgvector — un user qui pose la question retrouvera
    le bon plat même si seul un des N chunks est le plus pertinent.

    Stratégie :
    1. Si content <= max_chars : un seul chunk identique (no-op).
    2. Extraire le header (jusqu'à la première blank line après
       `[Catégorie]`). Ce header est répliqué dans chaque chunk.
    3. Splitter le body par paragraphes (`\\n\\n`).
    4. Accumuler les paragraphes jusqu'à approcher max_chars.
    5. Si un paragraphe seul dépasse max_chars - header, le couper
       brutalement (cas pathologique rare type bloc fusionné géant).
    6. Si plus d'1 chunk, préfixer `[Partie N/Total]` au header pour
       que le LLM sache que c'est un fragment.
    """
    if len(content) <= max_chars:
        return [content]

    lines = content.split("\n")
    header_lines: list[str] = []
    body_start_idx = 0
    for i, line in enumerate(lines):
        header_lines.append(line)
        if line.strip() == "" and i > 0:
            body_start_idx = i + 1
            break
    if body_start_idx == 0:
        # Header non trouvé (content malformé) — fallback troncature simple
        return [content[: max_chars - 3] + "..."]

    header = "\n".join(header_lines).rstrip() + "\n\n"
    # Réservation pour le marker `[Partie 99/99]\n` ajouté après chunking.
    # 25 chars couvrent jusqu'à 99 parts (tous les cas réalistes).
    _MARKER_RESERVE = 25
    if len(header) + _MARKER_RESERVE >= max_chars - 50:
        # Pathologique : header + marker approche le cap → on tronque
        return [content[: max_chars - 3] + "..."]

    body_after_header = "\n".join(lines[body_start_idx:])
    paragraphs = [p.strip() for p in body_after_header.split("\n\n") if p.strip()]

    chunks: list[str] = []
    current_parts: list[str] = []
    current_size = len(header) + _MARKER_RESERVE
    # Taille max paragraphe = max - (header + marker + séparateur \n\n)
    max_para_size = max_chars - len(header) - _MARKER_RESERVE - 4
    effective_max = max_chars - _MARKER_RESERVE

    def _flush() -> None:
        nonlocal current_parts, current_size
        if current_parts:
            body = "\n\n".join(current_parts)
            chunks.append(header + body)
            current_parts = []
            current_size = len(header) + _MARKER_RESERVE

    for para in paragraphs:
        if len(para) > max_para_size:
            # Paragraphe géant — flush l'accumulé puis coupe brutalement
            _flush()
            for start in range(0, len(para), max_para_size):
                slice_ = para[start : start + max_para_size]
                chunks.append(header + slice_)
            continue
        added_size = len(para) + 4  # +4 pour le séparateur \n\n
        if current_size + added_size > effective_max and current_parts:
            _flush()
        current_parts.append(para)
        current_size += added_size
    _flush()

    if len(chunks) <= 1:
        return chunks if chunks else [content[: max_chars - 3] + "..."]

    # Préfixer chaque chunk avec [Partie N/Total]
    total = len(chunks)
    header_stripped = header.rstrip("\n")
    annotated_chunks: list[str] = []
    for i, c in enumerate(chunks, start=1):
        marker = f"[Partie {i}/{total}]"
        new_header = f"{header_stripped}\n{marker}\n\n"
        annotated_chunks.append(c.replace(header, new_header, 1))
    return annotated_chunks


def _build_rag_content(recipe: RecipeCanonical) -> str:
    """Construit le bloc texte qui sera embedded en pgvector.

    Format strict, lisible LLM. Le bandeau de tête est `[Recette]` pour
    une vraie recette et `[Astuce]` pour une `category="astuce"` (le LLM
    saura traiter le chunk différemment).

    La section `[Ingrédients]` est omise si la recette n'a aucun
    ingrédient — cas typique des astuces / descriptions / tutoriels.

    Format type recette :

        [Recette] {name}
        [Région] {region or "Cameroun"}
        [Catégorie] {category}

        [Description]
        {description or fallback}

        [Ingrédients]
        - {ing 1}
        - ...

        [Préparation]
        1. {step 1}
        2. ...
    """
    region = recipe.region or "Cameroun"
    category = recipe.category.replace("_", " ").title()
    is_advice = recipe.category == "astuce"
    fallback_desc = (
        "Astuce de cuisine camerounaise."
        if is_advice
        else "Recette traditionnelle camerounaise."
    )
    description = recipe.description or fallback_desc
    header_label = "Astuce" if is_advice else "Recette"

    parts: list[str] = [
        f"[{header_label}] {recipe.name}",
        f"[Région] {region}",
        f"[Catégorie] {category}",
        "",
        "[Description]",
        description,
        "",
    ]
    if recipe.ingredients:
        parts.append("[Ingrédients]")
        parts.extend(f"- {ing}" for ing in recipe.ingredients)
        parts.append("")
    parts.append("[Préparation]" if not is_advice else "[Étapes]")
    for i, step in enumerate(recipe.steps, start=1):
        parts.append(f"{i}. {step}")
    return "\n".join(parts)


# ══════════════════════════════════════════════════════════════
# Writers — JSON canoniques + report Markdown
# ══════════════════════════════════════════════════════════════


def write_canonicals(recipes: list[RecipeCanonical], canonical_dir: Path) -> None:
    """Écrit chaque recette dans `_canonical/{id_slug}.json` (UTF-8 indenté)."""
    canonical_dir.mkdir(parents=True, exist_ok=True)
    # Nettoyage des canoniques précédentes (idempotence du dry-run)
    for old in canonical_dir.glob("*.json"):
        old.unlink()
    for recipe in recipes:
        path = canonical_dir / f"{recipe.id_slug}.json"
        path.write_text(
            recipe.model_dump_json(indent=2, exclude_none=False),
            encoding="utf-8",
        )


def write_rejections(reports: list[RejectionReport], rejected_dir: Path) -> None:
    """Écrit chaque rejet dans `_rejected/{book}_{idx}_{reason}.json`."""
    rejected_dir.mkdir(parents=True, exist_ok=True)
    for old in rejected_dir.glob("*.json"):
        old.unlink()
    for r in reports:
        book_slug = _slugify(r.source_book)
        filename = f"{book_slug}_{r.section_index:03d}_{r.reason}.json"
        path = rejected_dir / filename
        path.write_text(
            json.dumps(r.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def write_validation_report(
    accepted: list[RecipeCanonical],
    rejected: list[RejectionReport],
    report_path: Path,
) -> None:
    """Génère le rapport de validation human-readable AVANT INSERT pgvector.

    Sections :
    1. Synthèse chiffrée (acceptées / rejetées).
    2. Distribution par région.
    3. Distribution par catégorie.
    4. Distribution par source_book.
    5. Échantillon 10 recettes acceptées (au hasard mais déterministe via tri slug).
    6. Détail des rejets groupés par raison.
    """
    report_path.parent.mkdir(parents=True, exist_ok=True)
    now_iso = datetime.now(UTC).isoformat()

    region_counts = Counter((r.region or "Non détectée") for r in accepted)
    category_counts = Counter(r.category for r in accepted)
    book_counts = Counter(r.source.book for r in accepted)
    rejection_counts = Counter(r.reason for r in rejected)

    lines: list[str] = [
        "# Rapport de validation — Expert Cuisine RAG (G2)",
        "",
        f"- **Date** : {now_iso}",
        f"- **Total acceptées** : **{len(accepted)}**",
        f"- **Total rejetées** : {len(rejected)}",
        f"- **Taux d'acceptation** : {len(accepted) / max(1, len(accepted) + len(rejected)) * 100:.1f} %",
        "",
        "## Distribution par source",
        "",
        "| Livre | Recettes acceptées |",
        "|---|---|",
    ]
    for book, count in book_counts.most_common():
        lines.append(f"| {book} | {count} |")

    lines += [
        "",
        "## Distribution par région détectée",
        "",
        "| Région / Ethnie | Recettes |",
        "|---|---|",
    ]
    for region, count in region_counts.most_common():
        lines.append(f"| {region} | {count} |")

    lines += [
        "",
        "## Distribution par catégorie",
        "",
        "| Catégorie | Recettes |",
        "|---|---|",
    ]
    for cat, count in category_counts.most_common():
        lines.append(f"| {cat} | {count} |")

    # Échantillon 10 recettes (tri par slug pour déterminisme + survol équilibré)
    sample = sorted(accepted, key=lambda r: r.id_slug)[:: max(1, len(accepted) // 10)][:10]
    lines += [
        "",
        "## Échantillon de 10 recettes acceptées (relire pour validation)",
        "",
    ]
    for i, recipe in enumerate(sample, start=1):
        lines += [
            f"### {i}. {recipe.name}",
            f"- **Région** : {recipe.region or 'Non détectée'}",
            f"- **Catégorie** : {recipe.category}",
            f"- **Source** : {recipe.source.book}, section #{recipe.source.section_index}",
            f"- **Ingrédients** ({len(recipe.ingredients)}) :",
        ]
        lines.extend(f"  - {ing}" for ing in recipe.ingredients[:10])
        if len(recipe.ingredients) > 10:
            lines.append(f"  - ... ({len(recipe.ingredients) - 10} autres)")
        lines += [
            f"- **Étapes** ({len(recipe.steps)}) :",
        ]
        for j, step in enumerate(recipe.steps[:5], start=1):
            preview = step[:200] + ("..." if len(step) > 200 else "")
            lines.append(f"  {j}. {preview}")
        if len(recipe.steps) > 5:
            lines.append(f"  ... ({len(recipe.steps) - 5} étapes restantes)")
        lines.append("")

    lines += [
        "",
        "## Rejets groupés par raison",
        "",
        "| Raison | Nombre |",
        "|---|---|",
    ]
    for reason, count in rejection_counts.most_common():
        lines.append(f"| `{reason}` | {count} |")

    lines += [
        "",
        "### Détail des 20 premiers rejets",
        "",
    ]
    for r in rejected[:20]:
        lines += [
            f"- **{r.source_book} #{r.section_index}** — `{r.reason}`",
            f"  - Titre brut : `{r.raw_title}`",
        ]
        if r.detail:
            lines.append(f"  - Détail : {r.detail}")
        if r.raw_excerpt:
            excerpt_short = r.raw_excerpt[:150] + ("..." if len(r.raw_excerpt) > 150 else "")
            lines.append(f"  - Extrait : {excerpt_short}")
        lines.append("")

    if len(rejected) > 20:
        lines.append(f"*... et {len(rejected) - 20} autres rejets, voir `_rejected/` pour le détail.*")

    report_path.write_text("\n".join(lines), encoding="utf-8")
    log.info("cuisine.report.written", path=str(report_path))


# ══════════════════════════════════════════════════════════════
# Embedding batch avec retry (réutilise le pattern G1)
# ══════════════════════════════════════════════════════════════


async def _embed_with_retry(
    provider, texts: list[str], *, task_type: str
) -> list[list[float]]:
    """Embed `texts` avec retry exponentiel honorant `retry_after`."""
    backoff = INITIAL_BACKOFF
    last_exc: Exception | None = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            response = await provider.embed(texts, task_type=task_type)
            return [v.values for v in response.vectors]
        except EmbeddingsRateLimitError as exc:
            wait = exc.retry_after if exc.retry_after else backoff
            log.warning(
                "cuisine.embed.rate_limit",
                attempt=attempt,
                wait_seconds=wait,
                provider=exc.provider,
            )
            await asyncio.sleep(wait)
            backoff *= 2
            last_exc = exc
        except EmbeddingsError as exc:
            log.warning(
                "cuisine.embed.retry",
                attempt=attempt,
                error=str(exc),
                wait_seconds=backoff,
            )
            await asyncio.sleep(backoff)
            backoff *= 2
            last_exc = exc
    raise RuntimeError(f"Embed failed after {MAX_RETRIES} attempts: {last_exc}")


# ══════════════════════════════════════════════════════════════
# Ingestion DB — INSERT pgvector idempotent
# ══════════════════════════════════════════════════════════════


async def _ingest_canonicals(
    canonical_dir: Path,
    *,
    provider,
    batch_size: int,
    force_reembed: bool,
) -> dict[str, int]:
    """Lit `_canonical/*.json`, embed, INSERT pgvector. Idempotent.

    Retourne `{"seen": N, "inserted": M}` où M = nb réellement insérés
    (les doublons SHA-256 sont absorbés par `ON CONFLICT DO NOTHING`).
    """
    files = sorted(canonical_dir.glob("*.json"))
    if not files:
        log.warning("cuisine.ingest.no_canonicals", dir=str(canonical_dir))
        return {"seen": 0, "inserted": 0}

    log.info(
        "cuisine.ingest.start",
        canonicals=len(files),
        batch_size=batch_size,
        force_reembed=force_reembed,
        provider=provider.name,
        dim=provider.dim,
        model=provider.default_model,
    )

    if provider.dim != settings.expert_corpus_embedding_dim:
        raise RuntimeError(
            f"Mismatch dim : provider {provider.name} dim={provider.dim} "
            f"mais settings.expert_corpus_embedding_dim={settings.expert_corpus_embedding_dim}. "
            f"Refus d'ingérer des vecteurs de mauvaise dim."
        )

    if force_reembed:
        async with AsyncSessionLocal() as db:
            result = await db.execute(
                delete(ExpertCorpusChunk).where(ExpertCorpusChunk.expert_slug == EXPERT_SLUG)
            )
            await db.commit()
            log.warning(
                "cuisine.force_reembed.delete_done",
                rows_deleted=result.rowcount or 0,
            )

    total_seen = 0
    total_inserted = 0
    started = time.monotonic()

    # Charge les recettes en RAM (volume max ~200 → OK)
    recipes: list[RecipeCanonical] = []
    for path in files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            recipes.append(RecipeCanonical.model_validate(data))
        except Exception as exc:  # noqa: BLE001
            log.warning("cuisine.ingest.canonical_load_failed", path=path.name, error=str(exc))

    # Cap pratique du provider Gemini text-embedding (2048 chars). On
    # cap à 1800 pour garder une marge sécurité confortable.
    # G2 V8 : remplacement de la troncature silencieuse par un CHUNKING
    # par paragraphe via `_chunk_rag_content()`. Les recettes longues
    # (Lait De Coco 24k, Pâtisseries 20k, Gâteau Yaourt 7k, etc. — 29 %
    # du corpus) deviennent N chunks autonomes (1 header recette répliqué
    # + N paragraphes). Chaque chunk reste retrouvable au retrieval.
    EMBED_TEXT_CAP = 1800

    # Étape pré-batch : génère TOUS les chunks pour TOUTES les recettes,
    # puis batche par chunk (et pas par recette) — important car une
    # recette peut générer 1-13 chunks et il faut respecter le batch_size
    # du provider Vertex AI.
    all_chunks: list[tuple[RecipeCanonical, int, int, str]] = []
    for recipe in recipes:
        full_content = _build_rag_content(recipe)
        recipe_chunks = _chunk_rag_content(full_content, max_chars=EMBED_TEXT_CAP)
        chunk_total = len(recipe_chunks)
        for chunk_idx, chunk_content in enumerate(recipe_chunks):
            all_chunks.append((recipe, chunk_idx, chunk_total, chunk_content))

    log.info(
        "cuisine.ingest.chunked",
        recipes=len(recipes),
        chunks_total=len(all_chunks),
        chunks_avg_per_recipe=round(len(all_chunks) / max(1, len(recipes)), 2),
        multi_chunk_recipes=sum(1 for c in all_chunks if c[2] > 1) // max(1, max((c[2] for c in all_chunks), default=1)),
    )

    # Batch loop sur les CHUNKS (pas sur les recettes)
    for batch_start in range(0, len(all_chunks), batch_size):
        batch_chunks = all_chunks[batch_start : batch_start + batch_size]
        contents = [c[3] for c in batch_chunks]
        shas = [hashlib.sha256(c.encode("utf-8")).hexdigest() for c in contents]
        vectors = await _embed_with_retry(provider, contents, task_type="RETRIEVAL_DOCUMENT")
        if len(vectors) != len(contents):
            raise RuntimeError(
                f"Embed mismatch: {len(vectors)} vecteurs pour {len(contents)} textes"
            )

        now = datetime.now(UTC)
        rows = [
            {
                "expert_slug": EXPERT_SLUG,
                "content": content,
                "content_sha256": sha,
                "embedding": vec,
                "embedding_model": provider.default_model,
                "source": SOURCE_TAG,
                "language_pair": None,
                "metadata_json": {
                    "name": recipe.name,
                    "id_slug": recipe.id_slug,
                    "region": recipe.region,
                    "category": recipe.category,
                    "owner": recipe.source.owner,
                    "book": recipe.source.book,
                    "section_index": recipe.source.section_index,
                    "n_ingredients": len(recipe.ingredients),
                    "n_steps": len(recipe.steps),
                    "chunk_index": chunk_idx,
                    "chunk_total": chunk_total,
                },
                "created_at": now,
            }
            for (recipe, chunk_idx, chunk_total, content), sha, vec in zip(
                batch_chunks, shas, vectors, strict=True
            )
        ]

        async with AsyncSessionLocal() as db:
            # Compteur précis : `result.rowcount` retourne `-1` avec
            # certains drivers psycopg sur `ON CONFLICT DO NOTHING` quand
            # le driver ne peut pas savoir combien de lignes ont vraiment
            # été insérées. On compte AVANT et APRÈS via un `SELECT
            # COUNT(*)` pour obtenir le vrai delta — coût négligeable
            # (~1ms) vs le batch embed (~secondes).
            count_before_row = await db.execute(
                text(
                    "SELECT COUNT(*) FROM expert_corpus_chunks "
                    "WHERE expert_slug = :slug"
                ).bindparams(slug=EXPERT_SLUG)
            )
            count_before = int(count_before_row.scalar_one())
            stmt = pg_insert(ExpertCorpusChunk.__table__).values(rows)
            stmt = stmt.on_conflict_do_nothing(
                index_elements=["expert_slug", "content_sha256"]
            )
            await db.execute(stmt)
            await db.commit()
            count_after_row = await db.execute(
                text(
                    "SELECT COUNT(*) FROM expert_corpus_chunks "
                    "WHERE expert_slug = :slug"
                ).bindparams(slug=EXPERT_SLUG)
            )
            count_after = int(count_after_row.scalar_one())
            inserted = max(0, count_after - count_before)

        total_seen += len(batch_chunks)
        total_inserted += inserted
        if total_seen % PROGRESS_EVERY == 0 or total_seen == len(all_chunks):
            elapsed = time.monotonic() - started
            log.info(
                "cuisine.ingest.progress",
                seen=total_seen,
                inserted=total_inserted,
                duplicates_skipped=total_seen - total_inserted,
                elapsed_s=round(elapsed, 1),
            )

    elapsed = time.monotonic() - started
    log.info(
        "cuisine.ingest.done",
        seen=total_seen,
        inserted=total_inserted,
        duplicates_skipped=total_seen - total_inserted,
        elapsed_s=round(elapsed, 1),
    )
    return {"seen": total_seen, "inserted": total_inserted}


# ══════════════════════════════════════════════════════════════
# Main runner
# ══════════════════════════════════════════════════════════════


def _discover_master_files(source_dir: Path) -> list[tuple[Path, str]]:
    """Trouve tous les `RECETTES DETAILLEES X/RECETTES DETAILLEES X.md`.

    Retourne `[(path, source_book), ...]` triés par nom.
    """
    if not source_dir.exists():
        log.error("cuisine.source.missing", path=str(source_dir))
        return []
    masters: list[tuple[Path, str]] = []
    for sub in sorted(source_dir.iterdir()):
        if not sub.is_dir():
            continue
        candidate = sub / f"{sub.name}.md"
        if candidate.exists():
            # Source book label : strip le suffixe ".pdf" éventuel
            book_label = sub.name
            masters.append((candidate, book_label))
    return masters


async def run(args: argparse.Namespace) -> None:
    source_dir = Path(args.source_dir).resolve()
    canonical_dir = Path(args.canonical_dir).resolve()
    rejected_dir = Path(args.rejected_dir).resolve()
    report_path = Path(args.report).resolve()

    log.info(
        "cuisine.run.start",
        source_dir=str(source_dir),
        canonical_dir=str(canonical_dir),
        rejected_dir=str(rejected_dir),
        dry_run=args.dry_run,
        ingest=args.ingest,
        validate_only=args.validate_only,
        force_reembed=args.force_reembed,
    )

    # Mode --validate-only : ne refait pas le parsing, lit juste _canonical/
    # et regenère le rapport.
    if args.validate_only:
        log.info("cuisine.validate_only.start")
        recipes_existing: list[RecipeCanonical] = []
        for path in sorted(canonical_dir.glob("*.json")):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                recipes_existing.append(RecipeCanonical.model_validate(data))
            except Exception as exc:  # noqa: BLE001
                log.warning("cuisine.validate.skip_invalid", file=path.name, error=str(exc))
        write_validation_report(recipes_existing, [], report_path)
        log.info("cuisine.validate_only.done", recipes=len(recipes_existing))
        return

    # Mode --ingest seul : skip le parsing, ingère directement depuis _canonical/
    if args.ingest and not args.dry_run:
        provider = get_embeddings_provider()
        stats = await _ingest_canonicals(
            canonical_dir,
            provider=provider,
            batch_size=args.batch_size,
            force_reembed=args.force_reembed,
        )
        log.info("cuisine.run.done_ingest", **stats)
        return

    # Mode dry-run / parsing standard
    masters = _discover_master_files(source_dir)
    if not masters:
        log.error("cuisine.no_masters_found", source_dir=str(source_dir))
        return

    log.info("cuisine.masters_found", count=len(masters), books=[b for _, b in masters])

    all_accepted: list[RecipeCanonical] = []
    all_rejected: list[RejectionReport] = []
    for path, book_label in masters:
        result = parse_master_file(path, book_label)
        all_accepted.extend(result.accepted)
        all_rejected.extend(result.rejected)

    # Dédup cross-book (priorité au plus récent = vol 3 si plus récent)
    seen_global: dict[str, int] = {}
    final_accepted: list[RecipeCanonical] = []
    for recipe in all_accepted:
        if recipe.id_slug in seen_global:
            existing = final_accepted[seen_global[recipe.id_slug]]
            new_size = len(recipe.description or "") + sum(len(s) for s in recipe.steps)
            existing_size = len(existing.description or "") + sum(len(s) for s in existing.steps)
            if new_size > existing_size:
                all_rejected.append(
                    RejectionReport(
                        source_book=existing.source.book,
                        section_index=existing.source.section_index,
                        raw_title=existing.name,
                        reason="duplicate",
                        detail="Cross-book dédup : version moins complète remplacée",
                    )
                )
                final_accepted[seen_global[recipe.id_slug]] = recipe
            else:
                all_rejected.append(
                    RejectionReport(
                        source_book=recipe.source.book,
                        section_index=recipe.source.section_index,
                        raw_title=recipe.name,
                        reason="duplicate",
                        detail=f"Cross-book : doublon de slug `{recipe.id_slug}` (déjà dans {existing.source.book})",
                    )
                )
        else:
            seen_global[recipe.id_slug] = len(final_accepted)
            final_accepted.append(recipe)

    write_canonicals(final_accepted, canonical_dir)
    write_rejections(all_rejected, rejected_dir)
    write_validation_report(final_accepted, all_rejected, report_path)

    log.info(
        "cuisine.parse.complete",
        accepted=len(final_accepted),
        rejected=len(all_rejected),
        canonical_dir=str(canonical_dir),
        report=str(report_path),
    )

    if args.dry_run:
        log.info(
            "cuisine.dry_run.done",
            note="Relis _validation_report.md puis lance --ingest pour pgvector.",
        )
        return

    # Mode standard sans --dry-run et sans --ingest = parsing seul (pour CI)
    if not args.ingest:
        log.info("cuisine.run.parse_only_done")
        return


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pipeline d'ingestion corpus expert Cuisine (G2) — recettes camerounaises propriétaires.",
    )
    parser.add_argument(
        "--source-dir",
        default=str(DEFAULT_SOURCE_DIR),
        help=f"Dossier racine des extracted cuisines (défaut: {DEFAULT_SOURCE_DIR})",
    )
    parser.add_argument(
        "--canonical-dir",
        default=str(DEFAULT_CANONICAL_DIR),
        help=f"Output _canonical/ (défaut: {DEFAULT_CANONICAL_DIR})",
    )
    parser.add_argument(
        "--rejected-dir",
        default=str(DEFAULT_REJECTED_DIR),
        help=f"Output _rejected/ (défaut: {DEFAULT_REJECTED_DIR})",
    )
    parser.add_argument(
        "--report",
        default=str(DEFAULT_REPORT_PATH),
        help=f"Output _validation_report.md (défaut: {DEFAULT_REPORT_PATH})",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Taille de batch embed Vertex AI (cap 100).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Génère uniquement JSON canoniques + rapport, PAS d'embed ni d'INSERT DB.",
    )
    parser.add_argument(
        "--validate-only",
        action="store_true",
        help="Régénère uniquement le rapport depuis les JSON canoniques existants.",
    )
    parser.add_argument(
        "--ingest",
        action="store_true",
        help="Lance l'ingestion pgvector depuis _canonical/ (présuppose --dry-run préalable validé).",
    )
    parser.add_argument(
        "--force-reembed",
        action="store_true",
        help="DELETE expert_slug='cooking' avant INSERT (switch corpus complet).",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.batch_size > 100:
        print(
            "WARN: batch-size > 100 — Gemini API tronquera silencieusement.",
            file=sys.stderr,
        )
    # Windows : psycopg async refuse ProactorEventLoop (défaut Py 3.8+).
    # Pattern aligné `app/main.py`, `migrations/env.py`, `import_expert_corpus_langues.py`.
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(run(args))
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
