"""
Tests unitaires — `scripts/import_expert_corpus_cuisine.py` (G2).

Valide les briques unitaires du pipeline d'ingestion des recettes
camerounaises propriétaires (livres Loth Ivan / Nexyalabs) sans toucher
au dataset disque ni à la DB :

- Helpers de pré-traitement (`_strip_footers`, `_normalize_bullets`).
- Découpe master → blocs (`_split_master_into_blocks`,
  `_is_orphan_heading`, `_body_has_recipe_structure`).
- Détection sommaire / décoratif / vrai titre.
- Détection région / catégorie.
- Extraction structurée (`_split_sections`, `_extract_ingredients`,
  `_extract_steps`, `_extract_ingredients_inline`).
- `_slugify` / `_titleize`.
- Validation Pydantic `RecipeCanonical`.
- `_build_rag_content` + SHA-256 déterministe.
- `_embed_with_retry` : succès immédiat, retry sur rate-limit.
- `parse_master_file` orchestrateur end-to-end.
- CLI argparse : defaults, flags `--dry-run`, `--ingest`,
  `--validate-only`, `--force-reembed`, paths custom.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from app.ai.embeddings.base import EmbeddingsRateLimitError
from scripts.import_expert_corpus_cuisine import (
    RecipeCanonical,
    SourceMetadata,
    _body_has_recipe_structure,
    _build_rag_content,
    _detect_category,
    _detect_region,
    _embed_with_retry,
    _extract_ingredients,
    _extract_ingredients_inline,
    _extract_real_title_from_body,
    _extract_steps,
    _is_decorative_title,
    _is_orphan_heading,
    _is_summary_block,
    _normalize_bullets,
    _parse_args,
    _slugify,
    _split_master_into_blocks,
    _split_sections,
    _strip_footers,
    _titleize,
    parse_master_file,
)


# ══════════════════════════════════════════════════════════════
# Helpers de pré-traitement
# ══════════════════════════════════════════════════════════════


def test_strip_footers_removes_pdf_footers() -> None:
    """Le footer `Recettes camerounaises\\nPage N` doit être supprimé."""
    raw = "Contenu utile\nRecettes camerounaises\nPage 41\nSuite recette"
    cleaned = _strip_footers(raw)
    assert "Recettes camerounaises" not in cleaned
    assert "Page 41" not in cleaned
    assert "Contenu utile" in cleaned
    assert "Suite recette" in cleaned


def test_strip_footers_collapses_triple_newlines() -> None:
    """Les newlines triples doivent être collapsées en double après strip."""
    raw = "ligne 1\n\n\n\nligne 2"
    cleaned = _strip_footers(raw)
    assert "\n\n\n" not in cleaned


def test_strip_footers_handles_empty_input() -> None:
    assert _strip_footers("") == ""


def test_normalize_bullets_unicode_to_dash() -> None:
    """Les bullets unicode (• ‣ ◦) doivent être converties en `-`."""
    raw = "• item un\n‣ item deux\n◦ item trois"
    out = _normalize_bullets(raw)
    assert "-" in out
    assert "•" not in out
    assert "‣" not in out
    assert "◦" not in out


def test_normalize_bullets_collapses_horizontal_spaces() -> None:
    """Les espaces multiples horizontaux doivent être réduits à un seul."""
    raw = "mot1     mot2\t\tmot3"
    out = _normalize_bullets(raw)
    assert "  " not in out
    assert "mot1 mot2 mot3" in out


# ══════════════════════════════════════════════════════════════
# _body_has_recipe_structure
# ══════════════════════════════════════════════════════════════


def test_body_has_recipe_structure_with_ingredients_and_preparation() -> None:
    body = "INGREDIENTS\n- tomate\n- oignon\n\nPREPARATION\nFaire cuire."
    assert _body_has_recipe_structure(body) is True


def test_body_has_recipe_structure_with_ingredients_and_bullets() -> None:
    """INGREDIENTS + bullets seuls (sans PREPARATION) suffit."""
    body = "INGREDIENTS\n- viande\n- huile\n- sel\n"
    assert _body_has_recipe_structure(body) is True


def test_body_has_recipe_structure_decoratif_only() -> None:
    """Un body purement décoratif (intro de chapitre) ne doit PAS matcher."""
    body = "Voici un chapitre dédié aux entrées camerounaises traditionnelles."
    assert _body_has_recipe_structure(body) is False


def test_body_has_recipe_structure_handles_empty() -> None:
    assert _body_has_recipe_structure("") is False


# ══════════════════════════════════════════════════════════════
# _is_orphan_heading + _split_master_into_blocks
# ══════════════════════════════════════════════════════════════


def test_is_orphan_heading_always_orphan_titles() -> None:
    """INGREDIENTS, PREPARATION, METHODE, ETAPES → toujours orphelins."""
    for title in ("INGREDIENTS", "INGRÉDIENTS", "PREPARATION", "METHODE", "ETAPES"):
        assert _is_orphan_heading(title) is True


def test_is_orphan_heading_decorative_with_recipe_preserved() -> None:
    """`INCONTOURNABLE...` avec body de recette → PAS orphelin (préservé)."""
    body_recipe = "NJAMA NJAMA\n\nINGREDIENTS\n- légumes\n- viande\n\nPREPARATION\nCuire."
    assert _is_orphan_heading("INCONTOURNABLE DE LA CUISINE CAMEROUNAISE", body_recipe) is False


def test_is_orphan_heading_decorative_without_recipe_merged() -> None:
    """`INCONTOURNABLE...` sans body recette → orphelin (à fusionner)."""
    body_decorative = "Voici un chapitre dédié aux plats traditionnels."
    assert _is_orphan_heading("INCONTOURNABLE DE LA CUISINE CAMEROUNAISE", body_decorative) is True


def test_is_orphan_heading_real_recipe_name_not_orphan() -> None:
    assert _is_orphan_heading("PEPPER SOUP") is False
    assert _is_orphan_heading("FOUFOU RIZ") is False


def test_split_master_into_blocks_basic() -> None:
    md = "# Titre Livre\n\n## RECETTE A\nbody A\n\n## RECETTE B\nbody B"
    blocks = _split_master_into_blocks(md)
    assert len(blocks) == 2
    assert blocks[0][0] == "RECETTE A"
    assert "body A" in blocks[0][1]
    assert blocks[1][0] == "RECETTE B"


def test_split_master_into_blocks_merges_always_orphan_subsection() -> None:
    """Une H2 INGREDIENTS isolée doit être fusionnée au bloc précédent."""
    md = (
        "## NDOLE\n"
        "Une recette traditionnelle\n\n"
        "## INGREDIENTS\n"
        "- légumes\n- huile\n\n"
        "## PREPARATION\n"
        "Faire cuire à feu doux."
    )
    blocks = _split_master_into_blocks(md)
    # Les 3 H2 doivent fusionner en un seul bloc.
    assert len(blocks) == 1
    assert blocks[0][0] == "NDOLE"
    assert "INGREDIENTS" in blocks[0][1]
    assert "PREPARATION" in blocks[0][1]


def test_split_master_into_blocks_skips_orphan_without_previous() -> None:
    """Un orphelin en tout début (sans précédent) doit être skip silencieusement."""
    md = "## INGREDIENTS\n- truc\n\n## VRAIE RECETTE\nbody"
    blocks = _split_master_into_blocks(md)
    # INGREDIENTS skipé → seul VRAIE RECETTE reste.
    assert len(blocks) == 1
    assert blocks[0][0] == "VRAIE RECETTE"


def test_split_master_into_blocks_preserves_decorative_with_recipe() -> None:
    """`Incontournable...` avec INGREDIENTS + PREPARATION → préservé (sera renommé downstream)."""
    md = (
        "## RECETTE PRECEDENTE\nbody\n\n"
        "## INCONTOURNABLE DE LA CUISINE CAMEROUNAISE\n"
        "NJAMA NJAMA\n\n"
        "INGREDIENTS\n- légumes\n- viande\n\n"
        "PREPARATION\nCuire 30 min."
    )
    blocks = _split_master_into_blocks(md)
    # Les 2 doivent rester séparés (le décoratif a une vraie recette).
    assert len(blocks) == 2
    assert blocks[1][0] == "INCONTOURNABLE DE LA CUISINE CAMEROUNAISE"


def test_split_master_into_blocks_empty_input() -> None:
    assert _split_master_into_blocks("") == []
    assert _split_master_into_blocks("Pas de heading H2 ici.") == []


# ══════════════════════════════════════════════════════════════
# _is_summary_block
# ══════════════════════════════════════════════════════════════


def test_is_summary_block_detects_long_list_with_pages() -> None:
    """Bloc avec >= 10 lignes `NOM PAGE` → sommaire."""
    body = "\n".join(f"Recette numero {i} {i * 10}" for i in range(1, 12))
    assert _is_summary_block(body) is True


def test_is_summary_block_rejects_short_recipe() -> None:
    body = "INGREDIENTS\n- tomate\n- sel\n\nPREPARATION\nMélanger."
    assert _is_summary_block(body) is False


def test_is_summary_block_handles_empty() -> None:
    assert _is_summary_block("") is False


# ══════════════════════════════════════════════════════════════
# _is_decorative_title
# ══════════════════════════════════════════════════════════════


def test_is_decorative_title_known_chapters() -> None:
    for title in (
        "ENTREES",
        "PLATS",
        "PATISSERIES",
        "SAUCES",
        "INCONTOURNABLE DE LA CUISINE CAMEROUNAISE",
        "SOMMAIRE",
    ):
        assert _is_decorative_title(title) is True


def test_is_decorative_title_real_recipe_name() -> None:
    for title in ("Pepper Soup", "Ndole", "Foufou Riz", "EKWANG"):
        assert _is_decorative_title(title) is False


# ══════════════════════════════════════════════════════════════
# _extract_real_title_from_body (4 passes)
# ══════════════════════════════════════════════════════════════


def test_extract_real_title_pattern_ingredients_pour() -> None:
    """Passe 1 — `INGREDIENTS POUR XXX` → titre = `XXX`."""
    body = "Une intro\n\nINGREDIENTS POUR NJAMA NJAMA\n- légumes\n- viande"
    assert _extract_real_title_from_body(body) == "NJAMA NJAMA"


def test_extract_real_title_pattern_ingredients_pour_skips_generic() -> None:
    """`INGREDIENTS POUR LA SAUCE` est trop générique → fallback passe 2."""
    body = "INGREDIENTS POUR LA SAUCE\n- tomate\n- piment"
    # Pas d'autre indice → None (pas de retour générique).
    result = _extract_real_title_from_body(body)
    assert result != "LA SAUCE"


def test_extract_real_title_uppercase_first_line() -> None:
    """Passe 2 — première ligne en MAJUSCULES non décorative."""
    body = "PEPPER SOUP\n\nUne recette du Cameroun\n\nINGREDIENTS\n- poisson\n- ail"
    assert _extract_real_title_from_body(body) == "PEPPER SOUP"


def test_extract_real_title_before_ingredients() -> None:
    """Passe 3 — remontée depuis INGREDIENTS standalone."""
    body = "Quelque intro courte\n\nKWEM\n\nINGREDIENTS\n- feuilles\n- huile"
    title = _extract_real_title_from_body(body)
    assert title in {"KWEM", "Quelque intro courte"}  # tolérance ordre passes


def test_extract_real_title_bon_a_savoir_pattern() -> None:
    """Passe 4 — `BON A SAVOIR : XXXX est un plat...`."""
    body = (
        "Pas de titre majuscule clair.\n\n"
        "BON A SAVOIR : KWANMKWALA est un plat traditionnel à base de manioc."
    )
    result = _extract_real_title_from_body(body)
    assert result is not None
    assert "KWANMKWALA" in result.upper()


def test_extract_real_title_returns_none_when_nothing_found() -> None:
    body = "juste du texte minuscule sans aucun titre exploitable."
    assert _extract_real_title_from_body(body) is None


# ══════════════════════════════════════════════════════════════
# _detect_region
# ══════════════════════════════════════════════════════════════


def test_detect_region_specific_ethnic() -> None:
    assert _detect_region("Recette traditionnelle Bassa") == "Bassa"
    assert _detect_region("Plat Bamileke ancestral") == "Bamileke"
    assert _detect_region("Spécialité Beti / Ewondo") == "Beti"


def test_detect_region_generic_cameroun_fallback() -> None:
    assert _detect_region("Cuisine du Cameroun") == "Cameroun"


def test_detect_region_returns_none_when_no_match() -> None:
    assert _detect_region("Texte sans aucune région") is None
    assert _detect_region("") is None


# ══════════════════════════════════════════════════════════════
# _detect_category
# ══════════════════════════════════════════════════════════════


def test_detect_category_patisserie_from_name() -> None:
    assert _detect_category("Gâteau au chocolat", "") == "patisserie"
    assert _detect_category("Beignets soufflés", "") == "patisserie"


def test_detect_category_sauce_from_name() -> None:
    assert _detect_category("Sauce arachide", "") == "sauce"


def test_detect_category_accompagnement() -> None:
    assert _detect_category("Foufou de manioc", "") == "accompagnement"
    assert _detect_category("Bâton de manioc", "") == "accompagnement"


def test_detect_category_default_plat_principal() -> None:
    assert _detect_category("Recette mystère", "body neutre") == "plat_principal"


# ══════════════════════════════════════════════════════════════
# _split_sections
# ══════════════════════════════════════════════════════════════


def test_split_sections_standalone_pattern() -> None:
    body = (
        "Une description courte.\n\n"
        "INGREDIENTS\n- tomate\n- oignon\n\n"
        "PREPARATION\nCuire à feu doux."
    )
    desc, ing, steps = _split_sections(body)
    assert "description courte" in desc
    assert "tomate" in ing
    assert "oignon" in ing
    assert "Cuire" in steps


def test_split_sections_inline_fallback() -> None:
    """Cas vol 1.2 : INGREDIENTS et PREPARATION inline (footers strippés)."""
    body = "INGREDIENTS - tapioca - sel - eau PREPARATION Mélanger le tout."
    desc, ing, steps = _split_sections(body)
    assert "tapioca" in ing or "tapioca" in steps  # selon où se fait le cut
    assert "Mélanger" in steps


def test_split_sections_missing_preparation() -> None:
    """Pas de section PREPARATION → steps vide, ingredients OK."""
    body = "intro\n\nINGREDIENTS\n- tomate\n- sel\n"
    desc, ing, steps = _split_sections(body)
    assert "tomate" in ing
    assert steps == ""


def test_split_sections_empty_body() -> None:
    assert _split_sections("") == ("", "", "")


# ══════════════════════════════════════════════════════════════
# _extract_ingredients
# ══════════════════════════════════════════════════════════════


def test_extract_ingredients_bullet_list() -> None:
    block = "- tomate\n- oignon\n- sel\n- huile"
    items = _extract_ingredients(block)
    assert items == ["tomate", "oignon", "sel", "huile"]


def test_extract_ingredients_numbered_list() -> None:
    block = "1. tomate\n2. oignon\n3. sel"
    items = _extract_ingredients(block)
    assert items == ["tomate", "oignon", "sel"]


def test_extract_ingredients_inline_fallback() -> None:
    """Cas FRITES D'IGNAME : tout sur une ligne après INGREDIENTS."""
    block = "1 igname - huile de friture - sel"
    items = _extract_ingredients_inline(block)
    assert "1 igname" in items
    assert "huile de friture" in items
    assert "sel" in items


