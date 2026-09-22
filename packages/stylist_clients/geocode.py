"""City name -> coordinates, via Open-Meteo's geocoding API.

WHY GEOCODE AT ALL INSTEAD OF ASKING THE BROWSER
------------------------------------------------
`navigator.geolocation` is more accurate and worse for this product. It fires
a permission prompt at a user who has not yet been told why, returns a
precision we deliberately throw away (the weather client rounds to 2dp ~=
1.1km), and on a desktop it is often wrong anyway because it geolocates the
ISP. Asking "which city?" once is a question the user can answer, audit and
change, and it is the only location this system ever needs.

SAME 2dp ROUNDING AS THE WEATHER CLIENT, APPLIED HERE TOO
---------------------------------------------------------
`user_profile.home_lat_2dp` / `home_lon_2dp` are numeric(5,2), so the schema
already refuses to store more than this. Rounding here as well means what we
send upstream matches what we persist, and nobody reading one has to go and
check the other.

NO API KEY, AND NO NEW DPA SURFACE
----------------------------------
Open-Meteo's geocoding is free and unauthenticated, like its forecast API. The
only thing sent is a city name the user typed — not a device location — so
this adds no personal data to an external call that was not already the
answer to "where do you live", at city granularity.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://geocoding-api.open-meteo.com/v1/search"

# Matches weather.py's COORD_PRECISION and the numeric(5,2) columns.
COORD_PRECISION = 2

# Short, for the same reason weather.py's is short: this runs inside a request
# where the user is waiting, and the fallback (ask again) is cheap.
REQUEST_TIMEOUT = 5.0


class GeocodeUnavailable(Exception):  # noqa: N818 - a state, not an error type
    """Upstream unreachable or malformed. The caller asks the user to retry."""


class PlaceNotFound(Exception):  # noqa: N818 - a state, not an error type
    """The query matched nothing. A typo, not an outage — different message."""


@dataclass(frozen=True, slots=True)
class Place:
    """A resolved location, at the only precision this system keeps."""

    label: str
    latitude: float
    longitude: float
    timezone: str | None


def _round(value: float) -> float:
    return round(float(value), COORD_PRECISION)


def _label(hit: dict[str, object]) -> str:
    """"Patna, Bihar, India" — enough to tell two same-named cities apart.

    There are Hyderabads in India and Pakistan and a dozen Springfields. Echoing
    only the name the user typed would let them confirm a city they did not
    mean, and the weather would be quietly wrong forever after.
    """
    parts = [
        str(hit.get("name") or "").strip(),
        str(hit.get("admin1") or "").strip(),
        str(hit.get("country") or "").strip(),
    ]
    return ", ".join(p for p in parts if p)


# How many candidates to offer. Five covers the same-name collisions that
# actually occur (Hyderabad returns five) without turning a one-question
# onboarding step into a list to read.
MAX_CANDIDATES = 5


async def search(query: str, *, limit: int = MAX_CANDIDATES) -> list[Place]:
    """Candidates for a place name, most populous first. Never guesses silently.

    MOST POPULOUS FIRST, AND THE USER STILL SEES THE LIST. Measured against
    the live API 2026-09-22:

      "Patna"      -> Patna, Bihar, India (pop 1,684,297) beats Patna,
                      Scotland (pop 2,190). First-match order returned the
                      Indian city here by luck.
      "Hyderabad"  -> Telangana, India (6.99M) beats Hyderabad, Sindh,
                      Pakistan (1.92M). First-match order got this right too.
      "Bangalore"  -> the ONLY upstream match is "Bangalore Town, Sindh,
                      PAKISTAN". The Indian city is indexed as Bengaluru, so
                      NO ranking rule rescues this one.

    That third case is why this returns a list rather than an answer. Storing
    the top hit and saying nothing would have set a Bangalore user's weather
    to Sindh permanently, and they would have had no way to notice — the
    suggestions would just be subtly wrong forever. The caller shows the
    resolved label and the alternatives, so a wrong match is visible in one
    glance and correctable in one tap.

    Population is missing for many entries; those sort last rather than being
    dropped, because a small town with no population figure is still the right
    answer for someone who lives there.
    """
    name = query.strip()
    if not name:
        raise PlaceNotFound("no place given")

    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
            resp = await client.get(
                BASE_URL,
                params={
                    "name": name,
                    "count": max(1, min(limit, MAX_CANDIDATES)),
                    "language": "en",
                    "format": "json",
                },
            )
            resp.raise_for_status()
            payload = resp.json()
    except Exception as exc:
        raise GeocodeUnavailable(f"{type(exc).__name__}: {exc}") from exc

    results = payload.get("results") or []
    if not results:
        # A 200 with no results is this API's normal "no such place", not an
        # outage. Distinguished from GeocodeUnavailable so the user is told to
        # check the spelling rather than to try again later.
        raise PlaceNotFound(f"no place matched {name!r}")

    places: list[tuple[int, Place]] = []
    for hit in results:
        lat, lon = hit.get("latitude"), hit.get("longitude")
        if lat is None or lon is None:
            continue
        population = hit.get("population")
        places.append(
            (
                int(population) if isinstance(population, (int, float)) else -1,
                Place(
                    label=_label(hit) or name,
                    latitude=_round(float(lat)),
                    longitude=_round(float(lon)),
                    timezone=(str(hit["timezone"]) if hit.get("timezone") else None),
                ),
            )
        )
    if not places:
        raise GeocodeUnavailable("no match carried coordinates")

    places.sort(key=lambda pair: -pair[0])
    return [place for _, place in places]


async def resolve(query: str) -> Place:
    """The single best candidate. For callers that cannot offer a choice."""
    return (await search(query, limit=MAX_CANDIDATES))[0]
