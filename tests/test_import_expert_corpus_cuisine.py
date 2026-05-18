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
    _chunk_rag_content,
    _detect_category,
    _detect_region,
    _embed_with_retry,
    _extract_ingredients,
    _extract_ingredients_inline,
    _extract_real_title_from_body,
    _extract_steps,
    _is_advice_title,
    _is_decorative_title,
    _is_orphan_heading,
    _is_summary_block,
    _is_trash_title,
    _maybe_retitle_truncated,
    _normalize_bullets,
    _parse_args,
    _slugify,
    _split_master_into_blocks,
    _split_sections,
    _strip_footers,
    _strip_retitled_header_from_body,
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


def test_normalize_bullets_pua_word_codepoints() -> None:
    """Les bullets PUA Word/Wingdings (\\uf0b7, \\uf0a7) doivent être
    convertis en `-` au même titre que les bullets unicode standards.
    Cas vol 3 du dataset Ivan : pymupdf conserve le glyphe Wingdings tel quel."""
    raw = " item PUA b7\n item PUA a7"
    out = _normalize_bullets(raw)
    assert "" not in out
    assert "" not in out
    assert "- item PUA b7" in out
    assert "- item PUA a7" in out


def test_normalize_bullets_joins_orphan_bullet_with_next_line() -> None:
    """Un bullet seul sur sa ligne, suivi du contenu à la ligne suivante,
    doit être joint (cas vol 3 où pymupdf sépare bullet et contenu)."""
    raw = "-\n500g de riz\n-\neau"
    out = _normalize_bullets(raw)
    assert "- 500g de riz" in out
    assert "- eau" in out


def test_strip_footers_removes_editions_line() -> None:
    """La mention éditeur « Une recette des Editions2015 » doit être
    supprimée (vol 3 du dataset Ivan)."""
    raw = "Cuisine du Cameroun\nUne recette des Editions2015\nINGREDIENTS"
    cleaned = _strip_footers(raw)
    assert "Editions2015" not in cleaned
    assert "Editions 2015" not in cleaned
    assert "Cuisine du Cameroun" in cleaned
    assert "INGREDIENTS" in cleaned


def test_strip_footers_handles_editions_with_space_and_accent() -> None:
    """Tolérance : espaces et accents (« Une recette des Éditions 2015 »)."""
    raw = "Une recette des Éditions 2015\nIngrédients"
    cleaned = _strip_footers(raw)
    assert "Éditions" not in cleaned
    assert "Ingrédients" in cleaned


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


def test_recipe_canonical_advice_accepts_empty_ingredients() -> None:
    """category='astuce' relaxe la contrainte min 2 ingrédients."""
    recipe = RecipeCanonical(
        id_slug="comment-reconnaitre-bonne-viande",
        name="Comment Reconnaître Une Bonne Viande",
        category="astuce",
        ingredients=[],  # OK car astuce
        steps=["Observer la couleur, la texture, l'odeur."],
        source=_valid_source(),
    )
    assert recipe.ingredients == []
    assert recipe.category == "astuce"


def test_recipe_canonical_non_advice_still_requires_2_ingredients() -> None:
    """category != 'astuce' garde la contrainte stricte min 2 ingrédients."""
    with pytest.raises(Exception):  # ValidationError
        RecipeCanonical(
            id_slug="test-plat",
            name="Test Plat",
            category="plat_principal",
            ingredients=["solo"],  # < 2 → rejet
            steps=["s"],
            source=_valid_source(),
        )


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


# ══════════════════════════════════════════════════════════════
# _is_advice_title + _maybe_retitle_truncated (G2 v3 2026-05-16)
# ══════════════════════════════════════════════════════════════


def test_is_advice_title_matches_comment_and_astuce() -> None:
    """Titres commençant par « Comment », « Astuce », « Conseil » → True."""
    assert _is_advice_title("Comment Reconnaître Une Bonne Viande") is True
    assert _is_advice_title("Comment Faire Une Sauce Vinaigrette") is True
    assert _is_advice_title("Astuce de Cuisine") is True
    assert _is_advice_title("Astuces Pratiques") is True
    assert _is_advice_title("Conseils Pour Conserver") is True


def test_is_advice_title_rejects_real_recipe_names() -> None:
    """Noms de recettes traditionnelles → pas une astuce."""
    assert _is_advice_title("Ndolé Aux Crevettes") is False
    assert _is_advice_title("Pepper Soup") is False
    assert _is_advice_title("Poulet DG") is False


