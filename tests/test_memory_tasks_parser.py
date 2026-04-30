"""
Tests unitaires — `_parse_facts_json` (Session D2).

Parser JSON tolérant en 3 passes — garantit qu'un LLM qui wrappe sa
sortie en markdown, ajoute un commentaire, ou produit du JSON cassé ne
fait jamais crasher le worker (fallback `[]` silencieux).
"""

from __future__ import annotations

from workers.memory_tasks import (
    EXTRACTION_FACT_MAX_CHARS,
    EXTRACTION_MAX_FACTS,
    _parse_facts_json,
)

# ══════════════════════════════════════════════════════════════
# 1. Passe 1 — JSON direct valide
# ══════════════════════════════════════════════════════════════


def test_parse_direct_valid_json() -> None:
    raw = '{"facts": ["L\'utilisateur est dev Flutter", "L\'utilisateur habite au Cameroun"]}'
    facts = _parse_facts_json(raw)
    assert facts == [
        "L'utilisateur est dev Flutter",
        "L'utilisateur habite au Cameroun",
    ]


def test_parse_empty_facts_list() -> None:
    """Le LLM peut légitimement retourner une liste vide si rien de durable."""
    raw = '{"facts": []}'
    assert _parse_facts_json(raw) == []


# ══════════════════════════════════════════════════════════════
# 2. Passe 2 — JSON wrappé en markdown
# ══════════════════════════════════════════════════════════════


def test_parse_markdown_wrapped_json() -> None:
    """LLM qui ignore l'instruction et entoure en ```json ... ```."""
    raw = """```json
{"facts": ["L'utilisateur travaille sur NEXYA"]}
```"""
    facts = _parse_facts_json(raw)
    assert facts == ["L'utilisateur travaille sur NEXYA"]


def test_parse_prefixed_json() -> None:
    """LLM qui ajoute un préfixe malgré l'instruction."""
    raw = 'Voici les faits extraits : {"facts": ["L\'utilisateur est Ivan"]}'
    facts = _parse_facts_json(raw)
    assert facts == ["L'utilisateur est Ivan"]


# ══════════════════════════════════════════════════════════════
# 3. Passe 3 — fallback sur JSON cassé ou malformé
# ══════════════════════════════════════════════════════════════


def test_parse_broken_json_returns_empty() -> None:
    raw = "{facts: [not valid json"
    assert _parse_facts_json(raw) == []


def test_parse_empty_raw_returns_empty() -> None:
    assert _parse_facts_json("") == []


def test_parse_facts_not_a_list_returns_empty() -> None:
    """`"facts": "une string"` au lieu d'une liste → rejeté."""
    raw = '{"facts": "should be a list"}'
    assert _parse_facts_json(raw) == []


def test_parse_non_str_items_filtered() -> None:
    """Des items non-string dans la liste → filtrés sans crash."""
    raw = '{"facts": ["L\'utilisateur est dev", 42, null, "L\'utilisateur aime le code"]}'
    facts = _parse_facts_json(raw)
    assert facts == [
        "L'utilisateur est dev",
        "L'utilisateur aime le code",
    ]


# ══════════════════════════════════════════════════════════════
# 4. Post-parse filtering — dédup, truncate, whitespace
# ══════════════════════════════════════════════════════════════


def test_parse_dedup_case_insensitive() -> None:
    """Dédup interne : LLM qui répète le même fait → 1 seul dans la sortie."""
    raw = (
        '{"facts": ['
        '"L\'utilisateur est dev Flutter",'
        '"L\'UTILISATEUR EST DEV FLUTTER",'
        '"L\'utilisateur habite au Cameroun"'
        "]}"
    )
    facts = _parse_facts_json(raw)
    assert len(facts) == 2
    assert "L'utilisateur est dev Flutter" in facts
    assert "L'utilisateur habite au Cameroun" in facts


def test_parse_truncates_long_facts() -> None:
    long_fact = "L'utilisateur " + ("x" * 300)
    raw = '{"facts": ["' + long_fact + '"]}'
    facts = _parse_facts_json(raw)
    assert len(facts) == 1
    assert len(facts[0]) <= EXTRACTION_FACT_MAX_CHARS


def test_parse_strips_whitespace_only() -> None:
    raw = '{"facts": ["   ", "L\'utilisateur existe", "\\t\\n"]}'
    facts = _parse_facts_json(raw)
    assert facts == ["L'utilisateur existe"]


def test_parse_caps_at_max_facts() -> None:
    """Si LLM renvoie 10 faits, on tronque à EXTRACTION_MAX_FACTS."""
    items = [f"L'utilisateur fait chose numéro {i}" for i in range(10)]
    raw = '{"facts": [' + ",".join(f'"{s}"' for s in items) + "]}"
    facts = _parse_facts_json(raw)
    assert len(facts) == EXTRACTION_MAX_FACTS