def test_extract_ingredients_inline_strips_preparation_tail() -> None:
    block = "tapioca - sel PREPARATION mélanger"
    items = _extract_ingredients_inline(block)
    # Pas de `mélanger` dans la liste (coupé au PREPARATION).
    assert all("mélanger" not in i.lower() for i in items)


def test_extract_ingredients_handles_empty() -> None:
    assert _extract_ingredients("") == []


def test_extract_ingredients_inline_too_many_items_rejected() -> None:
    """Garde-fou : > 30 items → rejet de la passe inline."""
    block = ", ".join(f"item{i}" for i in range(40))
    items = _extract_ingredients_inline(block)
    assert items == []


# ══════════════════════════════════════════════════════════════
# _extract_steps
# ══════════════════════════════════════════════════════════════


def test_extract_steps_paragraph_split() -> None:
    block = "Étape 1 : préparer les légumes.\n\nÉtape 2 : faire cuire 10 min.\n\nÉtape 3 : servir chaud."
    steps = _extract_steps(block)
    assert len(steps) == 3
    assert "préparer les légumes" in steps[0]
    assert "servir chaud" in steps[2]


def test_extract_steps_numbered_steps() -> None:
    block = "1. Laver les légumes.\n2. Couper en dés.\n3. Faire revenir."
    steps = _extract_steps(block)
    assert len(steps) == 3
    assert "Laver" in steps[0]


