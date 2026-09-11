"""Phase 5 exit-criteria checks against a live stack.

Covers the four deliverables that are testable without real users: dedupe,
wear log + laundry, search + filters, and the most-worn ranking the onboarding
flow is built on. Run with the stack up:

    python scripts/verify_phase5_e2e.py

Exits 0 only if every check passes. Like verify_ingest_e2e.py, a check that
cannot be evaluated is a FAILURE, never a skip.
"""

from __future__ import annotations

import sys
import uuid
from datetime import date, timedelta
from typing import Any

import httpx

sys.path[:0] = ["scripts"]
from verify_ingest_e2e import (  # noqa: E402
    Session,
    flat_lay_jpeg,
    sign_up,
    upload_one,
    wait_for_terminal,
)

BASE = "http://localhost:8080"
passed = 0
failed = 0


def check(ok: bool, label: str, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  [PASS] {label}" + (f" — {detail}" if detail else ""))
    else:
        failed += 1
        print(f"  [FAIL] {label}" + (f" — {detail}" if detail else ""))


def ingest(client: httpx.Client, s: Session, image: bytes) -> str:
    up, key = upload_one(s, image)
    r = client.post(
        "/garments/ingest",
        json={"upload_ids": [up], "keys": [key]},
        headers={**s.auth, "Idempotency-Key": str(uuid.uuid4())},
    )
    assert r.status_code == 202, r.text
    jid = r.json()["job_ids"][0]
    wait_for_terminal(s, jid, timeout=180.0)
    return str(jid)


def garments(client: httpx.Client, s: Session) -> list[dict[str, Any]]:
    r = client.get("/garments", headers=s.auth, params={"limit": 100})
    body = r.json()
    return body if isinstance(body, list) else body.get("items", [])


def main() -> int:
    with httpx.Client(base_url=BASE, timeout=120) as client:
        s = sign_up(client)

        # ---------------------------------------------------------------
        print("\n== dedupe: the same photo twice is proposed, never merged ==")
        # ---------------------------------------------------------------
        same = flat_lay_jpeg(31337)
        ingest(client, s, same)
        ingest(client, s, same)
        items = garments(client, s)
        suspects = [g for g in items if g.get("state") == "duplicate_suspect"]
        check(
            len(suspects) == 1, "the second upload is flagged", f"{len(suspects)} of {len(items)}"
        )
        check(
            len([g for g in items if g.get("state") != "duplicate_suspect"]) >= 1,
            "the first upload is NOT flagged",
        )

        dupes = client.get("/wardrobe/duplicates", headers=s.auth).json()["items"]
        check(len(dupes) == 1, "the proposal is listed for review", f"{len(dupes)} pending")
        if dupes:
            check(
                dupes[0]["duplicate_of"]["id"] != dupes[0]["garment_id"],
                "the proposal points at a DIFFERENT garment",
            )

        # ---------------------------------------------------------------
        print("\n== wear log ==")
        # ---------------------------------------------------------------
        keep = next(g for g in items if g.get("state") != "duplicate_suspect")
        gid = keep["id"]
        r1 = client.post(f"/garments/{gid}/wear", json={}, headers=s.auth).json()
        check(r1["total_wears"] == 1, "first wearing recorded", f"total={r1['total_wears']}")
        check(r1["already_logged"] is False, "first tap is not a duplicate")

        r2 = client.post(f"/garments/{gid}/wear", json={}, headers=s.auth).json()
        check(r2["already_logged"] is True, "second tap the same day is idempotent")
        check(r2["total_wears"] == 1, "count did not double", f"total={r2['total_wears']}")

        yesterday = (date.today() - timedelta(days=1)).isoformat()
        r3 = client.post(
            f"/garments/{gid}/wear", json={"worn_on": yesterday}, headers=s.auth
        ).json()
        check(r3["total_wears"] == 2, "a different day is a separate wearing")

        future = (date.today() + timedelta(days=3)).isoformat()
        rf = client.post(f"/garments/{gid}/wear", json={"worn_on": future}, headers=s.auth)
        check(rf.status_code == 400, "a future wearing is refused", f"HTTP {rf.status_code}")

        hist = client.get(f"/garments/{gid}/wears", headers=s.auth).json()
        check(hist["total_wears"] == 2, "history agrees with the counter")

        client.request("DELETE", f"/garments/{gid}/wear/{yesterday}", headers=s.auth)
        hist2 = client.get(f"/garments/{gid}/wears", headers=s.auth).json()
        check(hist2["total_wears"] == 1, "a mis-tap can be undone", f"now {hist2['total_wears']}")

        # ---------------------------------------------------------------
        print("\n== laundry ==")
        # ---------------------------------------------------------------
        det = client.get(f"/garments/{gid}/detail", headers=s.auth).json()["garment"]
        check(det.get("needs_wash") is True, "wearing it puts it in the basket")
        client.patch(f"/garments/{gid}/laundry", json={"needs_wash": False}, headers=s.auth)
        det2 = client.get(f"/garments/{gid}/detail", headers=s.auth).json()["garment"]
        check(det2.get("needs_wash") is False, "washing it takes it back out")

        # ---------------------------------------------------------------
        print("\n== search + filters ==")
        # ---------------------------------------------------------------
        allr = client.get("/garments/search", headers=s.auth).json()
        check(allr["total"] >= 1, "search with no query returns the wardrobe", f"{allr['total']}")
        check(
            all(i["state"] != "rejected" for i in allr["items"]),
            "rejected garments are excluded",
        )

        sub = det2.get("subcategory")
        if sub:
            q = client.get("/garments/search", headers=s.auth, params={"q": sub}).json()
            check(q["total"] >= 1, f"full-text finds {sub!r}", f"{q['total']} hit(s)")
            check(
                all(i["rank"] is not None for i in q["items"]),
                "ranked results carry a rank",
            )

        bad = client.get("/garments/search", headers=s.auth, params={"slot": "not_a_slot"})
        check(
            bad.status_code == 400,
            "an unknown enum filter is a 400, not a 500",
            f"HTTP {bad.status_code}",
        )

        colour = det2.get("primary_colour")
        if colour:
            f1 = client.get(
                "/garments/search", headers=s.auth, params={"primary_colour": colour}
            ).json()
            check(
                all(i["primary_colour"] == colour for i in f1["items"]),
                f"colour filter returns only {colour}",
            )

        wash = client.get("/garments/search", headers=s.auth, params={"needs_wash": "true"}).json()
        check(
            all(i["needs_wash"] for i in wash["items"]),
            "needs_wash filter returns only unwashed",
        )

        fac = client.get("/wardrobe/facets", headers=s.auth).json()
        check("flags" in fac and fac["flags"]["total"] >= 1, "facets report wardrobe totals")
        check(
            fac["flags"]["duplicate_suspect"] == 1,
            "facets count the pending duplicate",
            str(fac["flags"]["duplicate_suspect"]),
        )

        # ---------------------------------------------------------------
        print("\n== most-worn (the onboarding ranking) ==")
        # ---------------------------------------------------------------
        mw = client.get("/wardrobe/most-worn", headers=s.auth, params={"limit": 20}).json()
        check(len(mw["items"]) >= 1, "most-worn returns a ranking")
        check(mw["items"][0]["id"] == gid, "the garment we wore ranks first")
        check(mw["items"][0]["wears"] == 1, "with the right count")
        counts = [i["wears"] for i in mw["items"]]
        check(counts == sorted(counts, reverse=True), "ranking is descending by wears")

        # ---------------------------------------------------------------
        print("\n== resolving the duplicate ==")
        # ---------------------------------------------------------------
        suspect_id = suspects[0]["id"]
        client.post(f"/garments/{suspect_id}/wear", json={}, headers=s.auth)
        res = client.post(
            f"/garments/{suspect_id}/duplicate-resolution",
            json={"resolution": "same"},
            headers=s.auth,
        )
        check(res.status_code == 200, "merge accepted", f"HTTP {res.status_code}")
        body = res.json()
        check(body["merged_into"] is not None, "merge names the surviving garment")
        after = garments(client, s)
        check(
            not any(g["id"] == suspect_id for g in after),
            "the merged garment leaves the wardrobe",
        )
        kept = client.get(f"/garments/{body['merged_into']}/wears", headers=s.auth).json()
        check(
            kept["total_wears"] >= 1,
            "its wear history moved to the survivor",
            f"{kept['total_wears']} wear(s)",
        )

        again = client.post(
            f"/garments/{suspect_id}/duplicate-resolution",
            json={"resolution": "same"},
            headers=s.auth,
        )
        check(
            again.status_code == 404,
            "an already-resolved proposal cannot be re-resolved",
            f"HTTP {again.status_code}",
        )

    print(f"\n{passed}/{passed + failed} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
