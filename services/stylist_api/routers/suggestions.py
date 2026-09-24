"""GET /suggestions — ranked outfits, zero model calls (Phase 6 exit criterion).

TWO PATHS, AND THE FAST ONE IS THE DEFAULT
------------------------------------------
    materialised  read the nightly precompute. An indexed read; this is what
                  meets the p95 < 300ms budget.
    on demand     generate and score live. Used when the request's context
                  does not match anything precomputed — a different occasion,
                  or weather that moved the warmth target.

The fallback is not a nicety. Precompute cannot cover 18 occasions x 5 warmth
levels x wet/dry per tenant, and a user who picks "interview" on a cold day
must not get an empty screen because nobody precomputed that combination.

WHY THE RESPONSE CARRIES `informative_weight`
---------------------------------------------
Three of the six sub-scores currently have no signal — formality, warmth and
(for an unworn wardrobe) novelty all come from tags that are either mock or
unset. A ranked list with no caveat invites the reader to trust an ordering
that is 30-65% arbitrary. The number is computed per outfit, not asserted here,
so it stops being a caveat on its own when real tags arrive.
"""

from __future__ import annotations

import contextlib
import json
import logging
import uuid
from collections import Counter
from dataclasses import replace
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import text

from stylist_api.deps import (
    CacheRedisDep,
    CurrentUser,
    LiteLLMDep,
    ObjectStoreDep,
    SettingsDep,
    TenantDB,
)
from stylist_clients.google_calendar import CalendarUnavailable, list_public_holidays
from stylist_db.session import tenant_session
from stylist_domain.bandit import Arm, daily_seed, explore_slots, reorder
from stylist_domain.context import resolve_context
from stylist_domain.observances import (
    coverage_until,
    is_stale,
    observance_for,
    occasion_for_holiday_name,
)
from stylist_domain.scoring import load_scoring_config
from stylist_domain.slots import OutfitItem, evaluate
from stylist_domain.taxonomy import load_taxonomy
from stylist_suggest import (
    TEMPLATE_RATIONALE,
    garment_set_hash,
    load_wardrobe,
    rerank,
    suggest,
)
from stylist_suggest.pipeline import RECENTLY_WORN_DAYS, CandidatePool, load_style_vector
from stylist_worker.trends import load_trends

logger = logging.getLogger(__name__)

# How many stored rows to read per outfit shown. See the materialised query.
STORED_OVERFETCH = 3

router = APIRouter(tags=["suggestions"])

# Default when the caller gives no weather. Not a forecast — an honest
# mid-scale placeholder, reported in the response as `weather_source`.
DEFAULT_FEELS_LIKE_C = 26.0

# Used only when the occasion was neither requested NOR derivable from a
# calendar. Named rather than inline so the response can say `default` and
# mean something specific.
DEFAULT_OCCASION = "casual_outing"


async def _persist_live_outfits(user_id: Any, ctx: Any, rows: list[dict[str, Any]]) -> None:
    """Write live-generated outfits to `outfits`, so their hash RESOLVES.

    A HASH THE API HANDS OUT MUST BE A HASH THE API CAN LOOK UP.
    Only `precompute.py` wrote this table, so every outfit produced on the
    live path existed solely inside one response. The response still carried
    `garment_set_hash` — the client needs it for the Try On button — and
    `POST /outfits/{hash}/tryon` then answered 404 "outfit not found", which
    is the one genuine 404 that endpoint has, raised on a hash the same
    service had just issued.

    Measured on this wardrobe: `outfits` held ZERO rows for the owner, because
    the nightly precompute has never had a reason to run for a brand-new
    account. So try-on was not "sometimes broken" — it could never once have
    worked from a live suggestion, which is every suggestion this account has
    ever seen. `GET /outfits/{hash}/board` has the same dependency.

    The same INSERT and the same ON CONFLICT as the precompute, deliberately:
    a second shape for the same row is how the two drift. Re-requesting the
    same context refreshes the score rather than erroring on the unique key.

    Best effort. This is a WRITE on a read path, and an outfit the user can
    see but not try on beats an error page — so a failure here is logged and
    swallowed rather than turned into a 500 on `GET /suggestions`.
    """
    if not rows:
        return
    version = int(load_scoring_config().get("version", 1))
    try:
        async with tenant_session(user_id) as session:
            for r in rows:
                ids = [str(g) for g in r["garment_ids"]]
                await session.execute(
                    text(
                        """
                        INSERT INTO outfits (
                            id, user_id, garment_ids, garment_set_hash, occasion,
                            warmth_target, formality_target, wet, score,
                            score_breakdown, scoring_version
                        ) VALUES (
                            :id, :uid, CAST(:ids AS uuid[]), :hash, :occasion,
                            :warmth, :formality, :wet, :score,
                            CAST(:breakdown AS jsonb), :version
                        )
                        ON CONFLICT (user_id, garment_set_hash, occasion, warmth_target)
                        DO UPDATE SET
                            score = EXCLUDED.score,
                            score_breakdown = EXCLUDED.score_breakdown,
                            scoring_version = EXCLUDED.scoring_version,
                            created_at = now()
                        """
                    ),
                    {
                        "id": uuid.uuid4(),
                        "uid": user_id,
                        "ids": ids,
                        "hash": garment_set_hash(ids),
                        "occasion": ctx.occasion,
                        "warmth": ctx.warmth_target,
                        "formality": ctx.formality_target,
                        "wet": ctx.wet,
                        "score": r["score"],
                        "breakdown": json.dumps(r["score_breakdown"]),
                        "version": version,
                    },
                )
    except Exception:
        logger.exception("could not persist live outfits; try-on will 404 for these")


