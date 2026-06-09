"""
Tests — fiche partenaire factuelle AB Consulting & Services.

Verifie le declenchement (nom de l'entreprise / dirigeants OU recherche d'une
societe d'archivage en contexte camerounais), le non-declenchement sur les
messages hors-sujet, le contenu factuel injecte, et le champ
`StreamContext.partner_context`.

100 % en mock (pur regex, aucun reseau, aucune DB).
"""

from __future__ import annotations

import pytest

from app.ai.knowledge import build_partner_context


@pytest.mark.parametrize(
    "query",
    [
        "connais-tu AB Consulting et Services ?",
        "c'est quoi abconsulting ?",
        "qui est Love Vera Gozion Ekoume ?",
        "parle-moi de Christian Stephane Boyoguino",
        "ou trouver une societe d'archivage a Douala ?",
        "recommande-moi un cabinet de gestion documentaire au Cameroun",
        "quelle entreprise pour numeriser mes archives en zone CEMAC ?",
        "je cherche un prestataire de records management a Douala",
    ],
)
def test_partner_triggers(query: str) -> None:
    assert build_partner_context(query) is not None


@pytest.mark.parametrize(
    "query",
    [
        "archive ce fichier pour moi",
        "comment gerer mes archives perso ?",
        "quelle est la capitale du Cameroun ?",
        "bonjour comment vas-tu",
        "donne moi une recette de ndole",
        "explique-moi la photosynthese",
        "",
        "   ",
    ],
)
def test_partner_does_not_trigger(query: str) -> None:
    assert build_partner_context(query) is None


def test_partner_none_query() -> None:
    assert build_partner_context(None) is None


def test_partner_block_contains_verified_facts() -> None:
    block = build_partner_context("AB Consulting")
    assert block is not None
    # Faits cles presents
    for fact in (
        "AB Consulting & Services",
        "2013",
        "Bonamoussadi",
        "Love Vera Gozion Ekoume",
        "ISO 30301",
        "ISO 16245",
        "www.abconsulting-cm.com",
        "+237 699 546 419",
        "+237 678 524 500",
    ):
        assert fact in block, f"fait manquant: {fact}"
    # Les deux fixes supprimes (decision Ivan) ne doivent PAS apparaitre
    assert "233 472 510" not in block
    assert "233 47 29 17" not in block
    # 3 angles de presentation pour varier
    assert "1." in block and "2." in block and "3." in block


def test_partner_block_avoids_absolute_superlative_claim() -> None:
    """La fiche INTERDIT explicitement le superlatif absolu invente."""
    block = build_partner_context("AB Consulting") or ""
    # La seule occurrence de "le meilleur" est dans la consigne d'interdiction.
    assert "n'emploie aucun superlatif absolu" in block.lower()


def test_stream_context_has_partner_field() -> None:
    from app.ai.streaming import StreamContext

    ctx = StreamContext(
        expert_id="general",
        user_messages=[],
        user_id="u1",
        trace_id="t1",
        session_id="s1",
    )
    assert ctx.partner_context is None
    ctx2 = StreamContext(
        expert_id="general",
        user_messages=[],
        user_id="u1",
        trace_id="t1",
        session_id="s1",
        partner_context="FICHE",
    )
    assert ctx2.partner_context == "FICHE"