def test_extract_steps_handles_empty() -> None:
    assert _extract_steps("") == []


# ══════════════════════════════════════════════════════════════
# _slugify / _titleize
# ══════════════════════════════════════════════════════════════


def test_slugify_strips_accents_and_lowercases() -> None:
    assert _slugify("Pépé Soupe À l'œuf") == "pepe-soupe-a-l-uf"


def test_slugify_kebab_case_with_special_chars() -> None:
    assert _slugify("NDOLÉ AUX CREVETTES & POISSON") == "ndole-aux-crevettes-poisson"


def test_slugify_caps_at_80_chars() -> None:
    long_name = "A" * 200
    slug = _slugify(long_name)
    assert len(slug) <= 80


def test_slugify_empty_returns_fallback() -> None:
    assert _slugify("") == "recette-anonyme"
    assert _slugify("!!!") == "recette-anonyme"


def test_titleize_uppercase_to_titlecase() -> None:
    assert _titleize("PEPPER SOUP") == "Pepper Soup"


def test_titleize_keeps_mixed_case() -> None:
    assert _titleize("Pepper Soup") == "Pepper Soup"


# ══════════════════════════════════════════════════════════════
# RecipeCanonical Pydantic
# ══════════════════════════════════════════════════════════════


def _valid_source() -> SourceMetadata:
    return SourceMetadata(owner="Loth Ivan / Nexyalabs", book="Vol 1", section_index=0)


