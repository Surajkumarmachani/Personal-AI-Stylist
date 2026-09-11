"""Step 6.1 — resolve (weather, occasion) into outfit targets.

A PURE FUNCTION, AND THAT IS THE POINT
--------------------------------------
Everything downstream — candidate filtering, the scorer, the nightly
precompute — keys off these three targets. If they are wrong, every outfit is
wrong, and a bug here is invisible because the output is still a plausible
number. So this module takes primitives, returns a dataclass, touches no
network and no database, and is exhaustively unit-tested.

EVERY THRESHOLD COMES FROM taxonomy.yaml
----------------------------------------
`warmth` carries `feels_like_c_max` per level (34/30/26/21/16) and `occasions`
carries `dress_code` + `formality_target` for all 18. Nothing here is invented,
so tuning the product means editing the taxonomy rather than editing code — and
`scripts/validate_taxonomy.py` already guards that file's internal consistency.

WHY `feels_like` AND NOT `temperature`
--------------------------------------
30°C at 90% humidity and 30°C at 20% humidity call for different clothes, and
Indian wardrobes span both within one city. Open-Meteo returns
`apparent_temperature`, which folds in humidity and wind, so the same
temperature reading can legitimately produce different targets. Using the dry
bulb reading would make the monsoon recommendations wrong in the one market
this taxonomy was built for.
"""

from __future__ import annotations

from dataclasses import dataclass

from stylist_domain.taxonomy import load_taxonomy

# Rain changes what is wearable more than it changes how warm you need to be:
# suede and silk are out, a bag matters, and a light layer beats a heavy one.
# Above this, monsoon suitability becomes a hard filter rather than a
# preference.
PRECIP_PROBABILITY_WET = 0.4

# Wind makes a given temperature feel colder than apparent_temperature alone
# captures for layered outfits, because a layer that is open flaps. One extra
# warmth step, not two — this is a nudge, not a reclassification.
WIND_LAYER_KMH = 25.0


@dataclass(frozen=True)
class OutfitContext:
    """What the candidate generator and scorer are aiming at."""

    warmth_target: int
    formality_target: int
    dress_code_target: str
    # True when the outfit must survive rain. Drives a HARD filter on material
    # via taxonomy's monsoon_suitability, not a soft score.
    wet: bool
    # Set when wind pushed the warmth target up, so `score_breakdown` can
    # explain a heavier outfit than the temperature alone implies.
    wind_adjusted: bool
    occasion: str
    feels_like_c: float


def warmth_for_feels_like(feels_like_c: float) -> int:
    """The heaviest warmth level still appropriate at this apparent temperature.

    `feels_like_c_max` reads as "wear this level up to here", so the answer is
    the HIGHEST level whose ceiling is still at or above the reading: at 20°C
    both `moderate` (26) and `warm` (21) qualify and `warm` is right, because
    picking the lowest qualifying level would leave you under-dressed on every
    cold day.

    Above the hottest level's ceiling there is nothing lighter, so it clamps
    to 1 rather than returning nothing.
    """
    levels = load_taxonomy().raw["warmth"]
    qualifying = [
        int(level["value"]) for level in levels if float(level["feels_like_c_max"]) >= feels_like_c
    ]
    return max(qualifying) if qualifying else 1


def resolve_context(
    *,
    occasion: str,
    feels_like_c: float,
    precip_probability: float = 0.0,
    wind_kmh: float = 0.0,
) -> OutfitContext:
    """Turn conditions and an intent into targets.

    `occasion` is REQUIRED and has no default. The same weather calls for very
    different clothes depending on whether you are interviewing or going to the
    gym, and a default here would silently pick one — producing confident
    suggestions for an occasion the user never named.
    """
    taxonomy = load_taxonomy()
    occasions = {o["id"]: o for o in taxonomy.raw["occasions"]}
    if occasion not in occasions:
        raise ValueError(f"unknown occasion {occasion!r}; taxonomy defines {sorted(occasions)}")
    spec = occasions[occasion]

    warmth = warmth_for_feels_like(feels_like_c)
    wind_adjusted = False
    if wind_kmh >= WIND_LAYER_KMH and warmth < 5:
        warmth += 1
        wind_adjusted = True

    return OutfitContext(
        warmth_target=warmth,
        formality_target=int(spec["formality_target"]),
        dress_code_target=str(spec["dress_code"]),
        wet=precip_probability >= PRECIP_PROBABILITY_WET,
        wind_adjusted=wind_adjusted,
        occasion=occasion,
        feels_like_c=feels_like_c,
    )