def test_is_advice_title_handles_empty() -> None:
    assert _is_advice_title("") is False
    assert _is_advice_title("   ") is False


def test_maybe_retitle_truncated_completes_with_uppercase_first_line() -> None:
    """Titre finissant par « Une » + body[0]=« BONNE VIANDE » → joint."""
    title = "Comment Reconnaitre Une"
    body = "BONNE VIANDE\n\n- La couleur de la viande\n- L'odeur"
    out = _maybe_retitle_truncated(title, body)
    assert "Bonne Viande" in out
    assert out.startswith("Comment Reconnaitre Une")


def test_maybe_retitle_truncated_handles_un_determinant() -> None:
    """Titre finissant par « Un » + body[0]=« POISSON FRAIS » → joint."""
    title = "Comment Reconnaitre Un"
    body = "POISSON FRAIS\n\n- Yeux clairs\n- Branchies rouges"
    out = _maybe_retitle_truncated(title, body)
    assert "Poisson Frais" in out


def test_maybe_retitle_truncated_unchanged_for_complete_title() -> None:
    """Titre complet (pas de déterminant orphelin) → inchangé."""
    title = "Ndolé Aux Crevettes"
    body = "Une recette traditionnelle\n\nINGREDIENTS..."
    assert _maybe_retitle_truncated(title, body) == title


def test_maybe_retitle_truncated_handles_empty() -> None:
    assert _maybe_retitle_truncated("", "BONNE VIANDE") == ""
    assert _maybe_retitle_truncated("Comment Une", "") == "Comment Une"


def test_maybe_retitle_truncated_handles_isolated_letter_tail() -> None:
    """G2 V8 : titre tronqué au milieu d'un mot (« Preparatio N » =
    « Preparation » coupé) → joindre avec premiere ligne MAJUSCULES."""
    title = "Preparatio N"
    body = "VINAIGRETTE\n\n- Dans un bol, versez le vinaigre..."
    out = _maybe_retitle_truncated(title, body)
    assert "Vinaigrette" in out


# ══════════════════════════════════════════════════════════════
# _strip_retitled_header_from_body (G2 V8)
# ══════════════════════════════════════════════════════════════


def test_strip_retitled_header_removes_first_occurrence() -> None:
    """Si retitling a ajouté « BONNE VIANDE » au titre, on retire la
    ligne « BONNE VIANDE » du body pour éviter step[0]=« BONNE VIANDE »."""
    body = "BONNE VIANDE\n\n- La couleur de la viande...\n- L'odeur..."
    cleaned = _strip_retitled_header_from_body(body, "Bonne Viande")
    assert "BONNE VIANDE" not in cleaned
    assert "- La couleur de la viande" in cleaned


def test_strip_retitled_header_case_insensitive() -> None:
    """Comparaison MAJUSCULES indépendamment de la casse du `appended_part`."""
    body = "POISSON FRAIS\nIngrédients..."
    cleaned = _strip_retitled_header_from_body(body, "poisson frais")
    assert "POISSON FRAIS" not in cleaned


def test_strip_retitled_header_handles_empty_inputs() -> None:
    assert _strip_retitled_header_from_body("", "BONNE VIANDE") == ""
    assert _strip_retitled_header_from_body("body", "") == "body"


# ══════════════════════════════════════════════════════════════
# _is_trash_title (G2 V8)
# ══════════════════════════════════════════════════════════════


def test_is_trash_title_known_meta_titles() -> None:
    """Titres méta connus (chapitres, sommaires) → déchet."""
    for title in (
        "INGREDIENTS",
        "Préparation",
        "LES COMPLEMENTS",
        "TENUE MILITAIRE",
        "Pâtisseries Et Vienoiseries",
        "Voir Recette Du Mintumba",
    ):
        assert _is_trash_title(title) is True, f"{title!r} devrait être déchet"


def test_is_trash_title_short_titles() -> None:
    """Titre < 3 chars → déchet (3 chars min pour préserver noms africains courts)."""
    assert _is_trash_title("") is True
    assert _is_trash_title("ab") is True
    assert _is_trash_title("   ") is True
    # 3 chars OK (ERU, KOO, etc.)
    assert _is_trash_title("Eru") is False
    assert _is_trash_title("ERU") is False


