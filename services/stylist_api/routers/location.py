"""Where the user lives, at city granularity (wires Phase 6.1's weather).

WHY THIS ENDPOINT EXISTS AT ALL
-------------------------------
`weather_fit` carries 0.15 of the outfit score and `monsoon_suitability` is a
HARD filter, so weather is not a garnish on this ranking — and until now both
ran on `DEFAULT_FEELS_LIKE_C = 26.0`, a constant. `packages/stylist_clients/
weather.py` was written in Phase 6 with cache keys, 2dp privacy rounding and a
degrade path, and had ZERO callers, because nothing could answer "where?".

One question, asked once, is the whole missing piece.

CITY, NOT COORDINATES FROM THE BROWSER
--------------------------------------
`navigator.geolocation` is more precise and worse here. It fires a permission
prompt before the user has been told why, returns precision this system throws
away (2dp, ~1.1km), and on desktop frequently geolocates the ISP instead of the
person. A city is a question someone can answer, audit and change.

THE RESOLVED LABEL IS RETURNED, AND SO ARE THE ALTERNATIVES
-----------------------------------------------------------
Measured against the live geocoder: "Bangalore" resolves ONLY to `Bangalore
Town, Sindh, Pakistan`, because the Indian city is indexed as Bengaluru. No
ranking rule fixes that. So this endpoint never quietly accepts its own best
guess — it stores it and hands back both the label and the runners-up, and the
UI shows them. A wrong match becomes visible in one glance instead of skewing
every suggestion forever.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from stylist_api.deps import CurrentUser, TenantDB
from stylist_clients.geocode import GeocodeUnavailable, Place, PlaceNotFound, search

router = APIRouter(tags=["location"])


class LocationRequest(BaseModel):
    # A city name, not coordinates. Accepting lat/lon from the client would
    # let a caller store precision the rest of the system promises not to keep.
    place: str = Field(min_length=1, max_length=120)
    # Which candidate to accept, for the second call after a user corrects a
    # wrong match. Defaults to the best one.
    choice: int = Field(default=0, ge=0, le=4)


def _payload(place: Place | None, alternatives: list[Place]) -> dict[str, Any]:
    return {
        "place": place.label if place else None,
        "latitude": float(place.latitude) if place else None,
        "longitude": float(place.longitude) if place else None,
        "timezone": place.timezone if place else None,
        # Named `alternatives`, not `results`: these are the ones NOT chosen,
        # offered so a wrong match is correctable.
        "alternatives": [
            {"place": p.label, "index": i} for i, p in enumerate(alternatives) if p != place
        ],
    }


@router.get("/me/location")
async def get_location(user: CurrentUser, db: TenantDB) -> dict[str, Any]:
    """The stored city, or nulls. Nulls mean suggestions use the placeholder."""
    row = await db.execute(
        text("SELECT home_place, home_lat_2dp, home_lon_2dp, timezone FROM user_profile LIMIT 1")
    )
    got = row.mappings().one_or_none()
    return {
        "place": got["home_place"] if got else None,
        "latitude": float(got["home_lat_2dp"]) if got and got["home_lat_2dp"] is not None else None,
        "longitude": float(got["home_lon_2dp"])
        if got and got["home_lon_2dp"] is not None
        else None,
        "timezone": got["timezone"] if got else None,
        # Stated rather than inferred by the client from whether place is null.
        "weather_is_real": bool(got and got["home_lat_2dp"] is not None),
    }


@router.put("/me/location")
async def set_location(body: LocationRequest, user: CurrentUser, db: TenantDB) -> dict[str, Any]:
    try:
        candidates = await search(body.place)
    except PlaceNotFound as exc:
        # A typo, not an outage — 404 and a message about spelling, because
        # "try again later" would be advice that never works.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc
    except GeocodeUnavailable as exc:
        # 503, not 500: the user did nothing wrong and retrying is the right
        # advice. Suggestions keep working on the placeholder meanwhile.
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"could not look that up right now ({exc})",
        ) from exc

    chosen = candidates[min(body.choice, len(candidates) - 1)]

    await db.execute(
        text(
            """
            UPDATE user_profile
            SET home_place = :place,
                home_lat_2dp = :lat,
                home_lon_2dp = :lon,
                -- The timezone comes from the same lookup. Time-of-day rules
                -- do not exist yet, but storing a timezone that contradicts
                -- the stored city would be a bug waiting for that feature.
                timezone = COALESCE(:tz, timezone),
                updated_at = now()
            """
        ),
        {
            "place": chosen.label,
            "lat": chosen.latitude,
            "lon": chosen.longitude,
            "tz": chosen.timezone,
        },
    )
    return _payload(chosen, candidates)


@router.delete("/me/location")
async def clear_location(user: CurrentUser, db: TenantDB) -> dict[str, Any]:
    """Forget it. Suggestions fall back to the placeholder and say so.

    Present because the alternative is a location a user cannot withdraw. It
    also has to actually clear the COORDINATES, not just the label — leaving
    those behind would keep fetching real weather for a place the UI says we
    no longer know.
    """
    await db.execute(
        text(
            "UPDATE user_profile SET home_place = NULL, home_lat_2dp = NULL, "
            "home_lon_2dp = NULL, updated_at = now()"
        )
    )
    return {"place": None, "cleared": True}
