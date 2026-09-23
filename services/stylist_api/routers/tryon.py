"""Try-on: consent, body photos, render, and degrading to a board (Phase 10).

WHAT IS BUILT HERE
------------------
Phase 10 opens with "benchmark before you build": a 10-body x 16-garment grid
including sarees, kurtas and a sherwani, because "the published benchmark used
Western garments; your routing table must come from your own grid".

An earlier pass read that as a reason to build nothing, which was wrong. The
grid gates the ROUTING TABLE — which model for which category — and there is no
way to run the grid without a working render path, so the render path is the
grid's prerequisite rather than its competitor. What is built is therefore ONE
provider, no fallback chain and no per-category selection; those are the parts
that genuinely need the data. See `stylist_clients.vton_client`.

A BODY PHOTOGRAPH IS NOT A PHOTOGRAPH OF A SHIRT
-------------------------------------------------
It identifies a person, it is the most sensitive thing this system stores, and
under try-on it LEAVES OUR INFRASTRUCTURE for a third-party generative model.
§C5 treats it accordingly: a separate consent record, timestamped and
INDEPENDENTLY revocable, because withdrawing consent for one feature must not
cost you your account.

So consent here is an explicit flag on the request, not an implication of
having uploaded. "They uploaded it, so they must have agreed" is the reasoning
that makes consent meaningless. And the notice names the third party, because
consent to storage is not consent to transmission.

THE RENDER IS ASYNCHRONOUS, BECAUSE IT HAS TO BE
-------------------------------------------------
30-120s on shared hardware, queued behind other users' traffic. Holding the
request open would occupy a uvicorn worker for minutes per outfit and break
every latency SLO in §B1. So this endpoint enqueues and answers with the board;
the render lands in object storage and the next request serves it. Exactly the
board's own pattern — render once, key by the outfit's identity, serve a URL.

THE DEGRADE IS THE FEATURE, NOT THE FALLBACK
----------------------------------------------
Phase 10's exit criterion is "try-on works or degrades to a board, NEVER
errors". A board is pixel-accurate to clothes the user owns; a try-on render is
a guess about how they would look. When the guess is unavailable — no consent,
no provider, a failed render, quota gone, or simply not finished yet — the
honest answer is the accurate picture, not an error page. That is why this
endpoint returns 200 with `rendered: false` rather than a 4xx for every one of
those cases.
"""

from __future__ import annotations

import json
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import text

from stylist_api.deps import CurrentUser, ObjectStoreDep, SettingsDep, TenantDB
from stylist_api.routers.boards import board_key
from stylist_db.outbox import emit
from stylist_db.session import tenant_session

router = APIRouter(tags=["tryon"])

BODY_PREFIX = "body"


def tryon_key(user_id: uuid.UUID | str, garment_set_hash: str) -> str:
    """Its OWN prefix, not `boards/`.

    A board is a picture of clothes; this is a picture of a person wearing
    them. They must not be indistinguishable to a prefix operation — erasure,
    consent revocation and retention all need to reach one without the other.
    """
    return f"tryon/{user_id}/{garment_set_hash}.png"


# Per-tenant cap, per §B3's "quotas at the gateway". A render is the most
# expensive call in the product and the one with the least bounded output, so
# the ceiling is deliberately low and deliberately per-DAY: a bug that retries
# costs one day's quota, not a month's.
TRYON_DAILY_QUOTA = 10

# Long, like a board's: the key names an exact garment set, so the object is
# immutable for its key and a short TTL would protect against nothing.
TRYON_URL_TTL_SECONDS = 24 * 3600

# How long a request stays "in flight" before another one may be enqueued.
# Generous, because a render is 30-120s on shared hardware and the queue is
# other people's traffic -- but FINITE, so a worker that dies mid-render does
# not leave an outfit stuck at "in progress" with no way to retry it.
RENDER_INFLIGHT_TTL_S = 15 * 60

# Named in the consent notice. Storing a body photo and transmitting it to a
# third party are different things to agree to, and the second one is the one
# people care about.
_PROVIDER_NOTICE = {
    "leffa": "Leffa, hosted on Hugging Face",
    "idm-vton": "IDM-VTON, hosted on Hugging Face",
    "ootdiffusion": "OOTDiffusion, hosted on Hugging Face",
    # A self-hosted tunnel (Colab/Kaggle) rather than a public Space, so the
    # destination is the operator's own machine. Still NAMED: the point of this
    # map is that a body photo is never transmitted somewhere the notice does
    # not mention, and "somewhere you set up yourself" is still somewhere.
    "leffa-dc": "Leffa, running on the notebook GPU configured for this deployment",
    "leffa-hd": "Leffa, running on the notebook GPU configured for this deployment",
    "leffa-colab": "Leffa, running on the notebook GPU configured for this deployment",
}