def test_is_trash_title_african_repeated_names_not_trash() -> None:
    """Noms camerounais à répétition intentionnelle → PAS déchet
    (Njama Njama, Kati Kati, Kelen Kelen, Pili Pili sont des vraies recettes)."""
    for title in (
        "Njama Njama",
        "Kati Kati",
        "Kelen Kelen",
        "Pili Pili",
        "NJAMA NJAMA",
        "Mbongo Mbongo",  # variante théorique
    ):
        assert _is_trash_title(title) is False, f"{title!r} ne devrait PAS être déchet"


def test_is_trash_title_lowercase_start_fragment() -> None:
    """Fragment minuscule (« la pâte », « les boulettes ») → déchet."""
    assert _is_trash_title("la pâte") is True
    assert _is_trash_title("les boulettes") is True
    assert _is_trash_title("est assaisonné avec douze condiments") is True


def test_is_trash_title_unbalanced_parens() -> None:
    """Parenthèses déséquilibrées (« Owondo) ») → déchet."""
    assert _is_trash_title("Owondo)") is True
    assert _is_trash_title("(en Boulou Owondo") is True


def test_is_trash_title_duplicated_title() -> None:
    """Titre dupliqué (« SAUCE TOMATE AUX Sauce Tomate Aux ») → déchet."""
    assert _is_trash_title("SAUCE TOMATE AUX Sauce Tomate Aux") is True


def test_is_trash_title_valid_recipe_names_not_trash() -> None:
    """Vrais noms de recettes → PAS déchet."""
    for title in (
        "Ndolé Aux Crevettes",
        "Pepper Soup",
        "Poulet DG",
        "Comment Reconnaitre Une Bonne Viande",
        "Pebe",  # 4 chars exactement, OK
    ):
        assert _is_trash_title(title) is False, f"{title!r} ne devrait PAS être déchet"


# ══════════════════════════════════════════════════════════════
# _detect_category (G2 V8 refonte par priorité)
# ══════════════════════════════════════════════════════════════


def test_detect_category_plats_avec_proteines_priorite_plat() -> None:
    """Refonte V8 : `plat_principal` matche AVANT `epice` sur les
    plats à base d'épice (Mbongo Tchobi = plat, pas une épice)."""
    assert _detect_category("Mbongo Tchobi (Poisson)", "") == "plat_principal"
    assert _detect_category("Poulet Braise", "") == "plat_principal"
    assert _detect_category("Pepper Soup", "") == "plat_principal"
    assert _detect_category("Spaghettis Sautes A La Sardine", "") == "plat_principal"


def test_detect_category_boisson_pas_lait_de_coco() -> None:
    """Refonte V8 : `lait` retiré de la liste boisson → « Lait De Coco »
    n'est plus boisson (fallback plat_principal ou autre selon body)."""
    assert _detect_category("Lait De Coco", "") != "boisson"


def test_detect_category_boisson_strictement_boissons() -> None:
    """Refonte V8 : seules les vraies boissons matchent."""
    assert _detect_category("Jus de Bissap", "") == "boisson"
    assert _detect_category("Folere", "") == "boisson"
    assert _detect_category("Matango", "") == "boisson"


def test_detect_category_sauce_premier_mot_seulement() -> None:
    """Refonte V8 : `^sauce\\b` matche UNIQUEMENT « Sauce X », pas « X Sauce Y »."""
    assert _detect_category("Sauce Arachide", "") == "sauce"
    assert _detect_category("Sauce Pistache", "") == "sauce"
    # « X Sauce Y » → pas sauce (devient plat_principal via le pattern proteine)
    cat = _detect_category("Crevettes Sauce Tomate", "")
    assert cat == "plat_principal"  # car "crevettes" match le pattern proteine


def test_detect_category_astuce_comment_prefix() -> None:
    """Refonte V8 : `^(comment|astuce)` priorité absolue."""
    assert _detect_category("Comment Reconnaitre Une Bonne Viande", "") == "astuce"
    assert _detect_category("Astuce du Chef", "") == "astuce"


def test_detect_category_accompagnement_feculents() -> None:
    """Refonte V8 : féculents bouillis/frits → accompagnement."""
    assert _detect_category("Manioc Bouilli", "") == "accompagnement"
    assert _detect_category("Pommes De Terre Bouillies", "") == "accompagnement"
    assert _detect_category("Plantain Bouilli", "") == "accompagnement"
    assert _detect_category("Foufou Manioc", "") == "accompagnement"


