"""Object storage adapter (S3 / R2 / MinIO).

WHY PRESIGNED POST AND NOT PRESIGNED PUT
----------------------------------------
The plan says "presigned PUT" and "constrain content-type and max size in the
presigned policy, not just in your handler". Those two requirements conflict:
a presigned PUT URL cannot cap the body size. You can sign a `Content-Length`
header, but then the client must declare its exact byte count up front and any
mismatch is a signature failure rather than a clean rejection — and a client
that simply omits the header gets an unconstrained upload.

`generate_presigned_post` takes a policy document with a real
`content-length-range` condition that S3/MinIO enforces server-side, before
the bytes are stored. Since the whole reason for presigning is that the client
uploads DIRECTLY to storage and never touches our API, the ceiling has to be
enforced by storage or it is not enforced at all.

So: presigned POST, multipart form upload. The client-side change is small and
the security property is real rather than nominal.

TWO ENDPOINTS, NOT ONE
----------------------
`endpoint_url` is where THIS PROCESS reaches storage. `public_endpoint_url` is
the host baked into URLs handed to clients. In production they are the same
(S3/R2 is public DNS). In docker compose they are NOT: the API talks to
`http://minio:9000` over the compose network, while the browser or phone
resolves `http://localhost:9000`. Signing with the internal name produces a URL
the client cannot resolve at all — and note this matters for correctness, not
just reachability: presigned GET signs the Host header, so a download URL
generated against the wrong endpoint fails its signature check even if the
client can somehow route to it.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import boto3
from botocore.client import Config

# Only these are accepted. Note the ingest pipeline re-checks the real bytes
# with a magic-byte sniff (Phase 2.3) — a declared content type is a claim by
# the client, not a fact, and this list is defence in depth, not the gate.
ALLOWED_CONTENT_TYPES: frozenset[str] = frozenset(
    {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
)

MAX_UPLOAD_BYTES = 12 * 1024 * 1024  # 12MB, matches the validate stage
MIN_UPLOAD_BYTES = 1024  # reject empty/truncated uploads at the edge


@dataclass(frozen=True, slots=True)
class PresignedUpload:
    upload_id: str
    key: str
    url: str
    fields: dict[str, str]
    expires_at: datetime
    max_bytes: int


class ObjectStore:
    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region: str = "us-east-1",
        access_key: str | None = None,
        secret_key: str | None = None,
        presign_ttl_seconds: int = 900,  # 15 min, per the edge rule in View 1
        public_endpoint_url: str | None = None,
    ) -> None:
        self.bucket = bucket
        self.presign_ttl_seconds = presign_ttl_seconds
        # SigV4 + path addressing so the same code works against MinIO and S3.
        cfg = Config(signature_version="s3v4", s3={"addressing_style": "path"})

        def _client(endpoint: str | None) -> Any:
            return boto3.client(
                "s3",
                endpoint_url=endpoint,
                region_name=region,
                aws_access_key_id=access_key,
                aws_secret_access_key=secret_key,
                config=cfg,
            )

        # Server-side operations: head, bucket config, deletes.
        self._client = _client(endpoint_url)
        # URL generation for clients. Same credentials, client-reachable host.
        # Falls back to the internal client when no public endpoint is set,
        # which is the production case.
        self._signer = _client(public_endpoint_url) if public_endpoint_url else self._client

    def presign_upload(
        self,
        *,
        user_id: uuid.UUID | str,
        content_type: str,
        max_bytes: int = MAX_UPLOAD_BYTES,
    ) -> PresignedUpload:
        if content_type not in ALLOWED_CONTENT_TYPES:
            raise ValueError(f"content_type not allowed: {content_type}")
        if not (MIN_UPLOAD_BYTES < max_bytes <= MAX_UPLOAD_BYTES):
            raise ValueError(f"max_bytes must be in ({MIN_UPLOAD_BYTES}, {MAX_UPLOAD_BYTES}]")

        upload_id = str(uuid.uuid4())
        # Tenant-prefixed key. Makes the erasure saga's "delete all objects for
        # this user" a prefix operation rather than a table scan (§C5 step 4).
        key = f"originals/{user_id}/{upload_id}"

        post = self._signer.generate_presigned_post(
            Bucket=self.bucket,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", MIN_UPLOAD_BYTES, max_bytes],
            ],
            ExpiresIn=self.presign_ttl_seconds,
        )
        return PresignedUpload(
            upload_id=upload_id,
            key=key,
            url=post["url"],
            fields=post["fields"],
            expires_at=datetime.now(UTC) + timedelta(seconds=self.presign_ttl_seconds),
            max_bytes=max_bytes,
        )

    def presign_download(self, key: str, *, ttl_seconds: int | None = None) -> str:
        """Short-lived read URL. No object is ever public (View 1)."""
        return cast(
            str,
            self._signer.generate_presigned_url(
                "get_object",
                Params={"Bucket": self.bucket, "Key": key},
                ExpiresIn=ttl_seconds or self.presign_ttl_seconds,
            ),
        )

    def head(self, key: str) -> dict[str, Any] | None:
        """Confirm an upload actually landed, and get its real size/type.

        Ingest calls this before creating work: a presign handed out is not
        evidence that bytes exist.
        """
        try:
            return cast(dict[str, Any], self._client.head_object(Bucket=self.bucket, Key=key))
        except self._client.exceptions.ClientError:
            return None

    def get_bytes(self, key: str) -> bytes:
        """Read an object whole.

        Fine for garment photos (<=12MB, capped in the presigned policy) and
        much simpler than streaming. Do NOT reuse this for anything unbounded:
        the cap is what makes reading into memory safe here.
        """
        resp = self._client.get_object(Bucket=self.bucket, Key=key)
        return cast(bytes, resp["Body"].read())

    def put_bytes(self, key: str, data: bytes, *, content_type: str) -> None:
        """Write a derived object (sanitised intermediate, cutout).

        Derived objects are regenerable from the original, which is why DR
        replicates originals but not these — cutouts are cheaper to recompute
        than to store twice in two regions (§C7).
        """
        self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType=content_type)

    def ensure_bucket(self, *, versioning: bool = True) -> None:
        """Local/dev convenience. In real environments the bucket is Terraform.

        Versioning matters beyond durability: erasure step 4 must delete ALL
        VERSIONS, because a plain delete against a versioned bucket leaves a
        recoverable version, which is not erasure.
        """
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except self._client.exceptions.ClientError:
            self._client.create_bucket(Bucket=self.bucket)
        if versioning:
            self._client.put_bucket_versioning(
                Bucket=self.bucket, VersioningConfiguration={"Status": "Enabled"}
            )

    def delete_all_versions(self, prefix: str) -> int:
        """Delete every object under `prefix`, INCLUDING ALL VERSIONS.

        THIS IS NOT `delete_object` IN A LOOP, and the difference is the whole
        point of §C5 step 4. The bucket has versioning ON (Phase 1 turned it on
        deliberately, so a bad migration cannot destroy a user's photos), which
        means `delete_object` writes a DELETE MARKER and leaves the bytes
        recoverable. To a developer reading the code that looks like deletion.
        To a regulator it is not erasure, and to anyone with console access the
        photos are two clicks away.

        So this enumerates `list_object_versions` — both `Versions` and
        `DeleteMarkers`, because a marker left behind still records that an
        object existed at that key — and deletes each by VersionId.

        Also aborts multipart uploads: an interrupted upload leaves parts that
        belong to no object, are invisible to `list_objects_v2`, and are billed
        and retained until someone notices.

        Returns the number of versions removed, so the caller can record a
        count rather than asserting success.
        """
        removed = 0
        paginator = self._client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            targets = [
                {"Key": obj["Key"], "VersionId": obj["VersionId"]}
                for kind in ("Versions", "DeleteMarkers")
                for obj in page.get(kind, [])
            ]
            # delete_objects caps at 1000 keys per call.
            for chunk in (targets[i : i + 1000] for i in range(0, len(targets), 1000)):
                if not chunk:
                    continue
                self._client.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": chunk, "Quiet": True}
                )
                removed += len(chunk)

        # Orphaned multipart parts: not objects, not listed, still stored.
        uploads = self._client.list_multipart_uploads(Bucket=self.bucket, Prefix=prefix)
        for upload in uploads.get("Uploads", []):
            self._client.abort_multipart_upload(
                Bucket=self.bucket, Key=upload["Key"], UploadId=upload["UploadId"]
            )
            removed += 1

        return removed

    def count_versions(self, prefix: str) -> int:
        """How many versions exist under `prefix`. Used to VERIFY an erasure.

        The exit criterion is "erasure verified absent", not "erasure ran". A
        count read back after the purge is the difference between the two.
        """
        total = 0
        paginator = self._client.get_paginator("list_object_versions")
        for page in paginator.paginate(Bucket=self.bucket, Prefix=prefix):
            total += len(page.get("Versions", [])) + len(page.get("DeleteMarkers", []))
        return total
