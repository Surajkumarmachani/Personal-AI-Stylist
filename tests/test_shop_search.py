"""Store searches for a gap: deterministic, specific, never a raw slot id."""

from __future__ import annotations

from urllib.parse import unquote

import pytest

from stylist_domain.taxonomy import load_taxonomy
from stylist_shop.search import search_phrase, store_links


def test_the_search_names_the_department_and_the_piece() -> None:
    assert search_phrase("lower", "smart_casual", department="men", warmth_target=3) == (
        "men chinos"
    )
    assert search_phrase("full_body", "formal_ethnic", department="women", warmth_target=3) == (
        "women lehenga choli"
    )


def test_cold_weather_asks_for_the_warm_version() -> None:
    """The bug this came from: 'it's freezing, I'm going to the office'."""
    assert search_phrase("upper_base", "smart_casual", department="men", warmth_target=4) == (
        "men sweater"
    )
    assert search_phrase("upper_layer", "business", department="women", warmth_target=5) == (
        "women wool blazer"
    )


def test_no_department_means_no_gendered_word() -> None:
    assert search_phrase("feet", "business", department=None, warmth_target=3) == "oxford shoes"


@pytest.mark.parametrize("department", ["men", "women", None])
def test_every_slot_and_dress_code_yields_words_not_an_id(department: str | None) -> None:
    """Across the whole taxonomy, a phrase never contains an underscore: a
    raw slot or dress-code id in a store's search box reads as a bug."""
    taxonomy = load_taxonomy()
    slots = list(taxonomy.raw["subcategories"])
    codes = [d["id"] for d in taxonomy.raw["dress_codes"]] + [None]
    for slot in slots:
        for code in codes:
            for warmth in (2, 4):
                phrase = search_phrase(slot, code, department=department, warmth_target=warmth)
                assert phrase and "_" not in phrase, (slot, code, department, phrase)


def test_links_carry_the_phrase_to_three_stores() -> None:
    links = store_links("men wool blazer")
    assert [link.store for link in links] == ["Myntra", "AJIO", "Amazon"]
    assert links[0].url == "https://www.myntra.com/men-wool-blazer"
    for link in links[1:]:
        assert "men+wool+blazer" in link.url or "men wool blazer" in unquote(link.url)