class ConsentRequest(BaseModel):
    upload_id: uuid.UUID
    key: str = Field(max_length=512)
    # EXPLICIT. Not inferred from the upload having happened — "they uploaded
    # it, so they must have agreed" is the reasoning that makes consent a
    # formality. The client has to say the word.
    consent_to_virtual_tryon: bool


@router.post("/me/body-photos/presign")
async def presign_body_photo(
    user: CurrentUser, store: ObjectStoreDep, settings: SettingsDep
) -> dict[str, Any]:
    """A presigned upload under the `body/` prefix.

    Its own prefix, not `originals/`, so consent, revocation and erasure can
    each target body photos without touching the wardrobe — and so that a
    photograph of a person is never indistinguishable from a photograph of a
    shirt to a prefix operation.
    """
    presigned = store.presign_upload(user_id=user.id, content_type="image/jpeg", prefix=BODY_PREFIX)
    provider = getattr(settings, "vton_provider", "")
    third_party = _PROVIDER_NOTICE.get(provider)
    return {
        "upload_id": presigned.upload_id,
        "key": presigned.key,
        "url": presigned.url,
        "fields": presigned.fields,
        "expires_at": presigned.expires_at,
        # Stated at the point of upload, not buried in a settings screen — and
        # it NAMES the third party when there is one. Consent to storage is not
        # consent to transmission, and a notice that omits where the photo goes
        # is asking for agreement to the wrong question.
        "notice": (
            "This photo is used only to render try-on images of your own clothes. "
            + (
                f"To do that it is sent to {third_party}, outside our infrastructure. "
                if third_party
                else "It is not sent anywhere outside our infrastructure today. "
            )
            + "You can revoke it at any time with DELETE /me/body-photos, which "
            "deletes it without affecting your account."
        ),
        "sent_to_third_party": third_party,
    }


@router.post("/me/body-photos", status_code=status.HTTP_201_CREATED)
async def record_body_photo(body: ConsentRequest, user: CurrentUser) -> dict[str, Any]:
    """Record the photo AND the consent, together.

    One row, one transaction: a body photo without a consent record is a photo
    nobody agreed to, and storing it first "to attach consent later" is how
    that happens.
    """
    if not body.consent_to_virtual_tryon:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="explicit consent is required to store a body photo for try-on",
        )
    # The key must be one WE issued, under this tenant's own body prefix.
    # Without this a caller could attach consent to any object in the bucket,
    # including another tenant's.
    expected = f"{BODY_PREFIX}/{user.id}/"
    if not body.key.startswith(expected):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="key is not a body-photo upload for this account",
        )

    photo_id = uuid.uuid4()
    async with tenant_session(user.id) as db:
        await db.execute(
            text("INSERT INTO body_photo (id, user_id, object_key) VALUES (:i, :u, :k)"),
            {"i": photo_id, "u": user.id, "k": body.key},
        )
    return {
        "body_photo_id": str(photo_id),
        "consented": True,
        "revoke_with": "DELETE /me/body-photos",
    }


@router.get("/me/body-photos")
async def list_body_photos(user: CurrentUser, db: TenantDB) -> dict[str, Any]:
    """What we hold and its consent state. The OBJECT KEY IS NOT RETURNED —
    it is a pointer to the most sensitive object this system stores, and the
    client has no use for it that a presigned URL does not serve better."""
    rows = await db.execute(
        text(
            "SELECT id, consented_at, revoked_at, deleted_from_storage "
            "FROM body_photo ORDER BY consented_at DESC"
        )
    )
    photos = [dict(r) for r in rows.mappings()]
    return {
        "photos": photos,
        "active": sum(1 for p in photos if p["revoked_at"] is None),
    }


