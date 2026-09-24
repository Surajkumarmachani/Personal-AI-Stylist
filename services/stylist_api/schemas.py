"""Request/response models. The OpenAPI spec generated from these is the truth
that clients are contract-tested against (§D2)."""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

from stylist_clients.storage import ALLOWED_CONTENT_TYPES, MAX_UPLOAD_BYTES


class RegisterRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=12, max_length=72)  # 72 = bcrypt's real limit
    # Whose clothes to suggest buying. Optional on the API so older clients
    # still register; the web sign-up form requires it. See migration 0026.
    dresses_as: Literal["women", "men", "all"] | None = None


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
    # IN THE WASH. A hard exclusion in the candidate pool — `AND NOT
    # g.needs_wash` — so a garment with this set silently cannot appear in any
    # suggestion. The wardrobe grid had no way to see or change it, which made
    # "why is my favourite shirt never suggested?" unanswerable from the app.
    needs_wash: bool = False
    # Free text, user-entered, not scored — see migration 0021.
    brand: str | None = None
    size_label: str | None = None
    # Wear count and last worn, so the grid can show what is actually used.
    # `novelty` is 10% of the outfit score and reads exactly these, and until
    # now the only way to log a wear was the build console.
    wear_count: int = 0
    last_worn: date | None = None
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