async def _resolve_weather(
    db: Any, cache: Any, feels_like_c: float | None, precip: float, wind: float
) -> tuple[float, float, float, str]:
    """(feels_like_c, precip_probability, wind_kmh, weather_source).

    THE CALLER'S VALUES WIN. An explicit `?feels_like_c=` is a deliberate
    override — the chat uses it to honour "it's freezing outside" — and a
    forecast for the user's home city must not silently replace what they
    just told us.

    Otherwise: the user's stored city, through the Phase 6 weather client that
    had NO CALLERS until now. `weather_fit` carries 0.15 of the score and
    `monsoon_suitability` is a hard filter, and both ran on the constant 26.0
    for every request ever made.

    Degrades in one direction only, and SAYS WHICH:

      user-stated    the caller passed a temperature
      forecast       real apparent temperature for the stored city
      placeholder    no city stored, or the provider is unreachable

    `weather_source` is returned to the client because the difference matters
    to a reader: "dressed for 26C" is a claim, and the honest version of it is
    either "your city is 31C right now" or "I do not know where you are".
    The module comment has claimed since Phase 6 that this was "reported in
    the response as `weather_source`"; nothing reported it until now.
    """
    if feels_like_c is not None:
        return feels_like_c, precip, wind, "user-stated"

    row = await db.execute(
        text("SELECT home_lat_2dp, home_lon_2dp FROM user_profile LIMIT 1")
    )
    got = row.mappings().one_or_none()
    lat = got["home_lat_2dp"] if got else None
    lon = got["home_lon_2dp"] if got else None
    if lat is None or lon is None:
        return DEFAULT_FEELS_LIKE_C, precip, wind, "placeholder-no-location"

    from stylist_clients.weather import WeatherClient, WeatherUnavailable

    try:
        weather = await WeatherClient(cache=cache).fetch(float(lat), float(lon))
    except WeatherUnavailable as exc:
        # Suggestions are the product; weather is an input. A provider outage
        # degrades the ranking's accuracy, never the response.
        logger.info("weather unavailable, using the placeholder: %s", exc)
        return DEFAULT_FEELS_LIKE_C, precip, wind, "placeholder-provider-down"

    # Caller-supplied precip/wind still win when non-zero — same override rule
    # as the temperature, since the chat sets precip from "it's raining".
    return (
        weather.feels_like_c,
        precip or weather.precip_probability,
        wind or weather.wind_kmh,
        "forecast-cached" if weather.from_cache else "forecast",
    )



async def _occasion_from_global_calendar(
    settings: Any, cache: Any, today: date
) -> tuple[str, str] | None:
    """Today's public holiday, as (occasion, reason). None if there is none.

    GOOGLE FIRST, THE LOCAL TABLE SECOND.
    -------------------------------------
    `config/observances.yaml` can hold fixed dates forever but not Diwali,
    Holi or Eid -- those move every year and a hand-kept list of them expires
    silently. Google's public holiday calendar already carries them and needs
    only an API key, because a national holiday is nobody's private diary.

    CACHED FOR A DAY. Holidays do not change, and without this every
    suggestion request on a festival would call Google. The cache key is the
    date and the calendar id, so changing country does not serve yesterday's
    answer for the wrong place.

    FAILS TO THE TABLE, NOT TO AN ERROR. A missing key, a 403 from a key
    without the Calendar API, or Google being slow all land here; the fixed
    dates still resolve offline, and no suggestion 500s because a holiday
    lookup did.
    """
    api_key = getattr(settings, "google_calendar_api_key", "")
    calendar_id = getattr(settings, "google_holiday_calendar_id", "")
    if api_key and calendar_id:
        cache_key = f"holiday:{calendar_id}:{today.isoformat()}"
        names: list[str] | None = None
        try:
            cached = await cache.get(cache_key)
            if cached is not None:
                names = json.loads(cached)
        except Exception:  # pragma: no cover - cache is an optimisation
            names = None

        if names is None:
            try:
                names = await list_public_holidays(
                    api_key=api_key, calendar_id=calendar_id, day=today
                )
                with contextlib.suppress(Exception):
                    await cache.set(cache_key, json.dumps(names), ex=24 * 3600)
            except CalendarUnavailable as exc:
                logger.warning("global calendar unavailable, using local table: %s", exc)
                names = None

        if names:
            # The first NAMED holiday wins. Google returns observances in no
            # meaningful priority order, and a day with two is rare enough
            # that picking the first recognised one beats inventing a ranking.
            for name in names:
                occasion, recognised = occasion_for_holiday_name(name)
                if recognised:
                    return occasion, f"{name} today, so this is dressed for that."
            first = names[0]
            occasion, _ = occasion_for_holiday_name(first)
            # SAID AS A GUESS, because it is one. An unmapped public holiday
            # may be a festival or may be a long weekend, and stating the
            # second with the confidence of the first is how a product loses
            # trust on the one day the user is paying attention.
            return occasion, (
                f"{first} today. It is a public holiday rather than one this "
                f"app has a dress rule for, so this is a best guess."
            )

    local = observance_for(today)
    if local is not None:
        return local.occasion, f"{local.name} today, so this is dressed for that."
    return None


