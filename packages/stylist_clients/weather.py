"""Open-Meteo client (Step 6.1).

COORDINATES ROUNDED TO 2dp, AND THAT IS A PRIVACY DECISION AS WELL AS A CACHE ONE
---------------------------------------------------------------------------------
2dp is ~1.1km. Two things follow, and both matter:

  - The cache actually hits. At full precision every request is a unique key,
    so a user who reopens the app three times makes three upstream calls and
    the 1h TTL never does anything.
  - We stop sending a third party a location precise enough to identify a
    building. The weather 1.1km away is identical; the privacy difference is
    not.

ONE CALL FOR CURRENT AND FORECAST
---------------------------------
`/v1/forecast` returns `current` and `hourly`/`daily` together. The plan is
explicit about why: "?date=tomorrow needs tomorrow's forecast, and a 07:00
push needs the day ahead, not the reading at 07:00". Fetching current
conditions from one endpoint and the forecast from another doubles the calls
and lets the two disagree — you can get a "current" reading from a different
model run than the forecast you pair it with.

apparent_temperature, NOT temperature_2m
----------------------------------------
`resolve_context` takes `feels_like_c` because 30°C at 90% humidity and 30°C
at 20% call for different clothes, and this product's market spans both within
one city. Open-Meteo computes apparent temperature from humidity, wind and
radiation, so asking for the dry-bulb reading here would quietly undo that.

NO API KEY
----------
Open-Meteo's free tier needs no credential for non-commercial use. That keeps
weather off the DPA critical path entirely — worth noting because every other
external call in this system is blocked on it.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BASE_URL = "https://api.open-meteo.com/v1/forecast"

# ~1.1km. See the module docstring: this is the cache key and the privacy
# boundary at once.
COORD_PRECISION = 2

CACHE_TTL_SECONDS = 3600
CACHE_PREFIX = "weather:"

# Short. Weather is an INPUT to suggestions, not the product: if it is slow we
# degrade to a seasonal default rather than making the user wait.
REQUEST_TIMEOUT = 4.0


class WeatherUnavailable(Exception):  # noqa: N818 - a state, not an error type
    """Upstream is unreachable or malformed. Callers degrade, never fail."""


@dataclass(frozen=True)
class Weather:
    feels_like_c: float
    precip_probability: float
    wind_kmh: float
    # Tomorrow's values, for the 07:00 push and `?date=tomorrow`.
    tomorrow_feels_like_c: float | None
    tomorrow_precip_probability: float | None
    latitude: float
    longitude: float
    from_cache: bool = False


def _round(value: float) -> float:
    return round(float(value), COORD_PRECISION)


def _cache_key(lat: float, lon: float) -> str:
    return f"{CACHE_PREFIX}{_round(lat)}:{_round(lon)}"


def _parse(payload: dict[str, Any], lat: float, lon: float, *, from_cache: bool) -> Weather:
    current = payload.get("current") or {}
    daily = payload.get("daily") or {}

    def day(field: str, index: int) -> float | None:
        values = daily.get(field) or []
        if len(values) > index and values[index] is not None:
            return float(values[index])
        return None

    feels = current.get("apparent_temperature")
    if feels is None:
        # A 200 with no apparent temperature is unusable. Returning 0.0 would
        # silently recommend an overcoat in Chennai.
        raise WeatherUnavailable("response carried no apparent_temperature")

    return Weather(
        feels_like_c=float(feels),
        # Open-Meteo reports probability as a percentage; the domain uses 0-1.
        precip_probability=float(current.get("precipitation_probability") or 0.0) / 100.0,
        wind_kmh=float(current.get("wind_speed_10m") or 0.0),
        tomorrow_feels_like_c=day("apparent_temperature_max", 1),
        tomorrow_precip_probability=(
            p / 100.0 if (p := day("precipitation_probability_max", 1)) is not None else None
        ),
        latitude=_round(lat),
        longitude=_round(lon),
        from_cache=from_cache,
    )


class WeatherClient:
    def __init__(self, cache: Any | None = None) -> None:
        # The cache is optional so the domain tests and a bare script can use
        # this without a Redis.
        self._cache = cache

    async def fetch(self, latitude: float, longitude: float) -> Weather:
        lat, lon = _round(latitude), _round(longitude)
        key = _cache_key(lat, lon)

        if self._cache is not None:
            try:
                cached = await self._cache.get(key)
            except Exception as exc:  # pragma: no cover - cache is best-effort
                logger.debug("weather cache read failed: %s", exc)
                cached = None
            if cached:
                try:
                    return _parse(json.loads(cached), lat, lon, from_cache=True)
                except Exception:
                    # A malformed cache entry must not be a permanent outage;
                    # fall through and refetch.
                    logger.warning("discarding unparseable cached weather for %s", key)

        params: dict[str, str | int | float] = {
            "latitude": lat,
            "longitude": lon,
            "current": "apparent_temperature,precipitation_probability,wind_speed_10m",
            "daily": "apparent_temperature_max,precipitation_probability_max",
            "timezone": "auto",
            "forecast_days": 2,
        }
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT) as client:
                resp = await client.get(BASE_URL, params=params)
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            raise WeatherUnavailable(f"{type(exc).__name__}: {exc}") from exc

        weather = _parse(payload, lat, lon, from_cache=False)
        if self._cache is not None:
            try:
                await self._cache.set(key, json.dumps(payload), ttl_seconds=CACHE_TTL_SECONDS)
            except Exception as exc:  # pragma: no cover
                logger.debug("weather cache write failed: %s", exc)
        return weather
