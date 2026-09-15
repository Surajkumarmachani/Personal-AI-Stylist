"""Nightly logical dump (Phase 9, DR).

    python scripts/backup.py              # dump to object storage
    python scripts/backup.py --list       # what backups exist

WHY LOGICAL AND NOT A SNAPSHOT
-------------------------------
A volume snapshot is faster to take and restores the WHOLE instance — it cannot
restore one database into a scratch instance to be verified, which is the only
way to find out whether a backup works before you need it. A logical dump is
restorable anywhere, including into a throwaway database on a laptop, which is
what makes the drill possible at all.

`--format=custom`, so `pg_restore` can do a selective or parallel restore
later. A plain SQL file can only be replayed start to finish.

DUMPED AS THE OWNER, AND THAT IS NOT AN OVERSIGHT
---------------------------------------------------
`stylist_app` is NOBYPASSRLS, so a dump taken as the app role would silently
contain only the rows visible under the current `app.user_id` — which is none
of them. The result is a backup that completes, weighs almost nothing, and
restores an empty database. That failure is invisible until the restore.

WHAT THIS DOES NOT DO
---------------------
§C5 says "nightly logical dump to the SECOND CLOUD ACCOUNT". This writes to the
same object storage as everything else, because there is one account. A backup
in the same blast radius as the thing it protects is not a disaster recovery
plan — it survives a bad migration, not a compromised or deleted account. That
gap is REAL and is recorded in the drill report rather than papered over.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from datetime import UTC, datetime

BACKUP_PREFIX = "backups/postgres"


def _store():
    from stylist_api.settings import get_settings
    from stylist_clients.storage import ObjectStore

    s = get_settings()
    return ObjectStore(
        bucket=s.s3_bucket,
        endpoint_url=s.s3_endpoint_url,
        public_endpoint_url=s.s3_public_endpoint_url,
        region=s.s3_region,
        access_key=s.s3_access_key,
        secret_key=s.s3_secret_key,
    )


def dump(dsn: str, path: str) -> int:
    """pg_dump to a local file. Returns bytes written."""
    result = subprocess.run(
        [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            # --no-owner so the restore does not need the exact role names to
            # exist first. Roles are infrastructure, not data, and a restore
            # that fails because `stylist_owner` is missing on a fresh box is
            # a restore that fails at the worst possible moment.
            "--no-privileges",
            "--file",
            path,
            dsn,
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"pg_dump failed: {result.stderr[:500]}")
    return os.path.getsize(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--list", action="store_true", help="list stored backups")
    parser.add_argument("--out", default=None, help="write locally instead of uploading")
    args = parser.parse_args()

    if args.list:
        store = _store()
        import boto3  # noqa: F401 - imported for the side of the client below

        paginator = store._client.get_paginator("list_objects_v2")
        found = 0
        for page in paginator.paginate(Bucket=store.bucket, Prefix=BACKUP_PREFIX):
            for obj in page.get("Contents", []):
                print(f"  {obj['Key']}  {obj['Size'] / 1e6:.1f} MB  {obj['LastModified']}")
                found += 1
        if not found:
            print("no backups found — a DR plan with no backups is a document")
            return 1
        return 0

    dsn = os.environ.get("MIGRATION_DATABASE_URL") or os.environ["DATABASE_URL"]
    dsn = dsn.replace("+asyncpg", "")
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    local = args.out or f"/tmp/stylist-{stamp}.dump"

    started = time.monotonic()
    size = dump(dsn, local)
    elapsed = time.monotonic() - started
    print(f"dump: {size / 1e6:.1f} MB in {elapsed:.1f}s -> {local}")

    if args.out:
        return 0

    key = f"{BACKUP_PREFIX}/{stamp}.dump"
    with open(local, "rb") as fh:
        _store().put_bytes(key, fh.read(), content_type="application/octet-stream")
    os.remove(local)
    print(f"uploaded: {key}")
    print(
        "NOTE: same storage account as the data it protects. Survives a bad "
        "migration, not a compromised or deleted account (see module docstring)."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
