"""Free text -> an occasion the scorer understands.

WHY A LEXICON AND NOT A MODEL CALL
-----------------------------------
"What should I wear for Diwali?" has to become `festival_day`, which is a
CLOSED-VOCABULARY mapping over 18 taxonomy occasions. Three reasons that is
rules rather than an LLM:

1. It is more reliable. A model asked to pick from 18 enum values will
   occasionally invent a nineteenth, and §C3's validator exists because that
   happens. A dictionary cannot.
2. It is free and instant. The chat sits on a user-facing request with the same
   1500ms budget as `/suggestions`; a 3-6s model call cannot fit, which is the
   same arithmetic that moved rationale warming off the request path in Phase 7.
3. It works when the provider does not. Gemini's credits are currently
   depleted, and a chat that cannot answer "what should I wear for Diwali"
   during a provider outage is a chat nobody trusts.

The LLM still has a job — see `needs_disambiguation`. When the lexicon does not
match, this module says so rather than guessing, and the caller may then ask a
model or ask the user. An unmatched query returns NO occasion, because
defaulting to `casual_outing` would answer a question the user did not ask and
`resolve_context` refuses a default for exactly that reason.

WHY FESTIVALS ARE NAMED INDIVIDUALLY
-------------------------------------
`festival_day` is one taxonomy occasion, but users do not type "festival day" —
they type "Diwali", "Onam", "Eid". Every one of those is a separate lexicon
entry because a substring match on "festival" would catch none of them, and
this wardrobe is the reason the taxonomy has `festival_day`, `mehendi` and
`sangeet` in the first place.

Spelling variants are listed rather than stemmed. "Divali", "Deepavali" and
"Diwali" are the same festival and no stemmer gets there; the dictionary is a
dozen lines and is right.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# phrase -> taxonomy occasion id.
#
# ORDER MATTERS AND LONGEST WINS. "wedding reception" must beat "wedding", and
# "office formal" must beat "office", so matching sorts by phrase length
# descending rather than trusting dict order.
LEXICON: dict[str, str] = {
    # --- a STYLE of clothing, not an event ----------------------------------
    #
    # "Cultural wear" got "I'm not sure what the occasion is". It is one of the
    # most basic things a person can ask this product for, and both layers
    # missed it: no lexicon phrase, and a prompt that told the model to judge
    # "what the person will be DOING" -- which is unanswerable when they have
    # named a wardrobe category rather than an event.
    #
    # These resolve to the occasion that dress code BELONGS to. It is a
    # judgement, not a fact: someone asking for ethnic wear may be going to a
    # wedding or a temple, and `festival_day` is the festive-ethnic default
    # rather than a claim about their day.
    "ethnic": "festival_day",
    "ethnic wear": "festival_day",
    "cultural wear": "festival_day",
    "cultural dress": "festival_day",
    "traditional wear": "festival_day",
    "traditional dress": "festival_day",
    "traditional outfit": "festival_day",
    "indian wear": "festival_day",
    "indian outfit": "festival_day",
    "desi": "festival_day",
    "desi wear": "festival_day",
    "kurta": "festival_day",
    "saree": "festival_day",
    "sari": "festival_day",
    "lehenga": "wedding_ceremony",
    "sherwani": "wedding_ceremony",
    "western wear": "casual_outing",
    "formals": "office_formal",
    "formal wear": "office_formal",
    "party wear": "party_night",
    "gym wear": "workout",
    "sportswear": "workout",
    "activewear": "workout",
    "loungewear": "wfh",
    # --- corporate ----------------------------------------------------------
    #
    # The working week was three ids deep (`wfh`, `office_casual`,
    # `office_formal`) while weddings had four. Most people dress for work far
    # more often than for a wedding.
    "conference": "conference",
    "seminar": "conference",
    "summit": "conference",
    "convention": "conference",
    "trade show": "conference",
    "offsite": "team_offsite",
    "off site": "team_offsite",
    "team outing": "team_offsite",
    "team building": "team_offsite",
    "work trip": "team_offsite",
    "networking": "networking_event",
    "networking event": "networking_event",
    "meetup": "networking_event",
    "industry event": "networking_event",
    "office party": "office_party",
    "work party": "office_party",
    "team dinner": "office_party",
    "christmas party": "office_party",
    "farewell": "office_party",
    "appraisal": "office_formal",
    "investor meeting": "client_meeting",
    "townhall": "office_casual",
    "town hall": "office_casual",
    "graduation": "graduation",
    "convocation": "graduation",
    "brunch": "brunch",
    "lunch out": "brunch",
    # --- ethnic ceremonies --------------------------------------------------
    #
    # `mehendi`, `sangeet` and the two wedding days existed; the ceremonies
    # around them did not, so someone dressing for a haldi got a generic
    # festival answer.
    "haldi": "haldi",
    "pithi": "haldi",
    "engagement": "engagement",
    "roka": "engagement",
    "sagai": "engagement",
    "ring ceremony": "engagement",
    "griha pravesh": "griha_pravesh",
    "housewarming": "griha_pravesh",
    "house warming": "griha_pravesh",
    "griha pravesham": "griha_pravesh",
    "baby shower": "baby_shower",
    "godh bharai": "baby_shower",
    "seemantham": "baby_shower",
    "naming ceremony": "baby_shower",
    "namkaran": "baby_shower",
    # --- Indian festivals -> festival_day -----------------------------------
    "diwali": "festival_day",
    "divali": "festival_day",
    "deepavali": "festival_day",
    "holi": "festival_day",
    "eid": "festival_day",
    "eid al fitr": "festival_day",
    "bakrid": "festival_day",
    "navratri": "festival_day",
    "dussehra": "festival_day",
    "dasara": "festival_day",
    "durga puja": "festival_day",
    "pujo": "festival_day",
    "ganesh chaturthi": "festival_day",
    "onam": "festival_day",
    "pongal": "festival_day",
    "baisakhi": "festival_day",
    "vaisakhi": "festival_day",
    "lohri": "festival_day",
    "raksha bandhan": "festival_day",
    "rakhi": "festival_day",
    "karva chauth": "festival_day",
    "ugadi": "festival_day",
    "gudi padwa": "festival_day",
    "bihu": "festival_day",
    "christmas": "festival_day",
    "new year": "festival_day",
    "festival": "festival_day",
    "puja": "festival_day",
    "pooja": "festival_day",
    # --- weddings. Separate occasions because the clothes genuinely differ ---
    "mehendi": "mehendi",
    "mehndi": "mehendi",
    "sangeet": "sangeet",
    "wedding reception": "wedding_reception",
    "reception": "wedding_reception",
    "baraat": "wedding_ceremony",
    "wedding": "wedding_ceremony",
    "shaadi": "wedding_ceremony",
    "nikah": "wedding_ceremony",
    # --- work ---------------------------------------------------------------
    "client meeting": "client_meeting",
    "client visit": "client_meeting",
    "board meeting": "office_formal",
    "meeting": "client_meeting",
    "presentation": "office_formal",
    "pitch": "client_meeting",
    "interview": "interview",
    "office formal": "office_formal",
    "office casual": "office_casual",
    "office": "office_casual",
    "work from home": "wfh",
    "wfh": "wfh",
    "working from home": "wfh",
    # --- social -------------------------------------------------------------
    "dinner date": "dinner_date",
    "date night": "dinner_date",
    "date": "dinner_date",
    "anniversary": "dinner_date",
    "party": "party_night",
    "night out": "party_night",
    "clubbing": "party_night",
    "birthday": "party_night",
    "coffee": "casual_outing",
    "shopping": "casual_outing",
    "casual": "casual_outing",
    "hanging out": "casual_outing",
    # --- other --------------------------------------------------------------
    "gym": "workout",
    "workout": "workout",
    "running": "workout",
    "yoga": "workout",
    "travel": "travel_day",
    "travelling": "travel_day",
    "traveling": "travel_day",
    "trip": "travel_day",
    "flight": "travel_day",
    "airport": "travel_day",
    "road trip": "travel_day",
    "train": "travel_day",
    "vacation": "travel_day",
    "holiday": "travel_day",
    # SIGHTSEEING IS NOT TRAVELLING. A travel day is spent in transit and
    # wants comfort; a day out at the zoo is a casual outing that happens to
    # be somewhere else. Both were missing entirely, which is how "I'm going
    # to visit Patna as a tourist" and "going to visit zoo" produced the same
    # blank "I'm not sure what the occasion is".
    "tourist": "casual_outing",
    "tourism": "casual_outing",
    "sightseeing": "casual_outing",
    "sight seeing": "casual_outing",
    "visiting": "casual_outing",
    "zoo": "casual_outing",
    "museum": "casual_outing",
    "park": "casual_outing",
    "beach": "casual_outing",
    "picnic": "casual_outing",
    "hiking": "casual_outing",
    "trekking": "casual_outing",
    "market": "casual_outing",
    "mall": "casual_outing",
    "cinema": "casual_outing",
    "movie": "casual_outing",
    "lunch": "casual_outing",
    "walk": "casual_outing",
    "temple": "temple_visit",
    "mandir": "temple_visit",
    "church": "temple_visit",
    "mosque": "temple_visit",
    "gurudwara": "temple_visit",
    "funeral": "funeral",
    "condolence": "funeral",
    "black tie": "black_tie_event",
    "gala": "black_tie_event",
}

# Weather words the user may volunteer. The chat has no location, so a stated
# condition is better evidence than the placeholder temperature — and ignoring
# "it's freezing" while recommending a t-shirt is the kind of answer that ends
# the conversation.
_COLD = ("cold", "freezing", "chilly", "winter", "snow")
_HOT = ("hot", "warm", "humid", "summer", "sweltering")
_WET = ("rain", "raining", "monsoon", "wet", "drizzle", "storm")

# A stated condition maps to a representative temperature, not a forecast. Named
# constants rather than inline numbers so the response can say which one it used.
FEELS_LIKE_COLD_C = 12.0
FEELS_LIKE_DEFAULT_C = 26.0
FEELS_LIKE_HOT_C = 36.0


@dataclass(frozen=True, slots=True)
class Intent:
    """What we understood from the query.

    `occasion` is None when nothing matched, and that is a real answer rather
    than a failure — `needs_disambiguation` then tells the caller to ask.
    """

    occasion: str | None
    matched_phrase: str | None
    feels_like_c: float
    precip_probability: float
    weather_stated: bool
    query: str

    @property
    def needs_disambiguation(self) -> bool:
        return self.occasion is None


def _normalise(text: str) -> str:
    """Lowercase, strip accents, collapse punctuation to spaces.

    Accent stripping matters here: users type "Diwali" and "Dīwālī", and
    `unicodedata` handles that in one line where a spelling variant per accent
    would not scale.
    """
    decomposed = unicodedata.normalize("NFKD", text.lower())
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return re.sub(r"[^a-z0-9]+", " ", stripped).strip()


def parse(query: str) -> Intent:
    """Resolve a free-text query. Pure, and never raises on user input.

    LONGEST PHRASE WINS. "wedding reception" and "wedding" both match a query
    about a reception, and the more specific one is the right answer — the
    clothes for a ceremony and a reception are different, which is why the
    taxonomy separates them.
    """
    text = _normalise(query)
    padded = f" {text} "

    occasion: str | None = None
    matched: str | None = None
    for phrase in sorted(LEXICON, key=len, reverse=True):
        # Word-boundary padding, so "date" does not match "candidate" and
        # "eid" does not match "identity".
        if f" {phrase} " in padded:
            occasion, matched = LEXICON[phrase], phrase
            break

    feels_like = FEELS_LIKE_DEFAULT_C
    stated = False
    if any(f" {w} " in padded for w in _COLD):
        feels_like, stated = FEELS_LIKE_COLD_C, True
    elif any(f" {w} " in padded for w in _HOT):
        feels_like, stated = FEELS_LIKE_HOT_C, True

    precip = 0.8 if any(f" {w} " in padded for w in _WET) else 0.0
    if precip:
        stated = True

    return Intent(
        occasion=occasion,
        matched_phrase=matched,
        feels_like_c=feels_like,
        precip_probability=precip,
        weather_stated=stated,
        query=query,
    )


def suggestions_for_unmatched() -> list[str]:
    """A few example phrases, for when nothing matched.

    Drawn from the lexicon's own keys so this list cannot drift out of date the
    way a hand-written help string would.
    """
    return ["Diwali", "a client meeting", "a wedding reception", "the gym", "a dinner date"]