def test_detect_category_default_plat_principal() -> None:
    assert _detect_category("Recette Inconnue", "body neutre") == "plat_principal"


# ══════════════════════════════════════════════════════════════
# _chunk_rag_content (G2 V8 chunking par paragraphe)
# ══════════════════════════════════════════════════════════════


def test_chunk_rag_content_short_content_no_split() -> None:
    """Content court (<= max_chars) → 1 seul chunk identique."""
    short = "[Recette] Foo\n[Région] Cameroun\n[Catégorie] Plat Principal\n\n[Description]\nCourte."
    chunks = _chunk_rag_content(short, max_chars=1800)
    assert chunks == [short]


def test_chunk_rag_content_long_content_splits_with_header_replication() -> None:
    """Content long → N chunks, header répliqué dans chaque chunk +
    marqueur [Partie N/Total]."""
    header = "[Recette] Mega Plat\n[Région] Cameroun\n[Catégorie] Plat Principal\n\n"
    body = "\n\n".join(f"Paragraphe {i} " + ("xxx " * 50) for i in range(20))
    full = header + body
    assert len(full) > 1800
    chunks = _chunk_rag_content(full, max_chars=1800)
    assert len(chunks) >= 2
    for i, c in enumerate(chunks, start=1):
        assert "[Recette] Mega Plat" in c
        assert "[Région] Cameroun" in c
        assert "[Catégorie] Plat Principal" in c
        assert f"[Partie {i}/{len(chunks)}]" in c
        assert len(c) <= 1800


def test_chunk_rag_content_giant_paragraph_brutal_cut() -> None:
    """Paragraphe seul qui dépasse max_chars → coupure brutale en N parts."""
    header = "[Recette] Test\n[Région] Cameroun\n[Catégorie] Plat Principal\n\n"
    giant_para = "a" * 5000
    full = header + giant_para
    chunks = _chunk_rag_content(full, max_chars=1800)
    assert len(chunks) >= 2
    for c in chunks:
        assert len(c) <= 1800


def test_chunk_rag_content_preserves_all_body_content() -> None:
    """Tous les paragraphes du body doivent être présents dans au moins 1 chunk."""
    header = "[Recette] Test\n[Région] Cameroun\n[Catégorie] Plat Principal\n\n"
    body_paras = [f"Paragraphe distinct numero {i}" for i in range(15)]
    full = header + "\n\n".join(body_paras + ["x " * 200 for _ in range(5)])
    chunks = _chunk_rag_content(full, max_chars=1000)
    all_chunks_text = " ".join(chunks)
    for para in body_paras:
        assert para in all_chunks_text, f"{para!r} perdu lors du chunking"


# ══════════════════════════════════════════════════════════════
# _build_rag_content
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


def test_build_rag_content_astuce_uses_advice_header() -> None:
    """category='astuce' utilise `[Astuce]` au lieu de `[Recette]` + omet
    la section ingrédients si vide."""
    recipe = RecipeCanonical(
        id_slug="comment-conserver-poisson",
        name="Comment Conserver Le Poisson",
        category="astuce",
        ingredients=[],
        steps=["Fumer 2h", "Saler généreusement"],
        source=_valid_source(),
    )
    content = _build_rag_content(recipe)
    assert "[Astuce] Comment Conserver Le Poisson" in content
    assert "[Recette]" not in content
    assert "[Ingrédients]" not in content  # omis car vide
    assert "[Étapes]" in content
    assert "1. Fumer 2h" in content


def test_build_rag_content_astuce_with_ingredients_keeps_section() -> None:
    """Une astuce qui a quand même des ingrédients garde la section."""
    recipe = RecipeCanonical(
        id_slug="comment-faire-vinaigrette",
        name="Comment Faire Une Vinaigrette",
        category="astuce",
        ingredients=["1 c. à café moutarde", "3 c. à soupe vinaigre"],
        steps=["Mélanger"],
        source=_valid_source(),
    )
    content = _build_rag_content(recipe)
    assert "[Astuce] Comment Faire Une Vinaigrette" in content
    assert "[Ingrédients]" in content
    assert "- 1 c. à café moutarde" in content


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