async def _occasion_from_calendar(
    db: Any, settings: Any, user: Any
) -> tuple[str | None, str | None, int | None]:
    """(occasion, why, events_seen) from today's calendar, or (None, None, None).

    CONFIDENT MATCHES ONLY. `classify` reports `confident` as its own flag
    precisely so a caller does not have to know the threshold, and an
    unconfident classification is a guess — which is what this is here to
    avoid. An unconfident day falls through to the ordinary default rather
    than dressing someone for a meeting the rules were unsure about.

    `why` is the classifier's own explanation, which names the RULE PHRASE it
    matched ("Matched 'client meeting'") and never the event title. That
    distinction is load-bearing: the calendar panel promises the user that
    titles are never sent to this app, and a reason line that quoted their
    diary would break that promise in the one place they would notice.
    """
    from stylist_api.routers.calendar import today as calendar_today

    try:
        result = await calendar_today(user=user, db=db, settings=settings)
    except Exception:
        # No link, revoked token, Google down — none of them are reasons to
        # fail a suggestion request. Fall through to the default.
        logger.info("calendar unavailable for occasion resolution", exc_info=True)
        return None, None, None

    if not result.get("confident") or result.get("is_fallback"):
        return None, None, result.get("events_seen")
    return (
        str(result["occasion"]),
        str(result.get("explanation") or ""),
        result.get("events_seen"),
    )


