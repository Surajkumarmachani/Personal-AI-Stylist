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

NO MODEL CALL ON THIS PATH
--------------------------
Intent resolution is a lexicon (see `stylist_domain.intent` for the three
reasons). The outfits themselves may be reranked by the LLM exactly as
`/suggestions` does, on the same budget and with the same degrade — this
endpoint adds no new dependency and works during a provider outage, which is
when someone asking "what do I wear in an hour" least wants a spinner.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Query
from pydantic import BaseModel, Field

from stylist_api.deps import (
    CacheRedisDep,
    CurrentUser,
    LiteLLMDep,
    ObjectStoreDep,
    SettingsDep,
    TenantDB,
)
from stylist_api.routers.suggestions import get_suggestions
from stylist_domain.intent import parse, suggestions_for_unmatched

router = APIRouter(tags=["chat"])

# Enough to be useful in a chat bubble, few enough to read on a phone. The
# suggestions endpoint's own default is 10, for a grid that scrolls.
CHAT_OUTFIT_LIMIT = 4


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=500)


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
    }.get(occasion, occasion.replace("_", " "))


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

    if intent.needs_disambiguation:
        # ASKS, does not guess. See the module docstring.
        examples = suggestions_for_unmatched()
        return {
            "reply": (
                "I'm not sure what the occasion is — tell me and I'll pull "
                "something from your wardrobe."
            ),
            "understood": None,
            "outfits": [],
            "examples": examples,
            "needs_clarification": True,
        }

    assert intent.occasion is not None  # narrowed by needs_disambiguation
    result = await get_suggestions(
        user=user,
        db=db,
        store=store,
        settings=settings,
        gateway=gateway,
        cache=cache,
        occasion=intent.occasion,
        feels_like_c=intent.feels_like_c,
        precip_probability=intent.precip_probability,
        limit=limit,
    )

    outfits = result.get("outfits") or []
    phrase = _phrase_for(intent.occasion)

    if not outfits:
        # The wardrobe could not dress this occasion. The NOTES say why —
        # "no wearable feet", "everything in the wash" — and passing them
        # through is the difference between a useful answer and a shrug.
        notes = result.get("notes") or []
        return {
            "reply": (
                f"I couldn't put together an outfit for {phrase}. "
                + (notes[0] if notes else "There isn't enough in your wardrobe yet.")
            ),
            "understood": {
                "occasion": intent.occasion,
                "matched": intent.matched_phrase,
                "feels_like_c": intent.feels_like_c,
                "weather_stated": intent.weather_stated,
            },
            "outfits": [],
            "notes": notes,
            "needs_clarification": False,
        }

    weather_note = ""
    if intent.weather_stated:
        # Only mentioned when the USER raised it. Volunteering "I assumed 26°C"
        # on every reply is noise, and on a query that said nothing about
        # weather it is also a claim we cannot support — there is no location.
        weather_note = (
            " I've taken the rain into account."
            if intent.precip_probability
            else f" I've dressed it for around {intent.feels_like_c:.0f}°C."
        )

    return {
        "reply": f"Here's what I'd wear for {phrase}.{weather_note}",
        "understood": {
            "occasion": intent.occasion,
            "matched": intent.matched_phrase,
            "feels_like_c": intent.feels_like_c,
            "weather_stated": intent.weather_stated,
        },
        "outfits": outfits,
        # Passed through so the UI can show the same provenance the
        # suggestions screen does. A chat that hides whether a model ranked the
        # answer is a chat that cannot be debugged when it is odd.
        "ranking_source": result.get("ranking_source"),
        "served_from": result.get("served_from"),
        "explored_slots": result.get("explored_slots"),
        "context": result.get("context"),
        "notes": result.get("notes") or [],
        "needs_clarification": False,
    }
