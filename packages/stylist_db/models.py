"""SQLAlchemy models.

Every user-scoped table carries `user_id uuid not null` plus `created_at` /
`updated_at`, and has RLS enabled with a tenant_isolation policy (see the
initial migration). The `user_id` column is what the policy keys on, so it is
never nullable and never updated after insert.

Enum types are created by the migration from config/taxonomy.yaml, so every
ENUM here is declared with `create_type=False` — SQLAlchemy must not try to
create or drop them.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import ARRAY, ENUM, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from stylist_domain.taxonomy import load_taxonomy

# Enum types are CREATED by the migration (create_type=False) but their VALUES
# are declared here, from taxonomy.yaml.
#
# Declaring the name alone is not enough, and the failure is delayed and
# confusing: SQLAlchemy can happily WRITE a value it does not know, then throws
#   LookupError: 'lower' is not among the defined enum values.
#   Enum name: slot. Possible values: None
# when READING the row back. It stayed hidden through Phases 1-2 because every
# enum column was NULL until segmentation started setting `slot` — so the first
# symptom was the wardrobe endpoint 500ing on a garment that had saved fine.
#
# Values come from the same loader that generates the Postgres types, so the
# ORM and the database cannot drift.
_taxonomy = load_taxonomy()

SlotEnum = ENUM(*_taxonomy.slots, name="slot", create_type=False)
SubcategoryEnum = ENUM(*_taxonomy.subcategories, name="subcategory", create_type=False)
ColourEnum = ENUM(*_taxonomy.colours, name="colour", create_type=False)
MaterialEnum = ENUM(*_taxonomy.materials, name="material", create_type=False)
PatternEnum = ENUM(*_taxonomy.patterns, name="pattern", create_type=False)
FitEnum = ENUM(*_taxonomy.fits, name="fit", create_type=False)
DressCodeEnum = ENUM(*_taxonomy.dress_codes, name="dress_code", create_type=False)
ClimateBandEnum = ENUM(*_taxonomy.climate_bands, name="climate_band", create_type=False)


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


def _updated_at() -> Mapped[datetime]:
    return mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class User(Base):
    """Not user-scoped in the RLS sense — this table IS the tenant list.

    `deleted_at` is set at step 1 of the erasure saga (§C5) and the API returns
    410 from that moment; the actual row deletion happens at step 6, up to 30
    days later. So "deleted" must always be checked as `deleted_at IS NULL`,
    never as row absence.
    """

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _pk()
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class UserProfile(Base):
    __tablename__ = "user_profile"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    display_name: Mapped[str | None] = mapped_column(String(120))
    # Weather coordinates are stored ROUNDED TO 2DP. Full precision plus a
    # timestamp is a home address; 2dp is ~1.1km, which is all a forecast needs.
    home_lat_2dp: Mapped[float | None] = mapped_column(Numeric(5, 2))
    home_lon_2dp: Mapped[float | None] = mapped_column(Numeric(5, 2))
    timezone: Mapped[str] = mapped_column(String(64), nullable=False, server_default="Asia/Kolkata")
    locale: Mapped[str] = mapped_column(String(16), nullable=False, server_default="en-IN")
    preference_facts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    # The tenant's LiteLLM virtual key. Nullable: a gateway outage at signup
    # must not block registration, and the tag stage degrades for a tenant
    # without one.
    litellm_key: Mapped[str | None] = mapped_column(Text)
    litellm_budget_usd: Mapped[float | None] = mapped_column(Numeric(10, 4))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()


class Garment(Base):
    """One physical garment.

    Taxonomy columns are nullable because a garment becomes VISIBLE to the user
    at `CLASSIFIED`, before the VLM has filled in material/formality — partial
    usefulness is a deliberate property of the ingest state machine (View 5),
    not an accident. `is_active=false` is the soft-delete used by the wardrobe.
    """

    __tablename__ = "garments"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # provenance
    original_key: Mapped[str] = mapped_column(Text, nullable=False)
    cutout_key: Mapped[str | None] = mapped_column(Text)
    phash: Mapped[str | None] = mapped_column(String(64))

    # taxonomy — every one of these is an enum sourced from taxonomy.yaml
    slot: Mapped[str | None] = mapped_column(SlotEnum)
    subcategory: Mapped[str | None] = mapped_column(SubcategoryEnum)
    primary_colour: Mapped[str | None] = mapped_column(ColourEnum)
    secondary_colour: Mapped[str | None] = mapped_column(ColourEnum)
    pattern: Mapped[str | None] = mapped_column(PatternEnum)
    material: Mapped[str | None] = mapped_column(MaterialEnum)
    fit: Mapped[str | None] = mapped_column(FitEnum)
    dress_code: Mapped[str | None] = mapped_column(DressCodeEnum)
    climate_bands: Mapped[list[str] | None] = mapped_column(ARRAY(ClimateBandEnum))
    formality: Mapped[int | None] = mapped_column(SmallInteger)
    warmth: Mapped[int | None] = mapped_column(SmallInteger)

    # ML lifecycle (§D1). attributes_raw holds the untouched VLM response so a
    # backfill can reprocess WITHOUT re-calling the provider.
    attributes_raw: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    field_confidence: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    # Fields the user corrected by hand. NEVER overwritten by a backfill.
    user_verified_fields: Mapped[list[str]] = mapped_column(
        ARRAY(Text), nullable=False, server_default="{}"
    )
    extractor_version: Mapped[str | None] = mapped_column(String(32))
    embedding_version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # 768 dims, L2-normalised, from Marqo-FashionSigLIP's vision tower.
    #
    # Nullable and staying nullable: a garment is visible to the user before it
    # is embedded, and a placeholder vector would be worse than a null because
    # it would participate in similarity search and match things.
    embedding: Mapped[list[float] | None] = mapped_column(Vector(768))

    # NSFW score, verdict and model from the moderate gate. Held on the
    # garment because audit_log deliberately stores no per-image detail.
    moderation: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")

    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="received")
    needs_review: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")

    # ---- Phase 5 ---------------------------------------------------------
    # Laundry state. Phase 6's suggester excludes what is in the basket.
    needs_wash: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="false")
    # Minor units (paise), never a float: cost-per-wear divides this, and
    # binary floating point accumulates error across a wardrobe.
    purchase_price_minor: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    purchase_currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    # A PROPOSED duplicate, never an applied one. The pipeline asks; only the
    # user answers. Self-referential, so it is typed as the raw column rather
    # than a relationship to avoid a mapper cycle for a field that is read as
    # an id everywhere it is used.
    duplicate_of: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("garments.id", ondelete="SET NULL"), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default="true")

    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        CheckConstraint("formality IS NULL OR formality BETWEEN 1 AND 5", name="ck_formality"),
        CheckConstraint("warmth IS NULL OR warmth BETWEEN 1 AND 5", name="ck_warmth"),
        # The index that actually serves wardrobe retrieval. Note the HNSW
        # vector index (Phase 3) is NOT what makes wardrobe search fast —
        # retrieval filters to one tenant (<=400 rows) and uses this.
        Index("ix_garments_user_slot_active", "user_id", "slot", "is_active"),
        Index("ix_garments_user_created", "user_id", "created_at"),
    )


class Job(Base):
    """Durable state machine, one row per ingest job (View 5).

    `state` is the last SUCCESSFULLY COMPLETED state, so a resumed job skips
    stages it already finished. `attempts` is per-stage, tracked in
    `stage_attempts`, because a VLM failure must never re-run segmentation.
    """

    __tablename__ = "jobs"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False, server_default="ingest")
    state: Mapped[str] = mapped_column(String(32), nullable=False, server_default="received")
    garment_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("garments.id", ondelete="SET NULL")
    )
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    stage_attempts: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    # Set when the job is parked in the DLQ, along with the last good state.
    dlq_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = _updated_at()

    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_jobs_user_idempotency"),
        Index("ix_jobs_user_state", "user_id", "state"),
    )


class Outbox(Base):
    """Transactional outbox (§C1).

    Rows are inserted in the SAME transaction as the state change they describe.
    A relay process polls `sent_at IS NULL` with FOR UPDATE SKIP LOCKED and
    publishes to Redis, giving at-least-once delivery with the DB as source of
    truth. Never insert here outside the originating transaction.

    Deliberately NOT user-scoped for RLS: the relay runs as a background role
    with no tenant context and must see every tenant's rows.
    """

    __tablename__ = "outbox"

    id: Mapped[uuid.UUID] = _pk()
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    created_at: Mapped[datetime] = _created_at()
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Partial index: the relay only ever queries unsent rows, so the index
        # stays small no matter how large the table grows.
        Index(
            "ix_outbox_unsent",
            "created_at",
            postgresql_where=text("sent_at IS NULL"),
        ),
    )


class ProcessedKey(Base):
    """Idempotent consumers (§C3).

    A worker inserts its key here inside the same transaction as its effect, so
    a replay is a no-op: `INSERT ... ON CONFLICT DO NOTHING`, and if the insert
    conflicts the work was already done.
    """

    __tablename__ = "processed_keys"

    idempotency_key: Mapped[str] = mapped_column(String(255), primary_key=True)
    consumer: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = _created_at()


class ModelCall(Base):
    """Mirror of the LiteLLM spend log, so cost joins to our own user/job data.

    Never store prompt or response bodies here — image bytes and raw VLM output
    must not be logged (§D3). Langfuse holds the traces; this table holds money.
    """

    __tablename__ = "model_calls"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    model_name: Mapped[str] = mapped_column(String(128), nullable=False)
    model_version: Mapped[str | None] = mapped_column(String(64))
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    cache_hit: Mapped[bool | None] = mapped_column(Boolean)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()

    __table_args__ = (Index("ix_model_calls_user_created", "user_id", "created_at"),)


class AuditLog(Base):
    """Append-only. Retained through erasure (legally required) and therefore
    must contain NO personal data beyond the pseudonymous user id + timestamps.
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_type: Mapped[str | None] = mapped_column(String(64))
    subject_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default="{}")
    trace_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = _created_at()


# Tables carrying tenant data, guarded by RLS. The migration and
# tests/test_rls_isolation.py both read this list, so adding a user-scoped
# table without a policy fails CI rather than leaking silently.
TENANT_SCOPED_TABLES: tuple[str, ...] = (
    "user_profile",
    "garments",
    "jobs",
    "garment_corrections",
    "wear_log",
)


class WearLog(Base):
    """One row per wearing. Append-only.

    Not a counter on Garment: cost-per-wear, "your 20 most-worn" and "not worn
    since March" are all questions about WHEN, and a counter answers none of
    them. It also makes a mis-tap correctable by deleting a row rather than
    decrementing a number whose history is gone.
    """

    __tablename__ = "wear_log"

    id: Mapped[uuid.UUID] = _pk()
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    garment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("garments.id", ondelete="CASCADE"), nullable=False
    )
    # A DATE. "I wore this on Tuesday" is the fact; a timestamp would imply a
    # precision the user never gave and make "worn today" timezone-dependent.
    worn_on: Mapped[date] = mapped_column(Date, nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = _created_at()
