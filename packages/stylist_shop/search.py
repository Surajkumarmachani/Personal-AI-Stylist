"""Store searches for a gap the catalogue cannot fill.

WHY THIS EXISTS
---------------
`/shop/gaps` used to drop any gap the catalogue had no product for, on the
grounds that "no footwear — 0 suggestions" is not actionable. True, but the
result was worse: the chat said "I couldn't put together an outfit" and then
offered nothing at all. A seeded catalogue of a few dozen items cannot cover
every slot x dress code x weather x department, so on most real questions the
panel was simply absent.

A search on a real store is always actionable. The gap already knows exactly
what is missing (slot, dress code, warmth, whose clothes), so the query is
specific: "men chinos", "women wool blazer", "men mojari jutti", not a
generic ad.

DETERMINISTIC ON PURPOSE. The phrase comes from this table, not from the LLM:
it is shown next to money, so it must be the same every time and testable.
The chat's free-text shortfall advice stays separate and unchanged.

These are plain searches, not affiliate links, so nothing is tracked and no
impression is recorded for them.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import quote, quote_plus

# warmth_target at or above this means "it is cold": the search should say so.
# 4 is `warm` in the taxonomy (blazer, sweater), 5 is `heavy` (overcoat).
COLD_WARMTH = 4

# (slot, dress_code) -> phrase. `None` dress code is the fallback for a slot.
# Ethnic entries differ by department, so they are keyed per department.
_PHRASES: dict[tuple[str, str | None], str] = {
    ("upper_base", "casual"): "t-shirt",
    ("upper_base", "smart_casual"): "oxford shirt",
    ("upper_base", "business"): "formal shirt",
    ("upper_base", "activewear"): "sports t-shirt",
    ("upper_base", "loungewear"): "lounge t-shirt",
    ("upper_base", None): "shirt",
    ("upper_layer", "business"): "blazer",
    ("upper_layer", "smart_casual"): "blazer",
    ("upper_layer", "casual"): "jacket",
    ("upper_layer", "black_tie"): "tuxedo jacket",
    ("upper_layer", None): "jacket",
    ("lower", "casual"): "jeans",
    ("lower", "smart_casual"): "chinos",
    ("lower", "business"): "formal trousers",
    ("lower", "activewear"): "track pants",
    ("lower", "loungewear"): "lounge pants",
    ("lower", "black_tie"): "tuxedo trousers",
    ("lower", None): "trousers",
    ("feet", "casual"): "sneakers",
    ("feet", "smart_casual"): "loafers",
    ("feet", "business"): "oxford shoes",
    ("feet", "black_tie"): "patent oxford shoes",
    ("feet", "activewear"): "running shoes",
    ("feet", "festive_ethnic"): "mojari jutti",
    ("feet", "formal_ethnic"): "mojari jutti",
    ("feet", None): "shoes",
    # full_body is the alternative ROUTE to upper_base + lower, so for most
    # dress codes the useful purchase is a matched set, not a dress.
    ("full_body", "business"): "suit",
    ("full_body", "black_tie"): "tuxedo",
    ("full_body", None): "co-ord set",
    ("upper_layer", "festive_ethnic"): "nehru jacket",
    ("bag", None): "bag",
    ("head", None): "cap",
    ("drape", None): "dupatta",
    ("accessory", None): "belt",
}

# Department-specific phrases, checked before _PHRASES.
_BY_DEPARTMENT: dict[tuple[str, str | None, str], str] = {
    ("upper_base", "festive_ethnic", "men"): "kurta",
    ("upper_base", "festive_ethnic", "women"): "kurti",
    ("upper_base", "formal_ethnic", "men"): "silk kurta",
    ("upper_base", "formal_ethnic", "women"): "embroidered blouse",
    ("upper_base", "smart_casual", "women"): "shirt",
    ("upper_base", "business", "women"): "formal shirt",
    ("upper_layer", "festive_ethnic", "men"): "nehru jacket",
    ("upper_layer", "formal_ethnic", "men"): "nehru jacket",
    ("upper_layer", "festive_ethnic", "women"): "embroidered shrug",
    ("lower", "festive_ethnic", "men"): "churidar",
    ("lower", "festive_ethnic", "women"): "palazzo",
    ("lower", "formal_ethnic", "men"): "churidar",
    ("lower", "formal_ethnic", "women"): "lehenga skirt",
    ("lower", "smart_casual", "women"): "trousers",
    ("full_body", "casual", "women"): "casual dress",
    ("full_body", "smart_casual", "women"): "midi dress",
    ("full_body", "business", "women"): "formal dress",
    ("full_body", "black_tie", "women"): "evening gown",
    ("full_body", "black_tie", "men"): "tuxedo",
    ("full_body", "festive_ethnic", "women"): "anarkali suit",
    ("full_body", "festive_ethnic", "men"): "kurta pyjama set",
    ("full_body", "formal_ethnic", "women"): "lehenga choli",
    ("full_body", "formal_ethnic", "men"): "sherwani",
    ("full_body", "business", "men"): "suit",
    ("full_body", "smart_casual", "men"): "co-ord set",
    ("full_body", "casual", "men"): "co-ord set",
    ("feet", "smart_casual", "women"): "loafers",
    ("feet", "business", "women"): "pumps",
    ("feet", "black_tie", "women"): "heels",
    ("feet", "festive_ethnic", "women"): "embellished jutti",
    ("feet", "formal_ethnic", "women"): "embellished heels",
}

# Cold-weather replacements: the same need, in its warm form.
_COLD: dict[str, str] = {
    "t-shirt": "sweater",
    "oxford shirt": "sweater",
    "shirt": "sweater",
    "formal shirt": "wool sweater",
    "kurta": "wool kurta",
    "jacket": "winter jacket",
    "blazer": "wool blazer",
    "casual dress": "winter dress",
    "midi dress": "sweater dress",
    "sneakers": "boots",
    "loafers": "chelsea boots",
}


@dataclass(frozen=True, slots=True)
class StoreLink:
    store: str
    url: str


def search_phrase(
    slot: str, dress_code: str | None, *, department: str | None, warmth_target: int | None
) -> str:
    """The words to search for, e.g. "men wool blazer"."""
    phrase = None
    if department in {"men", "women"}:
        phrase = _BY_DEPARTMENT.get((slot, dress_code, department))
    # The last resort is still words: a raw slot id ("full_body") in a search
    # box reads as a bug.
    phrase = (
        phrase
        or _PHRASES.get((slot, dress_code))
        or _PHRASES.get((slot, None))
        or slot.replace("_", " ")
    )
    if warmth_target is not None and warmth_target >= COLD_WARMTH:
        phrase = _COLD.get(phrase, phrase)
    return f"{department} {phrase}" if department in {"men", "women"} else phrase


def store_links(phrase: str) -> list[StoreLink]:
    """The same search on three large Indian stores."""
    return [
        StoreLink("Myntra", f"https://www.myntra.com/{quote(phrase.replace(' ', '-'))}"),
        StoreLink("AJIO", f"https://www.ajio.com/search/?text={quote_plus(phrase)}"),
        StoreLink("Amazon", f"https://www.amazon.in/s?k={quote_plus(phrase)}"),
    ]