@router.get("/suggestions")
async def get_suggestions(
    user: CurrentUser,
    db: TenantDB,
    store: ObjectStoreDep,
    settings: SettingsDep,
    gateway: LiteLLMDep,
    cache: CacheRedisDep,
    # OPTIONAL, and omitting it now means "work it out". It used to default
    # silently to `casual_outing`, which is the exact move `resolve_context`
    # refuses to make on the caller's behalf: dressing someone for a casual
    # outing when they never said so is confidently answering a question they
    # did not ask. With a calendar connected there is real evidence to use
    # instead of a guess; without one, the honest default is still casual and
    # the response SAYS which of the two happened.
    occasion: Annotated[str | None, Query()] = None,
    feels_like_c: Annotated[float | None, Query(ge=-30, le=60)] = None,
    precip_probability: Annotated[float, Query(ge=0, le=1)] = 0.0,
    wind_kmh: Annotated[float, Query(ge=0, le=200)] = 0.0,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    force_live: Annotated[bool, Query()] = False,
    # Phase 7. ON by default — the reranker is part of `/suggestions` now, and
    # its exit criterion is a p95 on this endpoint. `rerank=false` preserves
    # the Phase 6 path exactly (deterministic, ZERO model calls), which is what
    # that phase's 7.5ms/58ms numbers were measured on and how they stay
    # re-measurable rather than becoming history.
    rerank_enabled: Annotated[bool, Query(alias="rerank")] = True,
    # NOT query parameters. These come from a CUSTOM OCCASION the user defined
    # (see routers/occasions.py), and the chat passes them through after
    # resolving the alias. Deliberately keyword-only and undocumented in the
    # HTTP surface: letting a client dial formality directly would make every
    # measured ranking number depend on an unaudited request field, where an
    # alias is a stored, listable, deletable object the user created.
    formality_override: int | None = None,
    dress_code_override: str | None = None,
) -> dict[str, Any]:
    taxonomy = load_taxonomy()

    # WHERE THE OCCASION CAME FROM, carried into the response. A user looking
    # at an outfit they did not ask for is entitled to know what it was
    # dressed for and why.
    occasion_source = "requested"
    occasion_reason: str | None = None
    events_seen: int | None = None
    if occasion is None:
        from_calendar, why, events_seen = await _occasion_from_calendar(db, settings, user)
        if from_calendar:
            occasion, occasion_source, occasion_reason = from_calendar, "calendar", why
        else:
            # THE UNIVERSAL CALENDAR, between the personal one and the default.
            #
            # A user with no Google calendar connected was offered an everyday
            # casual look on Diwali. The date is not personal data and needs no
            # integration -- the system already knew it and was not using it.
            #
            # Strictly BELOW the personal calendar: someone with a wedding in
            # their diary on Independence Day is going to the wedding. What is
            # on YOUR day beats what is on everyone's.
            today = date.today()
            from_global = await _occasion_from_global_calendar(settings, cache, today)
            if from_global is not None:
                occasion, occasion_reason = from_global
                occasion_source = "observance"
            else:
                occasion, occasion_source = DEFAULT_OCCASION, "default"
                if is_stale(today):
                    # NOT SILENT. Recurring dates still resolve past the
                    # table's coverage, but moving festivals are simply
                    # unknown -- so "no observance" here means "nobody has
                    # entered this year", not "an ordinary day". Saying so is
                    # the difference between a calendar that stopped working
                    # and one that looks like it is working.
                    occasion_reason = (
                        "The festival calendar has no entries past "
                        f"{coverage_until().isoformat()}, so moving festivals "
                        "such as Diwali cannot be detected for today."
                    )

    if occasion not in {o["id"] for o in taxonomy.raw["occasions"]}:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"unknown occasion; taxonomy defines {sorted(taxonomy.occasions)}",
        )

    feels, precip, wind, weather_source = await _resolve_weather(
        db, cache, feels_like_c, precip_probability, wind_kmh
    )
    ctx = resolve_context(
        occasion=occasion,
        feels_like_c=feels,
        precip_probability=precip,
        wind_kmh=wind,
    )
    # A CUSTOM OCCASION'S NUDGES, applied after the base context is built.
    #
    # `resolve_context` stays the single place that turns an occasion into
    # targets — it is pure, tested, and shared with the precompute — so the
    # override replaces a field on the result rather than adding a branch
    # inside it. "My office is more formal than most" becomes a different
    # `formality_target`, and everything downstream is unchanged.
    if formality_override is not None or dress_code_override is not None:
        ctx = replace(
            ctx,
            formality_target=(
                formality_override if formality_override is not None else ctx.formality_target
            ),
            dress_code_target=(
                dress_code_override
                if dress_code_override is not None
                else ctx.dress_code_target
            ),
        )

    served_from = "materialised"
    rows: list[dict[str, Any]] = []
    # None on the materialised path, which reads precomputed rows and never
    # builds a pool. Declared here so the notes below can be collected without
    # either branch having to remember to.
    pool: CandidatePool | None = None

    if not force_live:
        result = await db.execute(
            text(
                """
                SELECT garment_ids, score, score_breakdown
                FROM outfits
                WHERE occasion = :occasion AND warmth_target = :warmth
                -- THE TIE-BREAK IS NOT COSMETIC. `suggest()` sorts by
                -- (-score, garment_set_hash) precisely so that two outfits
                -- with the same score rank identically across runs; this
                -- read path had only `score DESC`, so Postgres returned an
                -- ARBITRARY set among ties.
                --
                -- On real data scores tie constantly (five outfits at 0.717
                -- here), so the nightly job reranked one arbitrary top-8 and
                -- the request read a different arbitrary top-5. Every
                -- rationale-cache lookup missed, the endpoint fell through to
                -- a live call it cannot afford, and §7.3's "hit rate >= 50%"
                -- would have been unreachable for a reason no dashboard
                -- could show. Phase 6 wrote this rule down in `suggest()`;
                -- the SQL that reads its output did not follow it.
                ORDER BY score DESC, garment_set_hash
                LIMIT :lim
                """
            ),
            # OVER-FETCHED, then trimmed after `_hydrate`. Hydration drops
            # every stored outfit that holds a garment in the wash or just
            # worn, and a favourite shirt is in most of the top rows — so
            # reading exactly `limit` turned "the shirt is in the wash" into
            # one suggestion instead of five.
            {"occasion": occasion, "warmth": ctx.warmth_target, "lim": limit * STORED_OVERFETCH},
        )
        rows = [dict(r) for r in result.mappings()]

    if not rows:
        # Nothing precomputed for this context. Generate live rather than
        # returning an empty list — see the module docstring.
        served_from = "live"
        pool = await load_wardrobe(db, ctx)
        style_vector, style_events = await load_style_vector(db)
        trends = await load_trends(db)
        live = suggest(
            pool,
            ctx,
            limit=limit,
            style_vector=style_vector,
            style_events=style_events,
            trends=trends,
        )
        rows = [
            {
                "garment_ids": [g.garment_id for g in items],
                "score": score.total,
                "score_breakdown": score.breakdown,
            }
            for items, score in live.outfits
        ]
        await _persist_live_outfits(user.id, ctx, rows)
        if not rows:
            return {
                "outfits": [],
                "served_from": served_from,
                "context": _context_payload(ctx, weather_source),
        "occasion_source": occasion_source,
        "occasion_reason": occasion_reason,
        "calendar_events_seen": events_seen,
                # WHY it is empty, not just that it is. "No suggestions" with
                # no reason is the least actionable screen in the product.
                "notes": pool.notes
                or ["no outfit satisfied the slot rules from the wearable pool"],
                "candidates_considered": live.candidates_considered,
            }

    # Hydrate the garments in ONE query rather than per outfit. Ten outfits of
    # four garments is 40 ids and would otherwise be 40 round trips.
    outfits, rows_by_id = await _hydrate(db, store, rows, ctx.dress_code_target)
    # Something stored could not be served today (in the wash, just worn, a
    # `never` rule, a stale dress code) AND what is left is short of what was
    # asked for. Measured on the live stack: with the tee in the wash and the
    # jeans worn, four stored outfits became ONE, while the wardrobe could
    # still make two. Most occasions have only the `limit` rows the last live
    # run persisted — the nightly job covers four occasions — so over-fetching
    # alone cannot refill them. The regenerated outfits are persisted, so a
    # wardrobe with enough wearable combinations is materialised again on the
    # next request; one that genuinely has fewer than `limit` pays the live
    # run each time, which for a wardrobe that small is cheap.
    short_after_filtering = len(outfits) < min(limit, len(rows))
    outfits = outfits[:limit]

    # PREFERENCE FILTERING CAN EMPTY THE MATERIALISED SET, and then the user
    # gets a blank screen for having set a rule. Measured: a `never white`
    # dropped all ten precomputed outfits, because the hydration predicate
    # removes the garment and the loop then skips the whole outfit.
    #
    # This is what the live path already exists for — "a user who picks
    # interview on a cold day must not get an empty screen" — it was simply
    # checked before filtering rather than after. Regenerating honours the
    # rules at generation time, so the result is non-empty AND obeys them.
    if served_from == "materialised" and (not outfits or short_after_filtering):
        served_from = "live"
        pool = await load_wardrobe(db, ctx)
        style_vector, style_events = await load_style_vector(db)
        trends = await load_trends(db)
        live = suggest(
            pool,
            ctx,
            limit=limit,
            style_vector=style_vector,
            style_events=style_events,
            trends=trends,
        )
        rows = [
            {
                "garment_ids": [g.garment_id for g in items],
                "score": score.total,
                "score_breakdown": score.breakdown,
            }
            for items, score in live.outfits
        ]
        await _persist_live_outfits(user.id, ctx, rows)
        outfits, rows_by_id = await _hydrate(db, store, rows, ctx.dress_code_target)
        if not outfits:
            return {
                "outfits": [],
                "served_from": served_from,
                "context": _context_payload(ctx, weather_source),
        "occasion_source": occasion_source,
        "occasion_reason": occasion_reason,
        "calendar_events_seen": events_seen,
                "notes": pool.notes
                or ["no outfit satisfied your preferences and today's slot rules"],
            }

    ranking_source = "deterministic"
    reject_rule: str | None = None
    # POOL NOTES SURVIVE A SUCCESSFUL RESPONSE.
    #
    # They were returned only from the two EMPTY branches, so "no wearable
    # feet — these outfits are shown without one" was dropped in exactly the
    # case it describes: outfits exist, and the user is looking at shoeless
    # ones with no explanation. `pool` is bound only on the live path (the
    # materialised path reads precomputed rows and never builds one), so this
    # collects what there is rather than assuming.
    pool_notes: list[str] = list(pool.notes) if pool is not None else []
    rerank_notes: list[str] = []

    if rerank_enabled and outfits:
        # WHAT LEAVES THE BUILDING, decided here and not in the prompt builder.
        # Tags only — no ids beyond the opaque uuid, no colours-of-a-person, no
        # image. Keeping this projection at the call site is deliberate: it is
        # a privacy decision, and a privacy decision buried three modules down
        # is one nobody reviews.
        items_by_id = {
            gid: {
                "slot": r["slot"],
                "subcategory": r["subcategory"],
                "colour": r["primary_colour"],
                "material": r["material"],
                "formality": r["formality"],
                "warmth": r["warmth"],
            }
            for gid, r in rows_by_id.items()
        }
        order_in = [tuple(str(g) for g in o["_ids"]) for o in outfits]

        # The tenant's own virtual key, never the master key — the reranker is
        # a paid call and must land on the budget that belongs to whoever asked
        # for it (§B3).
        key_row = await db.execute(text("SELECT litellm_key FROM user_profile LIMIT 1"))
        tenant_key = key_row.scalar()

        outcome = await rerank(
            order_in,
            ctx,
            items_by_id=items_by_id,
            active_ids=set(rows_by_id),
            slots_by_id={gid: (r["slot"], r["subcategory"]) for gid, r in rows_by_id.items()},
            gateway=gateway if tenant_key else None,
            api_key=tenant_key,
            model=settings.rerank_model,
            cache=cache,
            min_confidence=settings.rerank_min_confidence,
        )

        by_ids = {tuple(str(g) for g in o["_ids"]): o for o in outfits}
        outfits = [by_ids[ids] for ids in outcome.order if ids in by_ids]
        for o in outfits:
            o["rationale"] = outcome.rationales.get(
                tuple(str(g) for g in o["_ids"]), TEMPLATE_RATIONALE
            )
        ranking_source = outcome.source
        reject_rule = outcome.reject_rule
        rerank_notes = outcome.notes
    else:
        for o in outfits:
            o["rationale"] = TEMPLATE_RATIONALE

    # THE BANDIT ORDERS THE FINAL LIST (Phase 11), after the scorer and after
    # the reranker. Last, because it is the only step that deliberately shows
    # something other than the best-predicted outfit, and doing that before the
    # reranker would just let the reranker undo it.
    #
    # Seeded from (user, today): identical within a day, so pulling to refresh
    # does not reshuffle and the precompute and request paths agree; different
    # across days, so exploration actually happens.
    explored = 0
    if outfits:
        arms = await _load_bandit_arms(db)
        ranked = [(str(i), o.get("dress_code")) for i, o in enumerate(outfits)]
        order = reorder(ranked, arms, seed=daily_seed(str(user.id), date.today()))
        reordered = [outfits[int(i)] for i in order]
        # WHAT THE SCORER THOUGHT, kept alongside what is actually shown.
        #
        # `reorder` is allowed to promote a worse-predicted outfit — that is
        # the whole point of exploration. Labelling the top card "1st choice"
        # while the scorer ranked it 5th would be a small lie, and the UI can
        # only be honest about it if the pre-bandit position survives the
        # permutation. `order[k]` is the original index of the item now at k.
        for position, original in enumerate(order):
            reordered[position]["predicted_rank"] = int(original) + 1
        # THE COUNT IS THE EXPLORE BUDGET, not the number of positions that
        # moved. Promoting one outfit shifts every outfit after it, so a
        # positional diff reported 7 of 12 "explored" for a 2-slot budget —
        # a number that looks like a finding and is an artefact of list
        # surgery. `explore_slots` is what the bandit actually chose.
        explored = explore_slots(len(reordered))
        outfits = reordered
        if explored:
            ranking_source = f"{ranking_source}+bandit"

    # RANK IS STATED, NOT INFERRED FROM ARRAY POSITION.
    #
    # The list has always been ordered — scorer, then reranker, then bandit —
    # but nothing said so, and every screen rendered the cards as an unlabelled
    # grid. "Which of these do you actually recommend?" was unanswerable from
    # the UI even though the server had a definite answer.
    #
    # An explicit field rather than leaving the client to count: a client that
    # filters or re-sorts (the Saved screen does) would otherwise renumber the
    # ranking into nonsense, and the server is the only thing that knows the
    # real order.
    for position, o in enumerate(outfits):
        o["rank"] = position + 1
        # Absent when the bandit did not run (no outfits, or it was skipped),
        # and then the predicted order IS the shown order.
        o.setdefault("predicted_rank", position + 1)
        o.pop("_ids", None)
        o.pop("dress_code", None)

    return {
        "outfits": outfits,
        "served_from": served_from,
        # How many slots the bandit moved off the predicted-best order. Stated
        # rather than hidden: a user asking "why is this odd one at number 4"
        # deserves an answer that exists in the response.
        "explored_slots": explored,
        # HOW the list was ordered, surfaced rather than logged. A ranking that
        # silently switches between a model and a scorer is unexplainable when
        # someone asks why this morning's suggestion is different.
        "ranking_source": ranking_source,
        "validator_reject_rule": reject_rule,
        "context": _context_payload(ctx, weather_source),
        "occasion_source": occasion_source,
        "occasion_reason": occasion_reason,
        "calendar_events_seen": events_seen,
        # Pool notes first: "these outfits have no shoes" is about the
        # OUTFITS, where a reranker note is about how they were ordered.
        "notes": [*pool_notes, *rerank_notes],
    }