def test_recipe_canonical_valid_full() -> None:
    recipe = RecipeCanonical(
        id_slug="ndole",
        name="Ndolé",
        description="Plat traditionnel",
        region="Cameroun",
        category="plat_principal",
        ingredients=["légumes", "viande"],
        steps=["faire cuire"],
        source=_valid_source(),
    )
    assert recipe.id_slug == "ndole"
    assert recipe.category == "plat_principal"


def test_recipe_canonical_truncates_long_description() -> None:
    """Description > 2000 chars → tronquée avec `...` (pas une ValidationError)."""
    long_desc = "x" * 3000
    recipe = RecipeCanonical(
        id_slug="test",
        name="Test recette",
        description=long_desc,
        ingredients=["a", "b"],
        steps=["s"],
        source=_valid_source(),
    )
    assert len(recipe.description) == 2000
    assert recipe.description.endswith("...")


def test_recipe_canonical_rejects_one_ingredient() -> None:
    with pytest.raises(Exception):  # ValidationError
        RecipeCanonical(
            id_slug="test",
            name="Test",
            ingredients=["solo"],  # < 2 → rejet
            steps=["s"],
            source=_valid_source(),
        )


def test_recipe_canonical_rejects_invalid_slug() -> None:
    """id_slug doit matcher `^[a-z0-9-]+$`."""
    with pytest.raises(Exception):
        RecipeCanonical(
            id_slug="Slug Avec Espaces",
            name="Test",
            ingredients=["a", "b"],
            steps=["s"],
            source=_valid_source(),
        )