@router.post("/outfits/{garment_set_hash}/tryon")
async def tryon(
    garment_set_hash: str,
    user: CurrentUser,
    db: TenantDB,
    store: ObjectStoreDep,
    settings: SettingsDep,
    force_board: Annotated[bool, Query()] = False,
) -> dict[str, Any]:
    """Try-on for one outfit, or the board. ALWAYS 200.

    Phase 10's exit criterion is "works or degrades to a board, never errors",
    so every reason a render is not available returns the board with a stated
    reason rather than a 4xx:

      no consent          the user has not opted in, or revoked
      not configured      no VTON provider on this deployment
      quota exhausted     §B3 — a downgrade, not an outage
      queued              enqueued, not finished; poll again
      render failed       the provider erred or returned nothing usable

    A board is pixel-accurate to clothes the user owns. A render is a guess
    about how they would look. When the guess is unavailable the accurate
    picture is the better answer, not an error page — and that ordering is why
    the board is the DEFAULT visualisation and try-on the enhancement.
    """
    outfit = await db.execute(
        text("SELECT garment_set_hash FROM outfits WHERE garment_set_hash = :h LIMIT 1"),
        {"h": garment_set_hash},
    )
    if outfit.scalar_one_or_none() is None:
        # The ONE genuine 404: we cannot render or board an outfit that does
        # not exist, and pretending otherwise would hide a client bug.
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="outfit not found")

    rendered_key = tryon_key(user.id, garment_set_hash)
    if not force_board and store.head(rendered_key) is not None:
        # The render finished. Serve it — and NOT the board, because the whole
        # point of the feature is the picture of them in it.
        # WHICH SLOTS THE RENDER COVERED. No provider does footwear, so the
        # shoes in the image are the user's own from their body photo -- not
        # the ones in this outfit. Returned so the client can say so, instead
        # of leaving the user to spot the difference and mistrust the render.
        covered = await db.execute(
            text(
                """
                SELECT detail ->> 'skipped_slots' AS skipped
                FROM audit_log
                WHERE action = 'tryon.rendered'
                  AND user_id = :uid
                  AND detail ->> 'garment_set_hash' = :h
                ORDER BY created_at DESC
                LIMIT 1
                """
            ),
            {"uid": user.id, "h": garment_set_hash},
        )
        raw = covered.scalar_one_or_none()
        return {
            "garment_set_hash": garment_set_hash,
            "rendered": True,
            "reason": None,
            "tryon_url": store.presign_download(rendered_key, ttl_seconds=TRYON_URL_TTL_SECONDS),
            "board_endpoint": f"/outfits/{garment_set_hash}/board",
            "skipped_slots": json.loads(raw) if raw else [],
        }

    consented = await db.execute(text("SELECT count(*) FROM body_photo WHERE revoked_at IS NULL"))
    has_consent = int(consented.scalar_one() or 0) > 0
    provider = getattr(settings, "vton_provider", "")

    queued = False

    # A RENDER THAT ALREADY FAILED MUST NOT READ AS "still working".
    #
    # There is no object in storage either way, so a failure and a job still
    # in flight look identical from here -- and this endpoint used to answer
    # "queued" for both. A render that died in under two seconds against an
    # unreachable provider therefore invited the user to keep tapping, and
    # every tap spent quota re-enqueueing a job that would fail the same way.
    #
    # The worker records the reason (stylist_worker.tryon._record_failure).
    # This reads the most recent one for this outfit and, when it is NEWER
    # than the last request, reports it rather than re-enqueueing. An older
    # failure is ignored on purpose: the user may have fixed the cause, and
    # refusing to try again would make one bad render permanent.
    last_failure = await db.execute(
        text(
            """
            SELECT detail ->> 'reason' AS reason
            FROM audit_log
            WHERE action = 'tryon.failed'
              AND user_id = :uid
              AND detail ->> 'garment_set_hash' = :h
              -- SCOPED TO THIS OUTFIT. Without the hash filter this
              -- compared against the newest request for ANY outfit, so
              -- tapping a second card made the first card's failure look
              -- historic and it was suppressed. With several cards tapped,
              -- every failure was hidden and the screen polled for twelve
              -- minutes against a provider that had been answering 404 in
              -- two seconds the whole time.
              AND created_at > (
                  SELECT coalesce(max(created_at), to_timestamp(0))
                  FROM audit_log
                  WHERE action = 'tryon.requested'
                    AND user_id = :uid
                    AND detail ->> 'garment_set_hash' = :h
              )
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"uid": user.id, "h": garment_set_hash},
    )
    failed = last_failure.scalar_one_or_none()

    # IS A RENDER ALREADY IN FLIGHT FOR THIS OUTFIT?
    #
    # Without this, every poll re-enqueues. The card tells the user to "tap
    # again to check", each tap lands here, finds no image, and spends another
    # unit of a TEN-PER-DAY quota starting a duplicate of the job already
    # running. Polling the screen automatically would have made that far
    # worse -- which is why this lands in the same change as the polling.
    #
    # In flight = a request for THIS outfit that has not since been answered
    # by a success or a failure, and is younger than the render timeout. The
    # timeout matters: without it a worker that died mid-render would leave
    # the outfit permanently "queued" and unretryable.
    inflight = await db.execute(
        text(
            """
            SELECT count(*) FROM audit_log
            WHERE action = 'tryon.requested'
              AND user_id = :uid
              AND detail ->> 'garment_set_hash' = :h
              AND created_at > now() - make_interval(secs => :ttl)
              AND created_at > coalesce((
                    SELECT max(created_at) FROM audit_log
                    WHERE action IN ('tryon.rendered', 'tryon.failed')
                      AND user_id = :uid
                      AND detail ->> 'garment_set_hash' = :h
                  ), to_timestamp(0))
            """
        ),
        {"uid": user.id, "h": garment_set_hash, "ttl": float(RENDER_INFLIGHT_TTL_S)},
    )
    already_running = int(inflight.scalar_one() or 0) > 0

    if force_board:
        reason = "board requested"
    elif not has_consent:
        reason = "no body photo consented; add one to enable try-on"
    elif not provider:
        reason = "try-on is not enabled on this deployment"
    elif failed is not None:
        # Stated plainly instead of another wait. The board is still
        # returned, so the screen still shows the outfit accurately.
        reason = "render failed: " + str(failed)
    elif already_running:
        # Do NOT enqueue a second job, and do NOT charge quota for asking.
        queued = True
        reason = "render in progress"
    else:
        # `user_id` IS IN THE PREDICATE, not left to RLS.
        #
        # `audit_log` has row security DISABLED (relrowsecurity = false) -- it
        # is a cross-tenant operational log by design. So this count, which had
        # no tenant predicate, was counting EVERY user's renders against every
        # user's cap: the first ten try-ons on the deployment would have locked
        # out the whole tenancy for a day.
        #
        # It read as correct because the comment above says "per-tenant" and
        # because a single-user database cannot tell the two apart -- 10 mine,
        # 10 total.
        used = await db.execute(
            text(
                "SELECT count(*) FROM audit_log WHERE action = 'tryon.requested' "
                "AND user_id = :uid AND created_at > now() - interval '1 day'"
            ),
            {"uid": user.id},
        )
        quota = getattr(settings, "tryon_daily_quota", TRYON_DAILY_QUOTA)
        if int(used.scalar_one() or 0) >= quota:
            # §B3: a quota is a feature downgrade, not an outage. The user
            # still gets an accurate picture of the outfit.
            reason = "daily try-on limit reached; showing the flat-lay instead"
        else:
            # Audit row and outbox event in ONE transaction. The audit row is
            # also the quota counter, so committing the enqueue without it
            # would make the quota unenforceable in exactly the situation it
            # exists for — a client retrying in a loop.
            async with tenant_session(user.id) as session:
                # `subject_id` IS A UUID COLUMN, and a garment_set_hash is 64
                # hex characters. Passing the hash raised
                # `invalid UUID ... length must be between 32..36, got 64` and
                # turned this endpoint's "always 200" contract into a 500.
                #
                # It had never run: the branch is only reachable once a
                # provider is configured, so from Phase 10 until the token was
                # set it was dead code that typechecked, passed review and was
                # wrong. The hash goes in `detail`, which is jsonb and is where
                # it belonged all along.
                await session.execute(
                    text(
                        "INSERT INTO audit_log (id, user_id, action, subject_type, "
                        "subject_id, detail) VALUES (:i, :u, 'tryon.requested', "
                        "'outfit', NULL, CAST(:d AS jsonb))"
                    ),
                    {
                        "i": uuid.uuid4(),
                        "u": user.id,
                        # json.dumps, not an f-string: a provider name is
                        # config, but building JSON by interpolation is how a
                        # quote in a value silently corrupts the column.
                        "d": json.dumps(
                            {"provider": provider, "garment_set_hash": garment_set_hash}
                        ),
                    },
                )
                await emit(
                    session,
                    aggregate_id=user.id,
                    user_id=user.id,
                    event_type="tryon.requested",
                    payload={"garment_set_hash": garment_set_hash},
                )
            queued = True
            reason = "render queued; poll this endpoint for the result"

    key = board_key(user.id, garment_set_hash)
    board_url = store.presign_download(key) if store.head(key) is not None else None
    return {
        "garment_set_hash": garment_set_hash,
        "rendered": False,
        "reason": reason,
        "queued": queued,
        # The board is the answer, not an apology for one. It may be None if
        # the nightly warm has not reached this outfit — the caller can then
        # fetch it from GET /outfits/{hash}/board, which renders on demand.
        "board_url": board_url,
        "board_endpoint": f"/outfits/{garment_set_hash}/board",
    }
