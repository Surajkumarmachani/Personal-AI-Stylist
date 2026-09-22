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


# ---------------------------------------------------------------- intent_llm
#
# The LLM fallback is reached only on a lexicon MISS, so these tests use
# queries the lexicon deliberately does not know. The gateway is faked for the
# same reason tests/test_rerank.py fakes it: the real free-tier slug rotates
# and rate-limits, and a test that depends on a third party's daily quota
# tells you about the quota, not about this code.


class FakeIntentGateway:
    def __init__(self, *, content: str | None = None, raises: Exception | None = None):
        self.calls: list[dict[str, object]] = []
        self._content = content
        self._raises = raises

    async def chat(self, **kwargs: object) -> object:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises

        class R:
            content = self._content or "{}"

        return R()


def test_the_schema_enum_is_the_taxonomy_plus_unknown() -> None:
    """The enum must be generated from taxonomy.yaml, not typed out.

    A hard-coded list would keep accepting an occasion after it was renamed,
    and the classifier would then hand `resolve_context` an id it rejects —
    a 500 on a chat message.
    """
    from stylist_domain.intent_llm import UNKNOWN, build_schema

    enum = build_schema()["json_schema"]["schema"]["properties"]["occasion"]["enum"]
    assert set(enum) == set(load_taxonomy().occasions) | {UNKNOWN}


def test_an_invented_occasion_is_rejected_not_passed_through() -> None:
    """`strict` is a request the provider may ignore. The validator is the gate.

    This is the failure that matters most: an id outside the taxonomy reaching
    `resolve_context` raises inside a user-facing request.
    """
    from stylist_domain.intent_llm import validate

    assert validate({"occasion": "brunch_with_friends", "confidence": 0.99}) is None


def test_unknown_is_understood_as_not_an_occasion() -> None:
    from stylist_domain.intent_llm import UNKNOWN, validate

    got = validate({"occasion": UNKNOWN, "confidence": 0.9, "alternatives": []})
    assert got is not None
    assert got.occasion is None
    assert not got.confident


def test_a_low_confidence_answer_is_not_acted_on() -> None:
    from stylist_domain.intent_llm import MIN_CONFIDENCE, validate

    got = validate({"occasion": "workout", "confidence": MIN_CONFIDENCE - 0.01})
    assert got is not None and got.occasion == "workout"
    assert not got.confident, "a coin-flip classification must ask, not dress"


def test_alternatives_are_filtered_to_real_occasions() -> None:
    """A bad SUGGESTION is cosmetic where a bad classification is not, so
    unknown ids are dropped rather than failing the whole answer."""
    from stylist_domain.intent_llm import validate

    got = validate(
        {
            "occasion": "casual_outing",
            "confidence": 0.9,
            "alternatives": ["travel_day", "not_a_real_occasion", "casual_outing"],
        }
    )
    assert got is not None
    assert got.alternatives == ("travel_day",), "self and invented ids must be dropped"


@pytest.mark.asyncio
async def test_classifier_rescues_a_query_the_lexicon_cannot_parse(api, registered) -> None:
    """The reported failure: an ordinary sentence with no lexicon keyword got
    "I'm not sure what the occasion is" and five unrelated examples."""
    import json as _json

    from stylist_api.routers import chat as chat_router

    gateway = FakeIntentGateway(
        content=_json.dumps(
            {"occasion": "casual_outing", "confidence": 0.92, "alternatives": ["travel_day"]}
        )
    )
    got = await chat_router._classify(gateway, "sk-tenant", "my cousin's naming ceremony")
    assert got is not None and got.confident
    assert got.occasion == "casual_outing"
    assert gateway.calls, "the classifier must actually call the gateway"


@pytest.mark.asyncio
async def test_every_gateway_failure_degrades_to_asking(api, registered) -> None:
    """Timeout, 429, 404 on a rotated slug, prose instead of JSON — each one
    must return None so the caller asks. This is what keeps the classifier an
    enhancement rather than a dependency."""
    from stylist_api.routers import chat as chat_router

    for failure in (
        FakeIntentGateway(raises=TimeoutError("read timeout")),
        FakeIntentGateway(raises=RuntimeError("HTTP 429 free-models-per-day")),
        FakeIntentGateway(content="I think you should wear something nice!"),
        FakeIntentGateway(content='{"occasion": "made_up", "confidence": 1.0}'),
    ):
        assert await chat_router._classify(failure, "sk-tenant", "anything") is None

    # And with no tenant key there is no budget to bill it to, so it must not
    # call at all rather than land on someone else's key.
    unused = FakeIntentGateway(content='{"occasion":"workout","confidence":1.0}')
    assert await chat_router._classify(unused, None, "gym time") is None
    assert not unused.calls


