"""Phase 1 exit criterion: presigned upload -> object in the bucket, versioning on.

Run against a live stack (`make up`). Unlike tests/test_presign.py, which
inspects the signed policy offline, this pushes REAL BYTES through the real
presigned POST to real object storage and then verifies from the storage side.
That distinction matters: a policy can look perfect and still be rejected by
the server, and only one of those two checks would notice.

    python scripts/verify_upload_e2e.py

Exits non-zero on the first failed assertion, so it works as a smoke check in
a deploy pipeline as well as by hand.
"""

from __future__ import annotations

import argparse
import base64
import sys
import uuid

import boto3
import httpx
from botocore.client import Config

# A real 1x1 JPEG. Padded to clear MIN_UPLOAD_BYTES (1024) — trailing bytes
# after the EOI marker are tolerated by decoders, and storage does not parse
# the image anyway. Magic-byte validation of real content is Phase 2.3.
TINY_JPEG = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwcJC4nICIsIxwcKDcpMDA1NDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAQAAAAAA"
    "AAAAAAAAAAAAAAX/xAAUEAEAAAAAAAAAAAAAAAAAAAAA/8QAFAEBAAAAAAAAAAAAAAAAAAAAAP/E"
    "ABQRAQAAAAAAAAAAAAAAAAAAAAD/2gAMAwEAAhEDEQA/AKAA/9k="
)
PAYLOAD = TINY_JPEG + b"\x00" * (2048 - len(TINY_JPEG))

results: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    results.append((ok, label))
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        print("\nFAILED. Stopping here rather than reporting a partial pass.")
        sys.exit(1)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--api-url", default="http://localhost:8080")
    ap.add_argument("--s3-endpoint", default="http://localhost:9000")
    ap.add_argument("--bucket", default="stylist-local")
    ap.add_argument("--access-key", default="minioadmin")
    ap.add_argument("--secret-key", default="minioadmin")
    args = ap.parse_args()

    s3 = boto3.client(
        "s3",
        endpoint_url=args.s3_endpoint,
        aws_access_key_id=args.access_key,
        aws_secret_access_key=args.secret_key,
        config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
    )

    print("\n== bucket configuration ==")
    versioning = s3.get_bucket_versioning(Bucket=args.bucket)
    check(
        versioning.get("Status") == "Enabled",
        "bucket versioning is Enabled",
        f"status={versioning.get('Status')!r}",
    )
    # Erasure step 4 must delete ALL VERSIONS; a plain delete against a
    # versioned bucket leaves a recoverable version, which is not erasure.

    try:
        acl_public = False
        policy = s3.get_bucket_policy(Bucket=args.bucket)
        acl_public = '"Effect":"Allow"' in policy.get("Policy", "") and "*" in policy.get(
            "Policy", ""
        )
    except Exception:
        acl_public = False
    check(not acl_public, "bucket is not publicly readable")

    print("\n== api: register + presign ==")
    with httpx.Client(base_url=args.api_url, timeout=30.0) as client:
        email = f"verify-{uuid.uuid4()}@example.com"
        resp = client.post(
            "/auth/register", json={"email": email, "password": "a-long-enough-password"}
        )
        check(resp.status_code == 201, "registered a user", f"HTTP {resp.status_code}")
        token = resp.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}

        resp = client.post("/uploads/presign", json={"content_type": "image/jpeg"}, headers=auth)
        check(resp.status_code == 200, "got a presigned upload", f"HTTP {resp.status_code}")
        presigned = resp.json()
        check(presigned["method"] == "POST", "presign is a POST policy, not a PUT URL")
        key = presigned["key"]

        print("\n== upload real bytes straight to storage ==")
        # Note: no API involvement here at all. This is the client talking
        # directly to object storage, which is exactly why the size ceiling has
        # to live in the signed policy.
        upload = httpx.post(
            presigned["url"],
            data=presigned["fields"],
            files={"file": ("photo.jpg", PAYLOAD, "image/jpeg")},
            timeout=30.0,
        )
        check(
            upload.status_code in (200, 201, 204),
            "storage accepted the upload",
            f"HTTP {upload.status_code}",
        )

        head = s3.head_object(Bucket=args.bucket, Key=key)
        check(head["ContentLength"] == len(PAYLOAD), "object size matches what we sent")
        check(head["ContentType"] == "image/jpeg", "object content-type was preserved")

        versions = s3.list_object_versions(Bucket=args.bucket, Prefix=key)
        check(len(versions.get("Versions", [])) >= 1, "object has a version record")

        print("\n== the size ceiling is enforced BY STORAGE, not by us ==")
        oversized = b"\x00" * (13 * 1024 * 1024)  # 13MB against a 12MB cap
        resp = client.post("/uploads/presign", json={"content_type": "image/jpeg"}, headers=auth)
        p2 = resp.json()
        rejected = httpx.post(
            p2["url"],
            data=p2["fields"],
            files={"file": ("big.jpg", oversized, "image/jpeg")},
            timeout=60.0,
        )
        check(
            rejected.status_code >= 400,
            "13MB upload rejected against a 12MB policy",
            f"HTTP {rejected.status_code}",
        )
        check(
            s3.list_objects_v2(Bucket=args.bucket, Prefix=p2["key"]).get("KeyCount", 0) == 0,
            "the rejected object was never stored",
        )

        print("\n== ingest the uploaded object ==")
        idem = f"verify-{uuid.uuid4()}"
        resp = client.post(
            "/garments/ingest",
            json={"upload_ids": [presigned["upload_id"]], "keys": [key]},
            headers={**auth, "Idempotency-Key": idem},
        )
        check(resp.status_code == 202, "ingest accepted", f"HTTP {resp.status_code}")
        job_ids = resp.json()["job_ids"]

        replay = client.post(
            "/garments/ingest",
            json={"upload_ids": [presigned["upload_id"]], "keys": [key]},
            headers={**auth, "Idempotency-Key": idem},
        )
        check(
            replay.json()["job_ids"] == job_ids,
            "replaying the Idempotency-Key returned the same job id",
        )

        listed = client.get("/garments", headers=auth)
        check(len(listed.json()) == 1, "exactly one garment row exists, not two")

    passed = sum(1 for ok, _ in results if ok)
    print(f"\n{passed}/{len(results)} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
