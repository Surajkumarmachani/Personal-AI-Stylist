"""Building a data export, off the request path (§C5 portability, Phase 9).

WHY THIS MOVED OUT OF THE REQUEST
----------------------------------
The synchronous version built the whole ZIP in memory and returned it. That
works at nine garments and cannot work at two thousand: the archive contains
every cutout the user owns, so it grows with the wardrobe while the request
timeout does not. There is a wardrobe size at which the old endpoint silently
starts failing and no obvious place to notice.

WRITTEN TO A TEMPORARY FILE, NOT A BUFFER
-------------------------------------------
Moving to a worker is not enough on its own — an in-memory ZIP is just as fatal
in a worker, and worse, because it takes the other jobs on that process with
it. `zipfile` writes to any file object, so the archive is built on disk and
streamed to storage. Peak memory is one image, not the whole wardrobe.

CSV AND JSON, BOTH
------------------
§C5 says "CSV + all images". CSV is what a person can open; JSON is what
survives a round trip without a spreadsheet reinterpreting dates and ids.
Shipping only CSV makes the export unusable as a migration path, and only JSON
makes it unusable to a person — the obligation is portability, which is both.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import tempfile
import uuid
import zipfile
from pathlib import Path
from typing import Any

from sqlalchemy import text

from stylist_db.session import system_session, tenant_session
from stylist_obs import stage_span

logger = logging.getLogger(__name__)

# What an export must contain. Named explicitly rather than reflected off the
# schema: portability is a promise about CONTENT, so a new table holding user
# data that nobody adds here is a silent gap — and reflection would also sweep
# in internal bookkeeping that means nothing to the person reading it.
EXPORT_TABLES = (
    "garments",
    "wear_log",
    "outfit_feedback",
    "preference_fact",
    "garment_corrections",
    "outfits",
    "device_token",
    "calendar_link",
)

# Columns that must never leave in an export, per table. An export is handed to
# the user, but a refresh token or a device registration token is a CREDENTIAL —
# exporting it is handing over something that still works, into a file that will
# sit in a downloads folder.
REDACTED = {
    "calendar_link": {"refresh_token"},
    "device_token": {"token"},
    "user_profile": {"litellm_key", "password_hash"},
}

EXPORT_PREFIX = "exports"


def export_key(user_id: uuid.UUID, export_id: uuid.UUID) -> str:
    return f"{EXPORT_PREFIX}/{user_id}/{export_id}.zip"


def _redact(table: str, row: dict[str, Any]) -> dict[str, Any]:
    drop = REDACTED.get(table, set())
    return {k: ("[redacted]" if k in drop and v is not None else v) for k, v in row.items()}


def _csv_bytes(rows: list[dict[str, Any]]) -> bytes:
    if not rows:
        return b""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({k: ("" if v is None else str(v)) for k, v in row.items()})
    return buf.getvalue().encode()


async def build_export(
    ctx: dict[str, Any], *, user_id: str, aggregate_id: str, payload: dict[str, Any]
) -> dict[str, Any]:
    """Build one export and upload it. Idempotent per export id.

    The signature is the OUTBOX's, not one of our choosing: the relay calls
    every handler with (user_id, aggregate_id, payload), and `aggregate_id` is
    the export request. A handler with a bespoke signature would raise a
    TypeError inside the relay for every event — which is exactly how Phase 2's
    `job_id=` bug presented.
    """
    uid, eid = uuid.UUID(user_id), uuid.UUID(aggregate_id)
    from stylist_worker import deps

    store = deps.get_object_store()

    async with tenant_session(uid) as db:
        await db.execute(
            text("UPDATE export_request SET state = 'building' WHERE id = :i"), {"i": eid}
        )

    manifest: dict[str, Any] = {"tables": {}, "images": 0, "redacted": {}}

    try:
        with stage_span("build_export"), tempfile.TemporaryDirectory() as tmp:
            archive_path = Path(tmp) / "export.zip"

            # Built on DISK. An in-memory ZIP in a worker is worse than one in
            # a request — it takes the other jobs on the process with it.
            with zipfile.ZipFile(archive_path, "w", zipfile.ZIP_DEFLATED) as archive:
                async with tenant_session(uid) as db:
                    for table in EXPORT_TABLES:
                        result = await db.execute(text(f"SELECT * FROM {table}"))
                        rows = [_redact(table, dict(r)) for r in result.mappings()]
                        archive.writestr(
                            f"data/{table}.json", json.dumps(rows, indent=1, default=str)
                        )
                        archive.writestr(f"data/{table}.csv", _csv_bytes(rows))
                        manifest["tables"][table] = len(rows)
                        if table in REDACTED:
                            manifest["redacted"][table] = sorted(REDACTED[table])

                    cutouts = await db.execute(
                        text(
                            "SELECT id, cutout_key FROM garments "
                            "WHERE is_active AND cutout_key IS NOT NULL"
                        )
                    )
                    keys = [(str(r["id"]), r["cutout_key"]) for r in cutouts.mappings()]

                # ONE IMAGE IN MEMORY AT A TIME. Reading them all first would
                # reintroduce exactly the problem this rewrite exists to fix.
                missing = 0
                for garment_id, key in keys:
                    try:
                        archive.writestr(f"images/{garment_id}.png", store.get_bytes(key))
                        manifest["images"] += 1
                    except Exception:
                        # One unreadable object must not fail an export of
                        # everything else. Counted, so a gap is visible in the
                        # manifest rather than being an absence nobody notices.
                        missing += 1
                if missing:
                    manifest["images_missing"] = missing

                archive.writestr("manifest.json", json.dumps(manifest, indent=1))
                archive.writestr(
                    "README.txt",
                    "Your Personal AI Stylist data export.\n\n"
                    "data/   one JSON and one CSV per table. JSON round-trips exactly;\n"
                    "        CSV opens in a spreadsheet.\n"
                    "images/ the cutout for each garment, named by its id.\n\n"
                    "Credentials (calendar and push tokens) are shown as [redacted]:\n"
                    "they still work, and an export is not a safe place for them.\n",
                )

            size = archive_path.stat().st_size
            key = export_key(uid, eid)
            store.put_bytes(key, archive_path.read_bytes(), content_type="application/zip")

        async with tenant_session(uid) as db:
            await db.execute(
                text(
                    "UPDATE export_request SET state = 'ready', object_key = :k, "
                    "size_bytes = :s, manifest = CAST(:m AS jsonb), completed_at = now() "
                    "WHERE id = :i"
                ),
                {"i": eid, "k": key, "s": size, "m": json.dumps(manifest)},
            )
        logger.info("export %s ready: %d bytes, %d images", eid, size, manifest["images"])
        return {"export_id": str(eid), "bytes": size, **manifest}

    except Exception as exc:
        logger.exception("export %s failed", eid)
        # A fresh session: the failing statement may have aborted the one above,
        # and recording the failure must not depend on the transaction that
        # failed. Same trap the erasure saga hit.
        async with tenant_session(uid) as db:
            await db.execute(
                text("UPDATE export_request SET state = 'failed', last_error = :e WHERE id = :i"),
                {"i": eid, "e": f"{type(exc).__name__}: {exc}"[:2000]},
            )
        raise


async def sweep_expired_exports(ctx: dict[str, Any]) -> dict[str, Any]:
    """Delete exports past their 7-day life. §C5's "7-day link", enforced.

    A presigned URL expiring only stops new downloads — the ZIP is still in the
    bucket. That archive is a complete second copy of a user's wardrobe, i.e.
    precisely what the erasure saga works to remove, so leaving it is a
    liability that grows with every user who ever clicked the button.

    ALL VERSIONS, like everything else that deletes from this bucket: it is
    versioned, so a plain delete leaves the copy recoverable.
    """
    from stylist_worker import deps

    store = deps.get_object_store()

    async with system_session() as db:
        rows = await db.execute(text("SELECT * FROM expired_exports()"))
        expired = [dict(r) for r in rows.mappings()]

    removed = 0
    for record in expired:
        try:
            store.delete_all_versions(record["object_key"])
        except Exception as exc:
            # Left for the next sweep. Not marked deleted, because marking it
            # would mean nothing ever tries again and the copy stays forever.
            logger.warning("export sweep: %s failed: %s", record["object_key"], exc)
            continue
        async with tenant_session(record["user_id"]) as db:
            await db.execute(
                text(
                    "UPDATE export_request SET deleted_at = now(), object_key = NULL WHERE id = :i"
                ),
                {"i": record["id"]},
            )
        removed += 1

    if removed:
        logger.info("export sweep removed %d expired archive(s)", removed)
    return {"expired": len(expired), "removed": removed}