def test_recipe_canonical_invalid_category_falls_back() -> None:
    """Catégorie hors liste → fallback `plat_principal`."""
    recipe = RecipeCanonical(
        id_slug="test",
        name="Test",
        category="categorie-inconnue",
        ingredients=["a", "b"],
        steps=["s"],
        source=_valid_source(),
    )
    assert recipe.category == "plat_principal"


def test_recipe_canonical_truncates_long_ingredient() -> None:
    """Item ingrédient > 500 chars → tronqué avec `...`."""
    long_ing = "x" * 700
    recipe = RecipeCanonical(
        id_slug="test",
        name="Test",
        ingredients=[long_ing, "ok"],
        steps=["s"],
        source=_valid_source(),
    )
    assert len(recipe.ingredients[0]) == 500
    assert recipe.ingredients[0].endswith("...")


# ══════════════════════════════════════════════════════════════
# _build_rag_content + SHA-256 déterministe
# ══════════════════════════════════════════════════════════════


def test_build_rag_content_full_block() -> None:
    recipe = RecipeCanonical(
        id_slug="ndole",
        name="Ndolé",
        description="Plat national.",
        region="Cameroun",
        category="plat_principal",
        ingredients=["légumes", "viande"],
        steps=["cuire", "servir"],
        source=_valid_source(),
    )
    content = _build_rag_content(recipe)
    assert "[Recette] Ndolé" in content
    assert "[Région] Cameroun" in content
    assert "[Catégorie] Plat Principal" in content
    assert "- légumes" in content
    assert "- viande" in content
    assert "1. cuire" in content
    assert "2. servir" in content


