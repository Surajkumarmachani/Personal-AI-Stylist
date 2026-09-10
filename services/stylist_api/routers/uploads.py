"""Presigned upload issuance.

The client uploads DIRECTLY to object storage and the bytes never transit this
API. That is why the content-type allowlist and the size ceiling have to be
conditions in the presigned policy rather than checks in a handler: there is no
handler in the byte path to check anything.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from stylist_api.deps import CurrentUser, ObjectStoreDep
from stylist_api.schemas import PresignRequest, PresignResponse

router = APIRouter(prefix="/uploads", tags=["uploads"])


@router.post("/presign", response_model=PresignResponse)
async def presign(
    body: PresignRequest,
    user: CurrentUser,
    store: ObjectStoreDep,
) -> PresignResponse:
    try:
        presigned = store.presign_upload(
            user_id=user.id,
            content_type=body.content_type,
            max_bytes=body.max_bytes,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    return PresignResponse(
        upload_id=presigned.upload_id,
        key=presigned.key,
        url=presigned.url,
        fields=presigned.fields,
        expires_at=presigned.expires_at,
        max_bytes=presigned.max_bytes,
    )
