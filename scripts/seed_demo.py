"""Seed a demo wardrobe at Phase 5's scale: 10 users, 2000 garments.

WHAT THIS IS AND IS NOT
-----------------------
This is a SCALE TEST. At 2000 garments the questions that matter are whether
search, facets, most-worn and vector similarity still answer quickly, whether
the indexes are the right ones, and whether anything is accidentally O(n) per
row. Those are answerable with synthetic data.

It is NOT Phase 5's exit criteria. Those ask for "20 users, >=2,000 garments
ingested" and "correction rate per field measured on real data", and the plan
is explicit that this is "the real go/no-go". Correction rate against generated
tags measures the generator. Nothing here counts toward that.

WHY IT WRITES DIRECTLY TO THE DATABASE
--------------------------------------
The real pipeline takes ~6s per photo, so 2000 photos is over three hours of
inference to produce data whose ML content is not what is being tested. Ingest
is already verified end-to-end by verify_ingest_e2e.py. This fills the tables
the query layer reads, and uses the REAL ingest path for a small sample so the
two are comparable.

WHAT IS MADE REALISTIC ON PURPOSE
---------------------------------
  - Wardrobes are SKEWED, not uniform. A real closet is mostly tops and
    bottoms in a handful of colours; sampling 144 subcategories uniformly
    would make every facet count identical and hide exactly the distribution
    problems facets exist to surface.
  - Wear counts follow a power law. Most garments are worn rarely and a few
    constantly, which is the shape "most worn" and cost-per-wear have to
    handle.
  - Embeddings are clustered by subcategory, not random. Random vectors are
    all equidistant in 768 dimensions, so "find similar" would return noise
    and the HNSW index would never be exercised the way real data exercises it.
  - A few deliberate near-duplicates, so the duplicate review queue is not
    empty.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import random
import sys
import time
import uuid
from datetime import date, timedelta

import httpx
import numpy as np
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

sys.path[:0] = ["packages"]
from stylist_domain.taxonomy import load_taxonomy  # noqa: E402

API = os.environ.get("API_BASE", "http://localhost:8080")
OWNER_DSN = os.environ.get(
    "MIGRATION_DATABASE_URL",
    "postgresql://stylist_owner:stylist_owner_local_only@localhost:55432/stylist",
).replace("postgresql://", "postgresql+asyncpg://")

# A plausible Indian + Western mixed wardrobe rather than a uniform draw over
# the taxonomy. Weights are the point: facet counts and filter usefulness both
# depend on the distribution being lopsided.
SLOT_WEIGHTS = {
    "upper_base": 0.30,
    "lower": 0.22,
    "full_body": 0.10,
    "upper_layer": 0.09,
    "feet": 0.10,
    "accessory": 0.08,
    "drape": 0.05,
    "bag": 0.04,
    "head": 0.02,
}
COLOUR_WEIGHTS = {
    "black": 0.15,
    "white": 0.12,
    "blue_navy": 0.09,
    "grey_charcoal": 0.07,
    "denim_indigo": 0.07,
    "grey_light": 0.05,
    "beige": 0.05,
    "brown": 0.04,
    "olive": 0.04,
    "maroon": 0.04,
    "rust": 0.03,
    "mustard": 0.03,
    "teal": 0.03,
    "pink_blush": 0.03,
    "red": 0.03,
    "cream_ivory": 0.03,
    "green_forest": 0.02,
    "purple": 0.02,
    "tan_camel": 0.02,
    "emerald": 0.02,
    "rani_pink": 0.02,
}


def weighted(weights: dict[str, float], allowed: tuple[str, ...]) -> str:
    """Pick by weight, restricted to values the taxonomy actually contains.

    The intersection matters: a weight table that drifts from taxonomy.yaml
    would otherwise insert a value the enum rejects, and the failure would
    arrive 1,800 rows into a seed run.
    """
    pairs = [(k, v) for k, v in weights.items() if k in allowed]
    if not pairs:
        return random.choice(allowed)
    keys, vals = zip(*pairs, strict=True)
    return random.choices(keys, weights=vals, k=1)[0]


def power_law_wears(rng: random.Random) -> int:
    """Most things rarely, a few constantly."""
    r = rng.random()
    if r < 0.45:
        return 0
    if r < 0.75:
        return rng.randint(1, 3)
    if r < 0.93:
        return rng.randint(4, 12)
    return rng.randint(13, 60)


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=10)
    ap.add_argument("--garments", type=int, default=2000)
    ap.add_argument(
        "--real-ingest",
        type=int,
        default=3,
        help="photos per user pushed through the REAL pipeline",
    )
    args = ap.parse_args()

    rng = random.Random(20260911)
    np_rng = np.random.default_rng(20260911)
    taxonomy = load_taxonomy()

    # Subcategory -> slot, so a seeded garment is internally consistent. A
    # "jeans" in the `top` slot would make every slot filter look broken.
    # Subcategory must belong to its slot, or every slot filter looks broken:
    # a "jeans" filed under `head` is not a distribution quirk, it is wrong
    # data that makes the feature under test appear faulty.
    by_slot: dict[str, list[str]] = {
        slot: list(subs) for slot, subs in taxonomy.subcategories_by_slot.items()
    }

    print(f"seeding {args.users} users x ~{args.garments // args.users} garments")

    engine = create_async_engine(OWNER_DSN, pool_pre_ping=True)
    t0 = time.monotonic()

    # ---- real users through the real auth path -------------------------
    users: list[tuple[uuid.UUID, dict[str, str]]] = []
    with httpx.Client(base_url=API, timeout=60) as client:
        for i in range(args.users):
            email = f"demo{i + 1}-{uuid.uuid4().hex[:6]}@example.com"
            resp = client.post(
                "/auth/register", json={"email": email, "password": "a-long-enough-password"}
            )
            resp.raise_for_status()
            token = resp.json()["access_token"]
            auth = {"Authorization": f"Bearer {token}"}
            me = client.get("/garments", headers=auth)
            me.raise_for_status()
            users.append((email, auth))  # type: ignore[arg-type]
    print(f"  {len(users)} users registered via the API")

    async with engine.begin() as conn:
        rows = await conn.execute(
            text("SELECT id, email FROM users WHERE email = ANY(:emails)"),
            {"emails": [e for e, _ in users]},
        )
        ids = {r[1]: r[0] for r in rows}
        cutouts = [
            r[0]
            for r in await conn.execute(
                text(
                    "SELECT DISTINCT cutout_key FROM garments "
                    "WHERE cutout_key IS NOT NULL LIMIT 800"
                )
            )
        ]
    print(f"  reusing {len(cutouts)} existing cutouts so the UI shows real images")

    # One centroid per subcategory: similarity search is only meaningful if
    # like things are actually close together.
    centroids: dict[str, np.ndarray] = {}

    def embedding_for(subcat: str) -> list[float]:
        if subcat not in centroids:
            v = np_rng.normal(size=768)
            centroids[subcat] = v / np.linalg.norm(v)
        vec = centroids[subcat] + np_rng.normal(scale=0.35, size=768)
        vec = vec / np.linalg.norm(vec)
        return [round(float(x), 6) for x in vec]

    per_user = args.garments // args.users
    inserted = 0
    dup_pairs = 0
    wear_rows = 0

    for email, _ in users:
        uid = ids[email]
        batch: list[dict] = []
        wears: list[dict] = []
        previous: dict | None = None

        for n in range(per_user):
            slot = weighted(SLOT_WEIGHTS, taxonomy.slots)
            choices = by_slot.get(slot) or list(taxonomy.subcategories)
            subcat = rng.choice(choices)
            colour = weighted(COLOUR_WEIGHTS, taxonomy.colours)
            gid = uuid.uuid4()

            # Every 40th garment is a deliberate near-duplicate of the one
            # before it, so the review queue has something in it.
            make_dupe = previous is not None and n % 40 == 39
            phash = previous["phash"] if make_dupe else f"{rng.getrandbits(64):016x}"
            if make_dupe:
                dup_pairs += 1

            row = {
                "id": gid,
                "user_id": uid,
                "original_key": f"originals/{uid}/{gid}",
                "cutout_key": rng.choice(cutouts) if cutouts else None,
                "phash": phash,
                "slot": slot,
                "subcategory": subcat,
                "primary_colour": colour,
                "secondary_colour": rng.choice(taxonomy.colours) if rng.random() < 0.25 else None,
                "pattern": rng.choice(taxonomy.patterns) if rng.random() < 0.4 else None,
                "material": rng.choice(taxonomy.materials),
                "fit": rng.choice(taxonomy.fits),
                "dress_code": rng.choice(taxonomy.dress_codes),
                "climate": [rng.choice(taxonomy.climate_bands)],
                "formality": rng.randint(1, 5),
                "warmth": rng.randint(1, 5),
                "embedding": "[" + ",".join(str(x) for x in embedding_for(subcat)) + "]",
                "state": "duplicate_suspect" if make_dupe else "matted",
                "duplicate_of": previous["id"] if make_dupe else None,
                # ~12% flagged, roughly what a real low-confidence rate looks
                # like, so the review badge is exercised without drowning the UI.
                "needs_review": rng.random() < 0.12,
                "needs_wash": rng.random() < 0.18,
                "price": rng.choice([None, 49900, 129900, 79900, 249900, 29900, 189900]),
            }
            batch.append(row)
            previous = row

            for k in range(power_law_wears(rng)):
                wears.append(
                    {
                        "id": uuid.uuid4(),
                        "user_id": uid,
                        "garment_id": gid,
                        # Distinct days: the unique index enforces one wearing
                        # per garment per day, and a collision here would abort
                        # the whole batch.
                        "worn_on": date.today() - timedelta(days=k * 3 + rng.randint(0, 2)),
                    }
                )

        async with engine.begin() as conn:
            await conn.execute(
                text(
                    """
                    INSERT INTO garments (
                        id, user_id, original_key, cutout_key, phash, slot, subcategory,
                        primary_colour, secondary_colour, pattern, material, fit, dress_code,
                        climate_bands, formality, warmth, embedding, state, duplicate_of,
                        needs_review, needs_wash, purchase_price_minor, purchase_currency,
                        extractor_version, embedding_version, is_active, created_at, updated_at
                    ) VALUES (
                        :id, :user_id, :original_key, :cutout_key, :phash,
                        CAST(:slot AS slot), CAST(:subcategory AS subcategory),
                        CAST(:primary_colour AS colour), CAST(:secondary_colour AS colour),
                        CAST(:pattern AS pattern), CAST(:material AS material),
                        CAST(:fit AS fit), CAST(:dress_code AS dress_code),
                        :climate, :formality, :warmth, CAST(:embedding AS vector),
                        :state, :duplicate_of, :needs_review, :needs_wash,
                        :price, 'INR', 'seed-demo-v1', 'seed-demo-v1', true,
                        now() - (random() * interval '120 days'), now()
                    )
                    """
                ),
                batch,
            )
            if wears:
                # Dedupe (garment, day) in Python: the unique index is a
                # constraint, not a filter, and one collision would roll back
                # every wearing for this user.
                seen: set[tuple[uuid.UUID, date]] = set()
                unique = []
                for w in wears:
                    key = (w["garment_id"], w["worn_on"])
                    if key not in seen:
                        seen.add(key)
                        unique.append(w)
                await conn.execute(
                    text(
                        "INSERT INTO wear_log (id, user_id, garment_id, worn_on) "
                        "VALUES (:id, :user_id, :garment_id, :worn_on)"
                    ),
                    unique,
                )
                wear_rows += len(unique)
        inserted += len(batch)
        print(f"  {email.split('-')[0]}: {len(batch)} garments", flush=True)

    await engine.dispose()
    elapsed = time.monotonic() - t0
    print(
        f"\nseeded {inserted} garments, {wear_rows} wear rows, "
        f"{dup_pairs} duplicate proposals in {elapsed:.1f}s"
    )

    # ---- a small REAL ingest sample, for comparability ------------------
    if args.real_ingest:
        print(f"\npushing {args.real_ingest} photo(s) per user through the REAL pipeline")
        sys.path[:0] = ["scripts"]
        from verify_ingest_e2e import flat_lay_jpeg, sign_up, upload_one, wait_for_terminal

        latencies: list[float] = []
        with httpx.Client(base_url=API, timeout=180) as client:
            s = sign_up(client)
            for i in range(args.real_ingest):
                up, key = upload_one(s, flat_lay_jpeg(5000 + i))
                t = time.monotonic()
                r = client.post(
                    "/garments/ingest",
                    json={"upload_ids": [up], "keys": [key]},
                    headers={**s.auth, "Idempotency-Key": str(uuid.uuid4())},
                )
                r.raise_for_status()
                wait_for_terminal(s, r.json()["job_ids"][0], timeout=180.0)
                latencies.append(time.monotonic() - t)
        latencies.sort()
        print(
            f"  real ingest: median {latencies[len(latencies) // 2]:.1f}s, max {latencies[-1]:.1f}s"
        )

    print("\nNOTE: this is a scale test. It does NOT satisfy Phase 5's exit")
    print("criteria, which require real users and real wardrobes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
