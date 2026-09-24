"""Ask for an outfit in words: "what should I wear for Diwali?" (Phase 12).

WHY THIS IS A THIN ROUTER
-------------------------
It resolves an intent and then calls `/suggestions`' own handler. It does NOT
re-implement wardrobe loading, scoring, reranking or the bandit — a second path
to the same answer is a second path to DIFFERENT answers, and this codebase has
already paid for that twice (the precompute vs request-path ranking, and the
`_hydrate` preference-facts gap where a rule enforced on one path and not the
other looked like it worked).

So the only thing here is: words in, `OutfitContext` out, existing machinery,
words back.

WHAT IT DOES NOT DO: GUESS
--------------------------
An unmatched query returns NO outfits and asks. `resolve_context` refuses to
default an occasion because "the same weather calls for very different clothes
depending on whether you are interviewing or going to the gym" — answering
"what should I wear tomorrow?" with a casual outfit is confidently answering a
question the user did not ask, and the whole system's rule is that an absent
answer beats a confidently wrong one.

NO MODEL CALL ON THE FAST PATH
------------------------------
Intent resolution is a lexicon first (see `stylist_domain.intent` for the
three reasons). A model is consulted ONLY when the lexicon finds nothing —
see `stylist_domain.intent_llm`, which pays for each of those three
objections rather than waiving them. "Diwali" still costs nothing and answers
instantly; "visiting Patna as a tourist" now gets an answer instead of a
shrug. Every failure of that call degrades to the clarifying question, so the
endpoint still works during a provider outage.

The outfits themselves may be reranked by the LLM exactly as `/suggestions`
does, on the same budget and with the same degrade.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field
from sqlalchemy import text

from stylist_api.deps import (
    CacheRedisDep,
    CurrentUser,
    LiteLLMDep,
    ObjectStoreDep,
    SettingsDep,
    TenantDB,
)
from stylist_api.routers.suggestions import get_suggestions
from stylist_domain.intent import _normalise, parse, suggestions_for_unmatched
from stylist_domain.intent_llm import (
    INTENT_TIMEOUT_S,
    LLMIntent,
    build_schema,
    parse_answer,
)
from stylist_domain.intent_llm import (
    MODEL as INTENT_MODEL,
)
from stylist_domain.intent_llm import (
    SYSTEM_PROMPT as INTENT_SYSTEM_PROMPT,
)
from stylist_domain.shortfall import (
    ADVICE_TIMEOUT_S,
    build_user_message,
    parse_advice,
)
from stylist_domain.shortfall import (
    MODEL as ADVICE_MODEL,
)
from stylist_domain.shortfall import (
    SYSTEM_PROMPT as ADVICE_SYSTEM_PROMPT,
)
from stylist_domain.shortfall import (
    build_schema as build_advice_schema,
)

logger = logging.getLogger(__name__)

router = APIRouter(tags=["chat"])

# Enough to be useful in a chat bubble, few enough to read on a phone. The
# suggestions endpoint's own default is 10, for a grid that scrolls.
CHAT_OUTFIT_LIMIT = 4


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)


async def _classify(gateway: Any, api_key: str | None, message: str) -> LLMIntent | None:
    """Ask the model what occasion this is. None on any failure.

    EVERY failure returns None and the caller asks the user — timeout, outage,
    quota, unparseable answer, an id outside the taxonomy. That is what keeps
    this an enhancement rather than a dependency: before it existed, a lexicon
    miss produced the clarifying question, and if this never answers, a lexicon
    miss still produces the clarifying question.
    """
    if gateway is None or not api_key:
        # No tenant key means no budget to bill this to (§B3). Asking the user
        # is the honest answer, not billing someone else's key.
        return None
    try:
        resp = await gateway.chat(
            model=INTENT_MODEL,
            messages=[
                {"role": "system", "content": INTENT_SYSTEM_PROMPT},
                {"role": "user", "content": message},
            ],
            # The tenant's own virtual key, never the master key — same rule
            # the reranker follows, for the same reason.
            api_key=api_key,
            response_format=build_schema(),
            timeout=INTENT_TIMEOUT_S,
        )
        # NOT `validate(json.loads(...))`. The free tier's models are reasoning
        # models that ignore `strict`, and the measured failure was a CORRECT
        # classification wrapped in unparseable JSON — thrown away by the
        # `json.loads` that used to be here. `parse_answer` tries a clean parse
        # first and salvages only after it fails; `validate` still gates the id.
        return parse_answer(resp.content)
    except Exception:
        # Deliberately broad. Timeout, 429, 404 on a rotated free-tier slug,
        # prose instead of JSON, an id outside the taxonomy — every one of
        # them means "ask the user", and enumerating them would only risk
        # missing one and turning a clarifying question into a 500.
        logger.info("intent classifier unusable; asking the user instead", exc_info=True)
        return None


async def _advise(
    gateway: Any,
    api_key: str | None,
    occasion_phrase: str,
    dress_code: str,
    gap_note: str,
    dresses_as: str | None = None,
) -> str | None:
    """What this occasion is usually worn in. None on any failure.

    Reached only when the wardrobe produced NO outfits, so there is no answer
    for this to degrade — the reply it decorates is already the one the user
    would have got. Every failure path returns None and the reply stands
    without it, exactly as `_classify` treats the clarifying question.
    """
    if gateway is None or not api_key:
        return None
    try:
        resp = await gateway.chat(
            model=ADVICE_MODEL,
            messages=[
                {"role": "system", "content": ADVICE_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": build_user_message(
                        occasion_phrase, dress_code, gap_note, dresses_as
                    ),
                },
            ],
            api_key=api_key,
            response_format=build_advice_schema(),
            timeout=ADVICE_TIMEOUT_S,
        )
        return parse_advice(resp.content)
    except Exception:
        # Broad for the same reason as `_classify`: every one of timeout,
        # quota, prose-instead-of-JSON and a rotated model slug means "say the
        # gap and nothing more", and that is already a complete answer.
        logger.info("shortfall advice unusable; replying without it", exc_info=True)
        return None


def _phrase_for(occasion: str) -> str:
    """How to say the occasion back to the user.

    Taxonomy ids are snake_case machine identifiers (`wedding_reception`), and
    echoing one verbatim reads like a database error. Unlisted ids fall back to
    a de-underscored form rather than raising — a new occasion in the taxonomy
    should degrade to slightly stiff prose, never to a 500.
    """
    return {
        "festival_day": "the festival",
        "mehendi": "the mehendi",
        "sangeet": "the sangeet",
        "wedding_ceremony": "the wedding",
        "wedding_reception": "the reception",
        "client_meeting": "your client meeting",
        "office_formal": "a formal day at the office",
        "office_casual": "the office",
        "interview": "your interview",
        "wfh": "working from home",
        "dinner_date": "your dinner date",
        "party_night": "the party",
        "casual_outing": "heading out",
        "workout": "your workout",
        "travel_day": "travelling",
        "temple_visit": "the temple",
        "funeral": "the service",
        "black_tie_event": "a black-tie evening",
        # Added with migration 0024. The fallback below would render
        # `griha_pravesh` as "griha pravesh" and `team_offsite` as "team
        # offsite" — readable, but the article is what makes the sentence a
        # sentence: "what I'd wear for the offsite", not "for team offsite".
        "conference": "the conference",
        "networking_event": "the networking event",
        "office_party": "the office party",
        "team_offsite": "the offsite",
        "haldi": "the haldi",
        "engagement": "the engagement",
        "griha_pravesh": "the griha pravesh",
        "baby_shower": "the baby shower",
        # No article: "what I'd wear for brunch" is how people say it.
        "brunch": "brunch",
        "graduation": "the graduation",
    }.get(occasion, occasion.replace("_", " "))


async def _custom_match(db: Any, message: str) -> dict[str, Any] | None:
    """A user's own occasion name, matched before anything else.

    CUSTOM NAMES WIN OVER THE BUILT-IN LEXICON, and that ordering is the whole
    feature. If you have named an occasion "office party", the lexicon's
    `"office" -> office_casual` would otherwise claim it and you would get
    desk clothes for a night out — your own words losing to a generic keyword
    in your own wardrobe.

    Longest name first, for the reason `intent.parse` sorts its lexicon that
    way: "farmhouse haldi" must beat "haldi" when both are yours.

    Word-boundary matched on a normalised string, so "date" in your alias does
    not fire on "candidate", exactly as the built-in lexicon does. Both use
    `_normalise`, so the two cannot disagree about what a word is.
    """
    rows = await db.execute(
        text(
            "SELECT name, base_occasion, formality_override, dress_code_override "
            "FROM custom_occasion"
        )
    )
    items = [dict(r) for r in rows.mappings()]
    if not items:
        return None
    padded = f" {_normalise(message)} "
    for item in sorted(items, key=lambda i: len(str(i["name"])), reverse=True):
        needle = _normalise(str(item["name"]))
        if needle and f" {needle} " in padded:
            return item
    return None


@router.post("/chat")
async def chat(
    body: ChatRequest,
    user: CurrentUser,
    db: TenantDB,
    store: ObjectStoreDep,
    settings: SettingsDep,
    gateway: LiteLLMDep,
    cache: CacheRedisDep,
    limit: Annotated[int, Query(ge=1, le=10)] = CHAT_OUTFIT_LIMIT,
) -> dict[str, Any]:
    """Answer a wardrobe question with real outfits from this wardrobe."""
    intent = parse(body.message)
    occasion = intent.occasion
    matched = intent.matched_phrase

    # BEFORE the lexicon's answer is accepted, not after: a custom name is the
    # user's own vocabulary and outranks a generic keyword that happens to
    # appear inside it.
    custom = await _custom_match(db, body.message)
    formality_override: int | None = None
    dress_code_override: str | None = None
    if custom is not None:
        occasion = str(custom["base_occasion"])
        matched = f"your occasion {custom['name']!r}"
        formality_override = custom["formality_override"]
        dress_code_override = custom["dress_code_override"]

    if occasion is None:
        # The lexicon found nothing. Ask a model BEFORE giving up — this is
        # the long tail ("visiting Patna as a tourist", "meeting my
        # girlfriend's parents"), and the alternative is a dead end.
        key_row = await db.execute(text("SELECT litellm_key FROM user_profile LIMIT 1"))
        guess = await _classify(gateway, key_row.scalar(), body.message)

        if guess is not None and guess.confident:
            occasion = guess.occasion
            matched = "understood from your message"
        else:
            # STILL ASKS, and still does not guess. But it asks a question
            # someone can answer.
            #
            # The old reply was "I'm not sure what the occasion is" plus five
            # FIXED examples — Diwali, a client meeting, a wedding reception —
            # which had nothing to do with what the user typed. Two real
            # queries that hit it were "I'm going to visit Patna as a tourist"
            # and "going to visit zoo", and offering "Diwali" to either is a
            # form letter, not a question.
            #
            # When the model had a view but was not sure enough to act on it,
            # its guesses become the options. `suggestions_for_unmatched()` is
            # the fallback for when there is genuinely nothing to go on — a
            # greeting, a question about the app.
            proposed = []
            if guess is not None:
                proposed = [o for o in (guess.occasion, *guess.alternatives) if o]
            if proposed:
                phrases = [_phrase_for(o) for o in proposed[:3]]
                joined = (
                    phrases[0]
                    if len(phrases) == 1
                    else (" or ".join([", ".join(phrases[:-1]), phrases[-1]]))
                )
                reply = f"Did you mean {joined}? Say which and I'll pull something."
            else:
                reply = (
                    "I'm not sure what the occasion is — tell me and I'll pull "
                    "something from your wardrobe."
                )
            return {
                "reply": reply,
                "understood": None,
                "outfits": [],
                # Real occasions when we have them, so the UI can offer them as
                # buttons rather than asking the user to retype.
                "examples": [_phrase_for(o) for o in proposed[:3]] or suggestions_for_unmatched(),
                "suggested_occasions": proposed[:3],
                "needs_clarification": True,
            }

    assert occasion is not None
    result = await get_suggestions(
        user=user,
        db=db,
        store=store,
        settings=settings,
        gateway=gateway,
        cache=cache,
        occasion=occasion,
        # None UNLESS THE USER ACTUALLY SAID SOMETHING ABOUT THE WEATHER.
        #
        # `intent.feels_like_c` is never None — it defaults to
        # FEELS_LIKE_DEFAULT_C (26.0) so `resolve_context` always has a
        # number. Passing that straight through made every chat message look
        # like an explicit `?feels_like_c=26`, which `/suggestions` honours as
        # a deliberate override — so the forecast for the user's own city was
        # fetched, then ignored, and the response reported `user-stated` for a
        # temperature the user never stated.
        #
        # `weather_stated` is the field that distinguishes a default from an
        # answer, and it existed precisely for this.
        feels_like_c=intent.feels_like_c if intent.weather_stated else None,
        precip_probability=intent.precip_probability,
        limit=limit,
        formality_override=formality_override,
        dress_code_override=dress_code_override,
    )

    outfits = result.get("outfits") or []
    phrase = _phrase_for(occasion)

    if not outfits:
        # The wardrobe could not dress this occasion. The NOTES say why —
        # "no wearable feet", "everything in the wash" — and passing them
        # through is the difference between a useful answer and a shrug.
        notes = result.get("notes") or []
        # A BLOCKING note, never an "incomplete" one. "These outfits are shown
        # without shoes" was being quoted as the reason there were no outfits
        # at all — a reply that contradicted itself in one sentence.
        blocking = result.get("blocking_notes") or []
        gap = blocking[0] if blocking else "There isn't enough in your wardrobe for this yet."
        # The notes are written as clauses ("no wearable shoes — ..."); here
        # one starts a sentence.
        gap = gap[:1].upper() + gap[1:]
        if not gap.endswith((".", "!", "?")):
            gap += "."

        # THE GAP IS NOT AN ANSWER ON ITS OWN. It says which piece is missing;
        # it does not say what the occasion is dressed in, which is what was
        # actually asked. "Cultural wear" got a correct, complete, useless
        # reply for exactly this reason.
        #
        # Best-effort and last: the reply above is already the honest answer,
        # and this only adds to it when the model is reachable and stays
        # inside its one sentence.
        ctx_for_advice = result.get("context") or {}
        profile = (
            await db.execute(text("SELECT litellm_key, dresses_as FROM user_profile LIMIT 1"))
        ).one_or_none()
        advice = await _advise(
            gateway,
            profile[0] if profile else None,
            phrase,
            str(ctx_for_advice.get("dress_code_target") or ""),
            gap,
            # Whose clothes to describe: without it the advice named a saree
            # and a sherwani in one sentence. See migration 0026.
            profile[1] if profile else None,
        )

        return {
            "reply": (
                f"I couldn't put together an outfit for {phrase}. "
                + gap
                + (f" {advice}" if advice else "")
            ),
            "understood": {
                "occasion": occasion,
                "matched": matched,
                "feels_like_c": intent.feels_like_c,
                "weather_stated": intent.weather_stated,
            },
            "outfits": [],
            "notes": notes,
            # CARRIED ON THE EMPTY REPLY TOO. This is the branch where a user
            # asked "what do I wear" and got nothing, so what the system was
            # AIMING at — formality, dress code, the temperature it used — is
            # more actionable here than on a reply that already shows outfits.
            # It was omitted, which also made a custom occasion's formality
            # override unobservable exactly when the wardrobe could not meet it.
            "context": result.get("context"),
            "needs_clarification": False,
        }

    # WHAT THE OUTFIT WAS ACTUALLY DRESSED FOR, read off the resolved context
    # rather than off the intent.
    #
    # It used to come from `intent`, and was mentioned only when the USER had
    # raised the weather — because on any other query the temperature was the
    # hard-coded 26.0 placeholder and, as the old comment said, "a claim we
    # cannot support: there is no location". There is one now, so the
    # condition has changed rather than the caution: a real forecast is worth
    # saying, a placeholder still is not.
    ctx_out = result.get("context") or {}
    source = str(ctx_out.get("weather_source") or "")
    feels = ctx_out.get("feels_like_c")
    wet = bool(ctx_out.get("wet"))

    weather_note = ""
    if source.startswith("forecast") and feels is not None:
        weather_note = f" It's {float(feels):.0f}°C where you are"
        weather_note += ", and I've allowed for the rain." if wet else "."
    elif intent.weather_stated:
        weather_note = (
            " I've taken the rain into account."
            if intent.precip_probability
            else f" I've dressed it for around {intent.feels_like_c:.0f}°C."
        )
    elif source.startswith("placeholder"):
        # Said once, in the reply, rather than buried in a debug field. A user
        # who sets a city gets better suggestions, and this is the only place
        # they would ever learn that.
        weather_note = " Set your city in Profile and I'll use the real temperature."

    return {
        "reply": f"Here's what I'd wear for {phrase}.{weather_note}",
        "understood": {
            "occasion": occasion,
            "matched": matched,
            "feels_like_c": intent.feels_like_c,
            "weather_stated": intent.weather_stated,
        },
        "outfits": outfits,
        # Passed through so the UI can show the same provenance the
        # suggestions screen does. A chat that hides whether a model ranked the
        # answer is a chat that cannot be debugged when it is odd.
        "ranking_source": result.get("ranking_source"),
        "weather_source": source or None,
        "served_from": result.get("served_from"),
        "explored_slots": result.get("explored_slots"),
        "context": result.get("context"),
        "notes": result.get("notes") or [],
        "needs_clarification": False,
    }