async def _load_bandit_arms(db: Any) -> dict[str, Arm]:
    """This tenant's Thompson posteriors, keyed by arm.

    An arm absent from the table is NOT absent from the bandit — `reorder`
    substitutes `Arm(key)` with a uniform Beta(1,1) prior, which is the honest
    posterior for a kind nobody has reacted to and is what makes a cold start
    explore instead of settling on whichever arm happens to exist.
    """
    rows = await db.execute(text("SELECT arm_key, successes, failures FROM bandit_arm"))
    return {
        r["arm_key"]: Arm(r["arm_key"], int(r["successes"]), int(r["failures"]))
        for r in rows.mappings()
    }


async def _hydrate(
    db: Any, store: Any, rows: list[dict[str, Any]], dress_code_target: str | None = None
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Rows -> renderable outfits, in one query.

    The `never` predicate lives here rather than in the caller so BOTH the
    materialised and the live path go through it. `load_wardrobe` already
    filters the live pool, but routing both through one hydration keeps the two
    from drifting — a rule enforced on one path and not the other is worse than
    one enforced on neither, because it looks like it works.
    """
    if not rows:
        return [], {}

    # The dress codes this occasion accepts, asymmetric exactly as the pool
    # query uses them: `casual` admits smart_casual and activewear, while
    # `activewear` admits only itself.
    compatible: set[str] | None = None
    if dress_code_target:
        taxonomy = load_taxonomy()
        compatible = set(
            taxonomy.dress_code_compatibility.get(dress_code_target, (dress_code_target,))
        )
        compatible.add(dress_code_target)

    all_ids = sorted({gid for r in rows for gid in r["garment_ids"]})
    detail = await db.execute(
        text(
            """
            SELECT id, slot::text AS slot, subcategory::text AS subcategory,
                   primary_colour::text AS primary_colour, material::text AS material,
                   -- `dress_code` is the BANDIT ARM. Without it every outfit
                   -- lands in the `unknown` arm and the bandit learns one
                   -- meaningless posterior while looking like it works.
                   dress_code::text AS dress_code,
                   formality, warmth, cutout_key, needs_review, needs_wash
            FROM garments g
            WHERE g.id = ANY(CAST(:ids AS uuid[])) AND g.is_active
              -- NOT IN TODAY'S WARDROBE: in the wash, or worn within the
              -- rest window. The SAME two rules as `POOL_SQL`, and they have
              -- to be here as well, because a stored outfit is a claim about
              -- the garments as they were when it was built.
              --
              -- MEASURED (tests/test_wash_and_wear.py): a shirt put in the
              -- basket, jeans worn this morning, and shoes worn yesterday
              -- were all still served from `materialised` — both rules held
              -- only on the live path, and nothing re-checked a stored row
              -- until the nightly precompute rebuilt it.
              AND NOT g.needs_wash
              AND NOT EXISTS (
                SELECT 1 FROM wear_log w
                WHERE w.garment_id = g.id
                  AND w.worn_on >= CURRENT_DATE - make_interval(days => :recent_days)
              )
              AND NOT EXISTS (
                SELECT 1 FROM preference_fact pf
                WHERE pf.kind = 'never'
                  AND (
                       (pf.field_name = 'subcategory' AND pf.field_value = g.subcategory::text)
                    OR (pf.field_name = 'primary_colour'
                        AND pf.field_value = g.primary_colour::text)
                    OR (pf.field_name = 'material' AND pf.field_value = g.material::text)
                    OR (pf.field_name = 'fit'      AND pf.field_value = g.fit::text)
                    OR (pf.field_name = 'pattern'  AND pf.field_value = g.pattern::text)
                  )
              )
            """
        ),
        {"ids": [str(i) for i in all_ids], "recent_days": RECENTLY_WORN_DAYS},
    )
    rows_by_id = {str(r["id"]): dict(r) for r in detail.mappings()}
    garments = {
        gid: {
            "id": gid,
            "slot": r["slot"],
            "subcategory": r["subcategory"],
            "primary_colour": r["primary_colour"],
            "needs_review": r["needs_review"],
            "needs_wash": r["needs_wash"],
            "cutout_url": store.presign_download(r["cutout_key"]) if r["cutout_key"] else None,
        }
        for gid, r in rows_by_id.items()
    }

    outfits: list[dict[str, Any]] = []
    for r in rows:
        breakdown = r["score_breakdown"]
        if isinstance(breakdown, str):
            breakdown = json.loads(breakdown)
        items = [garments[str(g)] for g in r["garment_ids"] if str(g) in garments]
        if len(items) != len(r["garment_ids"]):
            # A garment that is gone, one a `never` rule just removed, or one
            # that is in the wash or was just worn. Every case means the
            # outfit cannot be worn today, so it is skipped rather than shown
            # with a gap.
            continue

        # RE-CHECK THE STRUCTURE RULES AGAINST TODAY'S TAGS.
        #
        # A stored outfit is a claim about garments AS THEY WERE TAGGED when
        # it was materialised, and that claim can stop being true underneath
        # it: correcting a slot, or changing the rules themselves, leaves rows
        # the generator would never produce now. Two real examples, both
        # served as FIRST CHOICE from one precompute run:
        #
        #   ['feet','lower','lower']   two pairs of jeans, no top
        #   ['feet','lower']           jeans and shoes, no top at all
        #
        # Pruning on correction (routers/corrections.py) stops NEW ones
        # appearing. This stops OLD ones being served, and costs one in-memory
        # check over at most `limit` outfits. The live path comes through here
        # too, so the rule is enforced once for both rather than twice and
        # differently -- a rule enforced on one path only is worse than on
        # neither, because it looks like it works.
        verdict = evaluate(
            [
                OutfitItem(garment_id=i["id"], slot=i["slot"], subcategory=i["subcategory"])
                for i in items
            ]
        )
        if not verdict.valid:
            logger.warning(
                "dropping stored outfit %s: %s",
                r.get("garment_set_hash"),
                ", ".join(verdict.violations),
            )
            continue

        # RE-CHECK THE DRESS CODE TOO, for the same reason and in the same
        # place. The structure check above asks "is this still an outfit?";
        # it never asked "is it still an outfit FOR THIS OCCASION?".
        #
        # MEASURED: "something for the gym" returned denim shirts, jeans and
        # sneakers as the top four looks. The rows were materialised on
        # 2026-09-21 under `occasion = 'workout'` from garments that are
        # tagged `casual` today, and `activewear` accepts only `activewear`.
        # Re-running `load_wardrobe` for that user and occasion now yields an
        # EMPTY pool — the live path was right and the stored rows outlived
        # the tags they were built from.
        #
        # A garment with NO dress code still passes, matching `POOL_SQL`
        # exactly: the two paths have to agree about an untagged garment, or
        # this becomes another rule enforced on one path and not the other.
        if compatible is not None:
            wrong = sorted(
                {
                    code
                    for gid in (str(g) for g in r["garment_ids"])
                    if (code := rows_by_id.get(gid, {}).get("dress_code"))
                    and code not in compatible
                }
            )
            if wrong:
                logger.warning(
                    "dropping stored outfit %s: dress code %s does not suit %s",
                    r.get("garment_set_hash"),
                    ", ".join(wrong),
                    dress_code_target,
                )
                continue
        # THE OUTFIT'S DRESS CODE = the most common among its garments, which
        # is the bandit's arm. Ties break on the value itself, not on row
        # order, so an arm cannot change because the query plan did. Stripped
        # from the response before it is returned — it is an internal key, not
        # a field the client asked for.
        codes = Counter(
            rows_by_id[gid]["dress_code"]
            for gid in (str(g) for g in r["garment_ids"])
            if rows_by_id.get(gid, {}).get("dress_code")
        )
        outfits.append(
            {
                "_ids": [str(g) for g in r["garment_ids"]],
                # THE OUTFIT'S IDENTITY, and the UI cannot request a try-on or
                # a board without it. Omitting it made the Try On button a
                # dead control: it fell through to "this look has no saved id
                # yet" for every outfit, on every screen.
                #
                # Recomputed here rather than selected: the live path builds
                # outfits that are not in the `outfits` table at all, so there
                # is no column to read. `garment_set_hash` is over the SORTED
                # ids, so both paths produce the same value for the same set.
                "garment_set_hash": garment_set_hash([str(g) for g in r["garment_ids"]]),
                "garments": items,
                "score": float(r["score"]),
                "informative_weight": (breakdown or {}).get("informative_weight"),
                "score_breakdown": breakdown,
                "dress_code": (min(sorted(codes), key=lambda c: (-codes[c], c)) if codes else None),
            }
        )
    return outfits, rows_by_id


def _context_payload(ctx: Any, weather_source: str | None = None) -> dict[str, Any]:
    return {
        "occasion": ctx.occasion,
        # WHERE THE TEMPERATURE CAME FROM. Three returns in this module build a
        # context payload, so the parameter lives here rather than being added
        # at each call site — the empty-wardrobe replies are exactly the ones a
        # user is most likely to question, and "dressed for 26C" with no
        # provenance is the claim they cannot check.
        "weather_source": weather_source,
        "warmth_target": ctx.warmth_target,
        "formality_target": ctx.formality_target,
        "dress_code_target": ctx.dress_code_target,
        "wet": ctx.wet,
        "wind_adjusted": ctx.wind_adjusted,
        "feels_like_c": ctx.feels_like_c,
    }
