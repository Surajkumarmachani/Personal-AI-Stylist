"""The chat: free text -> an occasion -> real outfits.

WHY THE INTENT TESTS ARE THE BULK OF THIS FILE
-----------------------------------------------
The router is deliberately thin — it resolves an intent and calls
`/suggestions`' own handler, so everything about wardrobe loading, scoring,
reranking and the bandit is already covered by those tests. What is NEW here is
the mapping from words to a closed vocabulary, and that is where the bugs are.
"""

from __future__ import annotations

import pytest

from stylist_domain.intent import LEXICON, parse
from stylist_domain.taxonomy import load_taxonomy


def test_every_lexicon_target_is_a_real_occasion() -> None:
    """A phrase mapping to an occasion the taxonomy does not define would raise
    inside `resolve_context` at request time — a 500 for a query the lexicon
    was supposed to have handled.

    Asserted against the taxonomy rather than a hand-written list, so renaming
    an occasion fails here instead of in production.
    """
    occasions = set(load_taxonomy().occasions)
    unknown = {phrase: occ for phrase, occ in LEXICON.items() if occ not in occasions}
    assert not unknown, f"lexicon points at occasions that do not exist: {unknown}"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("What should I wear for Diwali?", "festival_day"),
        ("diwali party tonight", "festival_day"),
        ("deepavali", "festival_day"),
        ("onam sadya", "festival_day"),
        ("eid tomorrow", "festival_day"),
        ("I have a client meeting", "client_meeting"),
        ("interview on monday", "interview"),
        ("wedding reception next week", "wedding_reception"),
        ("my cousin's mehendi", "mehendi"),
        ("sangeet night", "sangeet"),
        ("going to the temple", "temple_visit"),
        ("gym", "workout"),
        ("dinner date", "dinner_date"),
        ("black tie", "black_tie_event"),
    ],
)
def test_real_queries_resolve(query: str, expected: str) -> None:
    """The phrasings people actually type, including Indian festivals by NAME.

    Users do not type "festival day" — they type "Diwali". A substring match on
    "festival" catches none of these, which is why every festival is its own
    lexicon entry.
    """
    assert parse(query).occasion == expected


def test_the_longest_phrase_wins() -> None:
    """ "wedding reception" and "wedding" both match a query about a reception,
    and the clothes are genuinely different — which is why the taxonomy
    separates the ceremony from the reception in the first place."""
    assert parse("wedding reception").occasion == "wedding_reception"
    assert parse("wedding ceremony tomorrow").occasion == "wedding_ceremony"
    assert parse("client meeting").occasion == "client_meeting"


def test_word_boundaries_are_respected() -> None:
    """THE BUG THIS PREVENTS. "date" inside "candidate" and "eid" inside
    "identity" would both match on a naive substring check, and a user asking
    about an identity card would be dressed for dinner."""
    assert parse("reviewing a candidate profile").occasion != "dinner_date"
    assert parse("my identity document").occasion != "festival_day"


def test_accents_do_not_defeat_the_lexicon() -> None:
    """Users type "Dīwālī" as readily as "Diwali", and no stemmer gets there.
    `unicodedata` handles it in one line where a spelling variant per accent
    would not scale."""
    assert parse("what to wear for Dīwālī").occasion == "festival_day"


def test_stated_weather_is_used_and_unstated_weather_is_not_claimed() -> None:
    """The chat has no location, so a condition the USER states is better
    evidence than the placeholder — and on a query that says nothing about
    weather, claiming one is a claim we cannot support."""
    cold = parse("its freezing, going to the office")
    assert cold.occasion == "office_casual"
    assert cold.feels_like_c < 20 and cold.weather_stated

    wet = parse("monsoon temple visit")
    assert wet.precip_probability > 0 and wet.weather_stated

    plain = parse("client meeting")
    assert not plain.weather_stated, "must not claim a weather it was not told"


def test_an_unmatched_query_asks_rather_than_guessing() -> None:
    """THE RULE THE WHOLE SYSTEM FOLLOWS.

    `resolve_context` refuses to default an occasion because "the same weather
    calls for very different clothes depending on whether you are interviewing
    or going to the gym". Answering "what should I wear tomorrow?" with a
    casual outfit answers a question the user did not ask.
    """
    intent = parse("what should I wear tomorrow")
    assert intent.occasion is None
    assert intent.needs_disambiguation


def test_parse_never_raises_on_hostile_input() -> None:
    """It takes free text off the internet. Every branch must survive."""
    for query in ["", "   ", "🎉🎉🎉", "'; DROP TABLE garments; --", "a" * 500, "日本語"]:
        result = parse(query)
        assert result.query == query
        assert result.occasion is None or isinstance(result.occasion, str)


# ------------------------------------------------------------- the endpoint


async def test_chat_asks_when_it_cannot_tell(api, registered) -> None:
    """No outfits, no guess, and a usable prompt back."""
    resp = await api.post("/chat", json={"message": "what about tomorrow"}, headers=registered.auth)
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_clarification"] is True
    assert body["outfits"] == []
    assert body["understood"] is None
    assert body["examples"], "asking without examples makes the user guess too"


async def test_chat_resolves_diwali_to_a_festival(api, registered) -> None:
    """The query this feature exists for. The wardrobe here is one unmatted
    garment, so there are no outfits — what matters is that the OCCASION was
    understood and the reply says why it could not dress it, rather than
    returning an empty list with no explanation."""
    resp = await api.post(
        "/chat", json={"message": "What should I wear for Diwali?"}, headers=registered.auth
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["needs_clarification"] is False
    assert body["understood"]["occasion"] == "festival_day"
    assert body["understood"]["matched"] == "diwali"
    assert body["reply"], "a reply with no words is not an answer"


async def test_chat_rejects_an_empty_message(api, registered) -> None:
    """Validation at the edge, so an empty string never reaches the lexicon."""
    resp = await api.post("/chat", json={"message": ""}, headers=registered.auth)
    assert resp.status_code == 422


async def test_chat_requires_auth(api) -> None:
    """It reads a wardrobe. An unauthenticated caller must not reach it."""
    resp = await api.post("/chat", json={"message": "Diwali"})
    assert resp.status_code in (401, 403)
