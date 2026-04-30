"""
Tests unitaires — `app.features.files.chunker.chunk_text` (D4 RAG).

Vérifie les invariants critiques :
- Offsets cohérents et strictement monotones.
- Tokens comptés par tiktoken.
- `page_number` extrait des marqueurs `[[PAGE:N]]` avec résolution
  milieu-du-chunk, `None` sans marqueurs.
- Overlap effectif entre chunks consécutifs.
- Soft-breaks privilégient `\\n\\n` > `\\n` > `. ` > espace.
"""

from __future__ import annotations

from app.features.files.chunker import Chunk, chunk_text

# ══════════════════════════════════════════════════════════════
# 1. Cas triviaux
# ══════════════════════════════════════════════════════════════


def test_chunker_empty_returns_empty_list() -> None:
    assert chunk_text("") == []


def test_chunker_short_text_returns_single_chunk() -> None:
    text = "Un paragraphe court de quelques mots."
    chunks = chunk_text(text, target_tokens=500, overlap_tokens=50)
    assert len(chunks) == 1
    assert chunks[0].content == text
    assert chunks[0].index == 0
    assert chunks[0].start_char_offset == 0
    assert chunks[0].end_char_offset == len(text)
    assert chunks[0].token_count >= 1


# ══════════════════════════════════════════════════════════════
# 2. Respect de la taille cible
# ══════════════════════════════════════════════════════════════


def test_chunker_respects_target_tokens_approximately() -> None:
    # Texte de ~2000 chars. target_tokens=100 → target_chars=400.
    # On attend au moins 4 chunks.
    paragraph = "Phrase simple. " * 150  # ~2250 chars
    chunks = chunk_text(paragraph, target_tokens=100, overlap_tokens=10)
    assert len(chunks) >= 4
    # Chaque chunk ne devrait pas dépasser largement la cible.
    for c in chunks:
        # Marge 2x car les soft-breaks peuvent étendre légèrement la fenêtre.
        assert c.token_count <= 300


# ══════════════════════════════════════════════════════════════
# 3. Overlap effectif
# ══════════════════════════════════════════════════════════════


def test_chunker_overlap_is_applied_between_chunks() -> None:
    # Texte bien long pour garantir ≥ 2 chunks avec overlap.
    text = " ".join([f"mot{i}" for i in range(500)])
    chunks = chunk_text(text, target_tokens=50, overlap_tokens=20)
    assert len(chunks) >= 2
    # Le 2ᵉ chunk commence AVANT la fin du 1er.
    assert chunks[1].start_char_offset < chunks[0].end_char_offset


# ══════════════════════════════════════════════════════════════
# 4. Offsets monotonement croissants + valides
# ══════════════════════════════════════════════════════════════


def test_chunker_offsets_monotonically_increasing() -> None:
    text = "Phrase. " * 200
    chunks = chunk_text(text, target_tokens=80, overlap_tokens=10)
    for c in chunks:
        assert c.start_char_offset < c.end_char_offset
        assert c.start_char_offset >= 0
        assert c.end_char_offset <= len(text)
    # Les starts se suivent dans l'ordre.
    starts = [c.start_char_offset for c in chunks]
    assert starts == sorted(starts)


def test_chunker_offsets_match_source_text() -> None:
    """Les offsets doivent refléter exactement la position dans le source.

    Contrat : `text[start:end]` doit contenir le contenu du chunk (au
    `strip()` près — le chunker strip les bords).
    """
    text = "Paragraphe un.\n\nParagraphe deux.\n\nParagraphe trois."
    chunks = chunk_text(text, target_tokens=5, overlap_tokens=0)
    for c in chunks:
        source_slice = text[c.start_char_offset : c.end_char_offset]
        # Le chunk content est le slice strip()é — vérifier
        # que le strip enlève juste du whitespace.
        assert c.content in source_slice or c.content == source_slice.strip()


# ══════════════════════════════════════════════════════════════
# 5. Soft-break privilégie paragraphe > ligne > phrase > espace
# ══════════════════════════════════════════════════════════════


def test_chunker_soft_break_prefers_paragraph_boundary() -> None:
    # target_chars ≈ 40 (10 tokens × 4). Texte construit pour qu'un
    # `\\n\\n` tombe dans la 2ᵉ moitié de la fenêtre de coupe.
    text = "A" * 25 + "\n\n" + "B" * 25 + "\n\n" + "C" * 25
    chunks = chunk_text(text, target_tokens=10, overlap_tokens=0)
    # Le 1er chunk doit se terminer après le 1er `\\n\\n` (avec bord
    # au split_priorities).
    assert len(chunks) >= 2
    assert chunks[0].content.startswith("A")
    # Le 2ᵉ chunk ne commence PAS au milieu du bloc de A.
    assert not chunks[1].content.startswith("AA")


# ══════════════════════════════════════════════════════════════
# 6. Page numbers
# ══════════════════════════════════════════════════════════════


def test_chunker_page_number_extracted_from_markers() -> None:
    text = (
        "[[PAGE:1]]\n"
        + ("Contenu page 1. " * 30)  # ~480 chars
        + "\n[[PAGE:2]]\n"
        + ("Contenu page 2. " * 30)
    )
    chunks = chunk_text(text, target_tokens=50, overlap_tokens=5)
    # Au moins un chunk sur page 1, au moins un sur page 2.
    pages = {c.page_number for c in chunks}
    assert 1 in pages
    assert 2 in pages


def test_chunker_page_number_none_when_no_markers() -> None:
    text = "Contenu sans aucun marqueur de page. " * 50
    chunks = chunk_text(text, target_tokens=50, overlap_tokens=5)
    assert chunks
    for c in chunks:
        assert c.page_number is None


def test_chunker_page_marker_content_retired_from_chunk() -> None:
    """Le contenu d'un chunk ne doit pas contenir `[[PAGE:N]]`."""
    text = "[[PAGE:3]]\nTexte de la page trois uniquement, rien de plus."
    chunks = chunk_text(text, target_tokens=50, overlap_tokens=0)
    assert chunks
    for c in chunks:
        assert "[[PAGE:" not in c.content


# ══════════════════════════════════════════════════════════════
# 7. Garde-fou anti-boucle infinie
# ══════════════════════════════════════════════════════════════


def test_chunker_progresses_even_on_overlap_ge_target() -> None:
    """Si overlap_tokens >= target_tokens, le chunker doit quand même
    progresser (garde-fou `cursor + 1`)."""
    text = "x" * 1000
    chunks = chunk_text(text, target_tokens=10, overlap_tokens=20)
    # Il doit retourner sans boucler.
    assert isinstance(chunks, list)
    assert len(chunks) >= 1


def test_chunker_returns_chunk_instances() -> None:
    chunks = chunk_text("Hello world.", target_tokens=500, overlap_tokens=50)
    assert chunks
    for c in chunks:
        assert isinstance(c, Chunk)
        assert c.token_count >= 1
