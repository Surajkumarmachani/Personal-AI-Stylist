"""Request/response models. The OpenAPI spec generated from these is the truth
that clients are contract-tested against (§D2)."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, EmailStr, Field

from stylist_clients.storage import ALLOWED_CONTENT_TYPES, MAX_UPLOAD_BYTES


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=72)  # 72 = bcrypt's real limit


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class RefreshRequest(BaseModel):
    refresh_token: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int


class PresignRequest(BaseModel):
    content_type: str = Field(examples=sorted(ALLOWED_CONTENT_TYPES))
    max_bytes: int = Field(default=MAX_UPLOAD_BYTES, le=MAX_UPLOAD_BYTES, gt=1024)


class PresignResponse(BaseModel):
    """Presigned POST, not PUT — see stylist_clients.storage for why.

    The client POSTs multipart/form-data to `url` with every entry of `fields`
    plus a `file` part. Storage enforces the size ceiling itself.
    """

    upload_id: str
    key: str
    url: str
    fields: dict[str, str]
    method: str = "POST"
    expires_at: datetime
    max_bytes: int


class IngestRequest(BaseModel):
    upload_ids: list[str] = Field(min_length=1, max_length=60)
    keys: list[str] = Field(min_length=1, max_length=60)


class IngestResponse(BaseModel):
    job_ids: list[uuid.UUID]
    accepted: int
    # True when an Idempotency-Key replay returned the original jobs rather
    # than creating new ones.
    idempotent_replay: bool = False


class GarmentSummary(BaseModel):
    id: uuid.UUID
    slot: str | None
    subcategory: str | None
    primary_colour: str | None
    state: str
    needs_review: bool
    cutout_url: str | None
    created_at: datetime


class JobStatus(BaseModel):
    id: uuid.UUID
    kind: str
    state: str
    garment_id: uuid.UUID | None
    last_error: str | None
    created_at: datetime
    updated_at: datetime