def test_build_rag_content_handles_no_region() -> None:
    """Pas de région → fallback `Cameroun`."""
    recipe = RecipeCanonical(
        id_slug="test",
        name="Test",
        ingredients=["a", "b"],
        steps=["s"],
        source=_valid_source(),
    )
    content = _build_rag_content(recipe)
    assert "[Région] Cameroun" in content


def test_rag_content_sha256_is_deterministic() -> None:
    """Même recette → même content → même SHA → INSERT idempotent."""
    recipe1 = RecipeCanonical(
        id_slug="ndo",
        name="Ndo",
        ingredients=["aaa", "bbb"],
        steps=["faire cuire"],
        source=SourceMetadata(owner="Owner X", book="Book Y", section_index=0),
    )
    recipe2 = RecipeCanonical(
        id_slug="ndo",
        name="Ndo",
        ingredients=["aaa", "bbb"],
        steps=["faire cuire"],
        source=SourceMetadata(owner="Owner X", book="Book Y", section_index=999),
    )
    sha1 = hashlib.sha256(_build_rag_content(recipe1).encode()).hexdigest()
    sha2 = hashlib.sha256(_build_rag_content(recipe2).encode()).hexdigest()
    # source.section_index n'est pas dans content → SHA identique.
    assert sha1 == sha2


def test_rag_content_sha256_changes_on_content_change() -> None:
    sha1 = hashlib.sha256(
        _build_rag_content(
            RecipeCanonical(
                id_slug="ndo",
                name="Ndo",
                ingredients=["aaa", "bbb"],
                steps=["faire cuire"],
                source=_valid_source(),
            )
        ).encode()
    ).hexdigest()
    sha2 = hashlib.sha256(
        _build_rag_content(
            RecipeCanonical(
                id_slug="ndo",
                name="Ndo",
                ingredients=["aaa", "ccc"],  # changement
                steps=["faire cuire"],
                source=_valid_source(),
            )
        ).encode()
    ).hexdigest()
    assert sha1 != sha2


# ══════════════════════════════════════════════════════════════
# _embed_with_retry
# ══════════════════════════════════════════════════════════════


class _FakeProvider:
    default_model = "mock-768"

    def __init__(self, *, fails: int = 0) -> None:
        self._fails_remaining = fails
        self.calls = 0

    async def embed(self, texts, *, task_type=None):
        self.calls += 1
        if self._fails_remaining > 0:
            self._fails_remaining -= 1
            raise EmbeddingsRateLimitError("rate limit", provider="mock", retry_after=0.01)

        class _V:
            def __init__(self, t: str) -> None:
                self.values = [float(len(t))] * 768

        class _Resp:
            def __init__(self, ts: list[str]) -> None:
                self.vectors = [_V(t) for t in ts]

        return _Resp(texts)


@pytest.mark.asyncio
async def test_embed_with_retry_success_first_call() -> None:
    provider = _FakeProvider(fails=0)
    vecs = await _embed_with_retry(provider, ["hello", "world"], task_type="RETRIEVAL_DOCUMENT")
    assert len(vecs) == 2
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_embed_with_retry_recovers_after_rate_limit() -> None:
    provider = _FakeProvider(fails=2)
    vecs = await _embed_with_retry(provider, ["hello"], task_type="RETRIEVAL_DOCUMENT")
    assert len(vecs) == 1
    assert provider.calls == 3  # 2 échecs + 1 succès


