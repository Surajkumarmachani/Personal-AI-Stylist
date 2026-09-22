"""Re-matte cutouts that a segmentation mask shattered.

WHY A SCRIPT AND NOT A MIGRATION: this rewrites OBJECTS, not rows. Nothing in
the database changes except `attributes_raw.matte`, so there is no schema
version to bump and no down-migration to write. The bucket is versioned, so
every cutout this replaces is still retrievable.

It applies the same rule the matte stage now applies at ingest
(MIN_LARGEST_BLOB_SHARE): matte with the mask, measure how much of the alpha
is one connected piece, and if the mask shattered the garment, re-matte the
bounding box with rembg alone and keep whichever is less fragmented. Garments
that are already clean are skipped, so this is safe to re-run.

    python scripts/remedy_cutouts.py --email you@example.com [--apply]

Dry-run by default: it reports what it would change and writes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import os
import sys

import numpy as np
from PIL import Image
from sqlalchemy import text

from stylist_db.session import init_engine, system_session, tenant_session
from stylist_worker.deps import get_ml_client, get_object_store
from stylist_worker.stages.matte import MIN_LARGEST_BLOB_SHARE, _crop_to_bbox


def fragmentation(png: bytes) -> tuple[int, float]:
    """(blob count, share of alpha in the largest blob).

    Flood fill rather than `scipy.ndimage.label`, which is what the ml service
    uses for the same measurement: scipy is installed there and deliberately
    NOT in the worker, whose image stays small because it orchestrates rather
    than infers. A four-line BFS over a handful of 500x500 masks is not worth
    a dependency, and the two must agree on the number, so the definition is
    the one thing to keep identical: 4-connectivity over alpha > 128.
    """
    alpha = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA").getchannel("A")) > 128
    total = int(alpha.sum())
    if total == 0:
        return 0, 1.0

    h, w = alpha.shape
    seen = np.zeros_like(alpha)
    blobs, largest = 0, 0
    ys, xs = np.nonzero(alpha)
    for sy, sx in zip(ys.tolist(), xs.tolist(), strict=True):
        if seen[sy, sx]:
            continue
        blobs += 1
        size = 0
        stack = [(sy, sx)]
        seen[sy, sx] = True
        while stack:
            y, x = stack.pop()
            size += 1
            for ny, nx in ((y - 1, x), (y + 1, x), (y, x - 1), (y, x + 1)):
                if 0 <= ny < h and 0 <= nx < w and alpha[ny, nx] and not seen[ny, nx]:
                    seen[ny, nx] = True
                    stack.append((ny, nx))
        largest = max(largest, size)
    return blobs, largest / total


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--apply", action="store_true", help="write; otherwise dry-run")
    args = ap.parse_args()

    # Same as scripts/rebuild_style_vectors.py: a standalone script does not
    # inherit the worker's startup hook, so it opens its own engine.
    init_engine(os.environ["DATABASE_URL"])

    async with system_session() as s:
        row = await s.execute(text("SELECT id FROM users WHERE email = :e"), {"e": args.email})
        user_id = row.scalar_one_or_none()
    if user_id is None:
        print(f"no such user: {args.email}")
        return 1

    async with tenant_session(user_id) as s:
        rows = (
            (
                await s.execute(
                    text(
                        """
                        SELECT id, original_key, cutout_key,
                               attributes_raw->'segment'->>'mask_key' AS mask_key,
                               attributes_raw->'segment'->'bbox'      AS bbox
                        FROM garments
                        WHERE is_active AND cutout_key IS NOT NULL
                          AND state NOT IN ('rejected', 'quarantined')
                        ORDER BY created_at DESC
                        """
                    )
                )
            )
            .mappings()
            .all()
        )

    # The worker's own constructors, so this reads the same endpoints, bucket
    # and credentials the pipeline uses rather than a second set that can drift.
    store, ml = get_object_store(), get_ml_client()
    repaired = clean = 0
    for r in rows:
        before_n, before = fragmentation(store.get_bytes(r["cutout_key"]))
        if before >= MIN_LARGEST_BLOB_SHARE:
            clean += 1
            continue

        image = store.get_bytes(r["original_key"])
        bbox = list(r["bbox"]) if r["bbox"] else None
        retry = await ml.matte(image_bytes=_crop_to_bbox(image, bbox), mask_png=None)
        after_n, after = fragmentation(retry.cutout_png)

        verdict = "REPAIR" if after > before else "no better, left alone"
        print(
            f"  {str(r['id'])[:8]}  {before_n:3d} blobs / {before:.2f}"
            f"  ->  {after_n:3d} blobs / {after:.2f}   {verdict}"
        )
        if after <= before:
            continue
        repaired += 1
        if not args.apply:
            continue

        store.put_bytes(r["cutout_key"], retry.cutout_png, content_type="image/png")
        async with tenant_session(user_id) as s:
            await s.execute(
                text(
                    """
                    UPDATE garments
                    SET attributes_raw = jsonb_set(
                            COALESCE(attributes_raw, '{}'::jsonb), '{matte}',
                            -- CAST(), not `:matte::jsonb`: SQLAlchemy's text()
                            -- parser swallows the parameter next to the `::`
                            -- cast and binds only the uuid, which fails at
                            -- execute time rather than at parse time.
                            CAST(:matte AS jsonb), true),
                        updated_at = now()
                    WHERE id = :gid
                    """
                ),
                {
                    "gid": r["id"],
                    "matte": (
                        '{"mask_discarded": true, "repaired_by": "remedy_cutouts",'
                        f' "largest_blob_share": {after:.4f}}}'
                    ),
                },
            )

    print(
        f"\n  {clean} already clean, {repaired} "
        + ("repaired" if args.apply else "would be repaired (dry run; pass --apply)")
    )
    return 0


sys.exit(asyncio.run(main()))
