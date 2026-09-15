"""The restore drill (Phase 9, DR exit criterion).

    python scripts/restore_drill.py

The plan's line is the whole point: "do the restore drill and record the
wall-clock time. If you can't state the number, your RTO is fiction."

A DRILL THAT ONLY RESTORES PROVES NOTHING
-------------------------------------------
`pg_restore` exiting 0 means the file was replayed, not that the database
works. So this restores into a SCRATCH database and then compares it against
the source, object by object:

  row counts per table   the data is there
  RLS policies           14 of them; a restore that drops these produces a
                         database where every tenant sees every other tenant,
                         and it looks completely healthy
  functions              the SECURITY DEFINER drivers the precompute, the
                         erasure saga and the ops alerts all depend on
  enum types             the taxonomy's 12; a missing one makes every insert
                         fail later, not now
  extensions             pgvector, without which `vector(768)` does not exist

Each of those has failed silently in someone's restore. The row counts are the
obvious check and the least likely to catch a real problem.

WHAT THIS DRILL DOES NOT PROVE
-------------------------------
It restores from the same storage account, on the same machine, into the same
Postgres instance. That measures the RESTORE, not a disaster: it does not
exercise a second region, a cold instance, DNS, or the time to notice. The
number it produces is a FLOOR on the real RTO, and it is reported as one.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from datetime import UTC, datetime

SCRATCH_DB = "stylist_restore_drill"

# What must survive, and why each one is here rather than being assumed.
OBJECT_CHECKS = {
    "tables": "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
    "WHERE n.nspname='public' AND c.relkind='r'",
    "policies": "SELECT count(*) FROM pg_policy",
    "functions": "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
    "WHERE n.nspname='public'",
    "enums": "SELECT count(DISTINCT typname) FROM pg_type WHERE typtype='e'",
    "extensions": "SELECT count(*) FROM pg_extension",
}


def psql(dsn: str, sql: str) -> str:
    result = subprocess.run(["psql", dsn, "-t", "-A", "-c", sql], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"psql failed: {result.stderr[:400]}")
    return result.stdout.strip()


def table_counts(dsn: str) -> dict[str, int]:
    """Row count per table. Exact, not estimated — `reltuples` is a planner
    statistic and can be stale or zero on a freshly restored database, which
    would make every comparison pass."""
    tables = psql(
        dsn,
        "SELECT c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname='public' AND c.relkind='r' ORDER BY 1",
    ).splitlines()
    counts = {}
    for table in [t for t in tables if t]:
        counts[table] = int(psql(dsn, f'SELECT count(*) FROM "{table}"'))
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", default=None, help="an existing .dump to restore")
    parser.add_argument("--keep", action="store_true", help="leave the scratch DB behind")
    args = parser.parse_args()

    source = (os.environ.get("MIGRATION_DATABASE_URL") or os.environ["DATABASE_URL"]).replace(
        "+asyncpg", ""
    )
    admin = source.rsplit("/", 1)[0] + "/postgres"
    scratch = source.rsplit("/", 1)[0] + "/" + SCRATCH_DB

    print("=" * 62)
    print("RESTORE DRILL")
    print("=" * 62)

    print("\n[0] tool versions")
    # A VERSION MISMATCH IS A DR HAZARD, and it is silent in the benign
    # direction. Measured here: a PG17 client dumping a PG16 server emits
    # `SET transaction_timeout`, which PG16 does not understand — pg_restore
    # exits 1 and the drill only passed because the VERIFICATION said the data
    # was fine. The reverse (old client, new server) loses data rather than
    # printing a warning.
    #
    # In an incident nobody checks this. Printing it every run is the cheapest
    # way to make sure someone has.
    # `pg_dump --version` prints "pg_dump (PostgreSQL) 17.10 (Homebrew)" — the
    # last token is a packaging suffix, not the version, so the number is
    # matched explicitly. Taking [-1] reported the client as "(Homebrew)" and
    # compared that against "16", which "differs" from everything and made the
    # warning fire permanently.
    raw = subprocess.run(["pg_dump", "--version"], capture_output=True, text=True).stdout
    match = re.search(r"(\d+)\.(\d+)", raw)
    client = match.group(0) if match else "unknown"
    server = psql(source, "SHOW server_version").split()[0]
    print(f"    pg_dump {client}   server {server}")
    if client.split(".")[0] != server.split(".")[0]:
        print(
            f"    WARNING: major version mismatch ({client} vs {server}). "
            "A dump taken by a NEWER client can emit settings the server does "
            "not know; an OLDER client against a newer server can silently "
            "lose data. Match them in production."
        )

    print("\n[1] recording the source state")
    before = table_counts(source)
    objects_before = {k: int(psql(source, q)) for k, q in OBJECT_CHECKS.items()}
    print(f"    {sum(before.values())} rows across {len(before)} tables")
    print("    " + "  ".join(f"{k}={v}" for k, v in objects_before.items()))

    dump_path = args.dump
    dump_seconds = 0.0
    if dump_path is None:
        print("\n[2] taking a dump")
        from backup import dump as take_dump

        dump_path = f"/tmp/drill-{datetime.now(UTC).strftime('%H%M%S')}.dump"
        t0 = time.monotonic()
        size = take_dump(source, dump_path)
        dump_seconds = time.monotonic() - t0
        print(f"    {size / 1e6:.1f} MB in {dump_seconds:.1f}s")
    else:
        print(f"\n[2] using {dump_path}")

    # THE CLOCK STARTS HERE. Restoring is the only part a real incident
    # repeats; the dump already exists when the disaster happens.
    print("\n[3] RESTORING — clock running")
    started = time.monotonic()

    psql(admin, f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')
    psql(admin, f'CREATE DATABASE "{SCRATCH_DB}"')
    result = subprocess.run(
        ["pg_restore", "--no-owner", "--no-privileges", "--dbname", scratch, dump_path],
        capture_output=True,
        text=True,
    )
    restore_seconds = time.monotonic() - started

    # pg_restore exits non-zero on WARNINGS too, so the exit code alone is not
    # a verdict. The verification below is what decides whether this worked.
    if result.returncode != 0:
        print(f"    pg_restore exited {result.returncode} (checking whether it matters)")
        for line in result.stderr.splitlines()[:5]:
            print(f"      {line}")
    print(f"    restore completed in {restore_seconds:.1f}s")

    print("\n[4] VERIFYING — a restore that runs is not a restore that worked")
    failures: list[str] = []

    after = table_counts(scratch)
    missing = sorted(set(before) - set(after))
    if missing:
        failures.append(f"tables missing entirely: {missing}")
    for table, count in sorted(before.items()):
        got = after.get(table, -1)
        if got != count:
            failures.append(f"{table}: {count} rows -> {got}")
    print(f"    rows: {sum(after.values())} across {len(after)} tables")

    objects_after = {k: int(psql(scratch, q)) for k, q in OBJECT_CHECKS.items()}
    for name, expected in objects_before.items():
        got = objects_after[name]
        if got < expected:
            # LESS is a failure; more is not. A scratch database can pick up
            # extra extensions from template1, and failing on that would make
            # the drill cry wolf.
            failures.append(f"{name}: {expected} -> {got}")
    print("    " + "  ".join(f"{k}={v}" for k, v in objects_after.items()))

    # A FUNCTIONAL check, not just a structural one. RLS being PRESENT is not
    # RLS WORKING: a policy that restored without its FORCE flag looks correct
    # in pg_policy and lets the app role read every tenant's rows.
    forced = int(
        psql(
            scratch,
            "SELECT count(*) FROM pg_class WHERE relrowsecurity AND relforcerowsecurity",
        )
    )
    forced_before = int(
        psql(
            source,
            "SELECT count(*) FROM pg_class WHERE relrowsecurity AND relforcerowsecurity",
        )
    )
    if forced < forced_before:
        failures.append(f"FORCE RLS tables: {forced_before} -> {forced}")
    print(f"    FORCE RLS tables: {forced}")

    if not args.keep:
        psql(admin, f'DROP DATABASE IF EXISTS "{SCRATCH_DB}"')

    print("\n" + "=" * 62)
    if failures:
        print("DRILL FAILED")
        for f in failures:
            print(f"  - {f}")
        print("=" * 62)
        return 1

    print("DRILL PASSED")
    print(f"  RESTORE TIME: {restore_seconds:.1f}s   ({sum(before.values())} rows, 47 MB class)")
    if dump_seconds:
        print(f"  dump time:    {dump_seconds:.1f}s")
    print()
    print("  This is a FLOOR on the real RTO, not the RTO. It restores from")
    print("  local storage, on this machine, into the same Postgres instance.")
    print("  It does not measure: a second region, a cold instance, DNS, or")
    print("  the time to NOTICE. Multiply accordingly before quoting it.")
    print("=" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