# ══════════════════════════════════════════════════════════════
# parse_master_file orchestrateur (E2E sur master synthétique)
# ══════════════════════════════════════════════════════════════


def test_parse_master_file_full_pipeline_end_to_end(tmp_path: Path) -> None:
    """E2E : parse un master synthétique avec 3 recettes + 1 sommaire + 1 décoratif."""
    md = """# RECETTES DETAILLEES Test

## SOMMAIRE
Recette une 12
Recette deux 24
Recette trois 36
Recette quatre 48
Recette cinq 60
Recette six 72
Recette sept 84
Recette huit 96
Recette neuf 108
Recette dix 120
Recette onze 132

## NDOLE

Une recette traditionnelle du Cameroun, plat national Bassa.

INGREDIENTS

- 1kg de légumes ndolé
- 500g de viande de bœuf
- 2 oignons
- huile d'arachide

PREPARATION

Faire bouillir les légumes pendant 30 minutes.

Mélanger avec la viande et faire cuire encore 20 min.

## PEPPER SOUP

Cuisine du Cameroun, recette épicée.

INGREDIENTS

- 1kg de poisson
- 2 gousses d'ail
- 1 morceau de gingembre
- piment

PREPARATION

Écaillez le poisson et faites cuire 15 min.

Servez chaud accompagné de riz.

## INGREDIENTS

- ajout orphelin (doit fusionner avec la recette précédente)

## ENTREES

(décoratif sans recette → fusionné silencieusement avec la précédente)
"""
    master_path = tmp_path / "master.md"
    master_path.write_text(md, encoding="utf-8")

    result = parse_master_file(master_path, source_book="TestBook")

    # Au moins 2 recettes acceptées (NDOLE + PEPPER SOUP).
    assert len(result.accepted) >= 2
    names = {r.name for r in result.accepted}
    assert any("Ndole" in n or "NDOLE" in n.upper() for n in names)
    assert any("Pepper" in n or "PEPPER" in n.upper() for n in names)

    # Au moins 1 rejet (le SOMMAIRE).
    assert any(r.reason == "sommaire" for r in result.rejected)


def test_parse_master_file_missing_path_returns_empty(tmp_path: Path) -> None:
    result = parse_master_file(tmp_path / "inexistant.md", source_book="X")
    assert result.accepted == []
    assert result.rejected == []


# ══════════════════════════════════════════════════════════════
# CLI argparse
# ══════════════════════════════════════════════════════════════


def test_cli_defaults() -> None:
    args = _parse_args([])
    assert args.dry_run is False
    assert args.ingest is False
    assert args.validate_only is False
    assert args.force_reembed is False
    assert args.batch_size == 100
    assert args.source_dir.endswith("extracted cuisines")
    assert args.canonical_dir.endswith("_canonical")
    assert args.rejected_dir.endswith("_rejected")
    assert args.report.endswith("_validation_report.md")


def test_cli_dry_run_flag() -> None:
    args = _parse_args(["--dry-run"])
    assert args.dry_run is True


def test_cli_ingest_flag() -> None:
    args = _parse_args(["--ingest"])
    assert args.ingest is True


def test_cli_validate_only_flag() -> None:
    args = _parse_args(["--validate-only"])
    assert args.validate_only is True


def test_cli_force_reembed_flag() -> None:
    args = _parse_args(["--ingest", "--force-reembed"])
    assert args.force_reembed is True


def test_cli_custom_paths() -> None:
    args = _parse_args(
        [
            "--source-dir", "/tmp/src",
            "--canonical-dir", "/tmp/canon",
            "--rejected-dir", "/tmp/rejet",
            "--report", "/tmp/report.md",
            "--batch-size", "50",
        ]
    )
    assert args.source_dir == "/tmp/src"
    assert args.canonical_dir == "/tmp/canon"
    assert args.rejected_dir == "/tmp/rejet"
    assert args.report == "/tmp/report.md"
    assert args.batch_size == 50