# ------------------------------------------------------- custom occasions
#
# A custom occasion is an ALIAS for a taxonomy one, never a nineteenth enum
# value — taxonomy.yaml is frozen and its enums generate Postgres types. See
# migration 0018.


@pytest.mark.asyncio
async def test_a_custom_occasion_must_name_a_real_base(api, registered) -> None:
    """`base_occasion` is a plain varchar in the database, deliberately: a FK
    onto a generated enum would couple this table to the freeze it exists to
    avoid. That moves the check into the router, so the check has to be real —
    an unknown base would otherwise reach `resolve_context` and raise inside a
    user-facing request."""
    r = await api.post(
        "/me/occasions",
        json={"name": "brunch o'clock", "base_occasion": "brunch_o_clock"},
        headers=registered.auth,
    )
    assert r.status_code == 400
    assert "base_occasion must be one of" in r.json()["detail"]


@pytest.mark.asyncio
async def test_a_custom_name_beats_the_built_in_lexicon(api, registered) -> None:
    """The ordering IS the feature.

    The lexicon maps bare `office` to `office_casual`, and "office" is longer
    than "party" so it wins the longest-phrase rule. Someone who has named an
    occasion "office party" would therefore get desk clothes for a night out —
    their own words losing to a generic keyword inside them, in their own
    wardrobe.

    Measured against the running API: without the alias the message resolves
    to `office_casual via office`; with it, `party_night via your occasion
    'office party'`; after deleting it, back to `office_casual`.
    """
    from stylist_domain.intent import parse

    msg = "what do i wear to the office party"
    assert parse(msg).occasion == "office_casual", "the control the alias has to beat"

    created = await api.post(
        "/me/occasions",
        json={"name": "office party", "base_occasion": "party_night"},
        headers=registered.auth,
    )
    assert created.status_code == 201

    reply = await api.post("/chat", json={"message": msg}, headers=registered.auth)
    assert reply.json()["understood"]["occasion"] == "party_night"

    gone = await api.delete(
        f"/me/occasions/{created.json()['id']}", headers=registered.auth
    )
    assert gone.status_code == 200
    back = await api.post("/chat", json={"message": msg}, headers=registered.auth)
    assert back.json()["understood"]["occasion"] == "office_casual", (
        "deleting an alias must restore the built-in answer, not leave a hole"
    )


@pytest.mark.asyncio
async def test_two_names_differing_only_in_case_collide(api, registered) -> None:
    """The unique index is on (user_id, lower(name)) because the intent matcher
    lower-cases before comparing — a second alias differing only in case could
    never win a lookup, so accepting it would create a row that does nothing."""
    body = {"name": "Farmhouse Haldi", "base_occasion": "mehendi"}
    first = await api.post("/me/occasions", json=body, headers=registered.auth)
    assert first.status_code == 201
    clash = await api.post(
        "/me/occasions",
        json={"name": "farmhouse haldi", "base_occasion": "mehendi"},
        headers=registered.auth,
    )
    assert clash.status_code == 409, "a user-fixable collision is a 409, not a 500"


@pytest.mark.asyncio
async def test_a_formality_override_reaches_the_scorer(api, registered) -> None:
    """Without overrides this is just a nickname. What people mean by "my own
    occasion" is usually a calibration — "my office is dressier than yours" —
    and formality is the knob `resolve_context` reads.

    `wfh` has `formality_target: 1` in the taxonomy, so an override to 2 is
    only observable if it actually replaced the resolved target.
    """
    created = await api.post(
        "/me/occasions",
        json={"name": "Friday standup", "base_occasion": "wfh", "formality_override": 2},
        headers=registered.auth,
    )
    assert created.status_code == 201

    reply = await api.post(
        "/chat", json={"message": "friday standup in an hour"}, headers=registered.auth
    )
    body = reply.json()
    assert body["understood"]["occasion"] == "wfh"
    assert body["context"]["formality_target"] == 2, "the override must replace the base target"


@pytest.mark.asyncio
async def test_an_out_of_range_formality_is_refused(api, registered) -> None:
    """A target no garment can satisfy presents to the user as "no outfits"
    with no reason given, which is the least debuggable screen in the product.
    Refused at the edge instead."""
    r = await api.post(
        "/me/occasions",
        json={"name": "very formal", "base_occasion": "wfh", "formality_override": 9},
        headers=registered.auth,
    )
    assert r.status_code == 422
