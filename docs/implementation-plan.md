# Personal AI Stylist — Implementation Plan

**Companion to** [architecture.md](architecture.md) v1.0
**Target** 14 weeks, 1–2 engineers · MVP at week 5, V1 at week 10, production at week 12

---

## How this plan is sequenced

Three rules govern the order, and they override "build the diagram top to bottom":

**1. Vertical slices, not horizontal layers.** Every phase ends with something a human can use. You never spend three weeks on infrastructure before the first photo becomes a garment. Phase 2 has a working upload→cutout→grid path with zero AI in it.

**2. Type-1 decisions first, provisional numbers last.** Six decisions are expensive to reverse: taxonomy/enums, tenancy boundary, outbox contract, validator contract, self-hosted CV boundary, event-log-as-truth. Those land in Phases 0–3. Latency allocations, HPA thresholds and instance classes are *provisional* — they're built against invented traffic and get retuned in Phase 9 against real traffic. Don't polish them now.

**3. Validate the riskiest assumption earliest.** The whole product rests on one unproven claim: *garment tagging is accurate enough on our users' actual clothes*. So the golden set is collected in Phase 0 and measured in Phase 3 — before you build anything that depends on tags. If accuracy is 60% on sarees, you want to know in week 3, not week 9.

**Deferred deliberately** (and each phase says what not to build): Kubernetes until Phase 9, VTON until Phase 10, learned compatibility model and trends until Phase 11. Building these early is the most common way this project dies at 80% done.

### Dependency graph

```
P0 taxonomy + golden set --+--> P1 skeleton + tenancy --> P2 ingest (no AI)
                           |                                    |
                           +---------------> P3 CV pipeline <---+
                                                  |
                                                  v
                                        P4 VLM + correction UI
                                                  |
                                                  v
                                       P5 MVP SHIP -- owner's wardrobe
                                                  |
                                                  v
                                 P6 suggest pipeline (deterministic)
                                                  |
                                                  v
                                      P7 LLM rerank + validator
                                                  |
                                                  v
                                    P8 boards + feedback -> V1 SHIP
                                                  |
                     +----------------------------+---------------------+
                     v                            v                     v
             P9 hardening (k8s,          P10 try-on (VTON)      P11 learning
             SLO, DR, erasure)                                  (bandit, trends)
                     |
                     v
             PRODUCTION LAUNCH
```

### Definition of done (applies to every step)

A step is not done until: code merged to main · unit + integration tests passing · works from a clean `docker compose up` · exit criterion demonstrably met (you ran the command and saw the output) · anything provisional is labelled `# PROVISIONAL: retune in P9`.

---

# PHASE 0 — Decide and prepare (3 days)

Nothing here is code. All of it is load-bearing on the schema.

## Step 0.1 — Garment taxonomy workshop (1 day) ⚠ BLOCKING

The single most expensive thing to change later. Do not start Phase 1 without it.

**Produce four frozen enum lists** in `config/taxonomy.yaml`:

```yaml
# 1. SLOTS — what positions an outfit has
slots:
  - id: head            # cap, hat, turban, dupatta-as-headcover
  - id: upper_base      # t-shirt, shirt, kurta, blouse/choli
  - id: upper_layer     # cardigan, blazer, jacket, nehru jacket
  - id: lower           # trousers, jeans, skirt, churidar, dhoti, lehenga
  - id: full_body       # dress, jumpsuit, gown, sherwani, anarkali
  - id: drape           # ⚠ saree, dupatta, stole, shawl — SEE BELOW
  - id: feet
  - id: bag
  - id: accessory

# 2. SLOT LEGALITY — replaces "{top+bottom} or {one_piece}"
outfit_rules:
  - exactly_one_of: [[upper_base, lower], [full_body]]
  - at_most_one: [upper_layer, head, bag]
  - exactly_one: [feet]
  - range: {slot: accessory, min: 0, max: 3}
  - range: {slot: drape, min: 0, max: 2}
  # composite garments: a saree occupies drape AND requires an
  # upper_base (blouse) — model it as a REQUIRES edge, not a slot
  requires:
    - {subcategory: saree, needs_slot: upper_base}
    - {subcategory: lehenga, needs_slot: upper_base}
```

**The decision you have to actually make:** does a saree occupy `full_body` (and the blouse is an implementation detail) or `drape + upper_base`? I recommend the latter, because users own blouses independently and re-pair them, and because your segmentation will produce separate masks anyway. But it's your product call and it must be made now.

**Also freeze:**
- `subcategory` — target ~120 values. Start from ATR's 18 classes, then add the ethnic set explicitly: saree, lehenga, choli, kurta, kurti, anarkali, salwar, churidar, palazzo, dhoti, sherwani, nehru_jacket, dupatta, stole, bandhgala.
- `primary_colour` — 24-value palette. **Do not use a Western palette unmodified.** Add the values that carry meaning in ethnic wear (maroon, mustard, rani_pink, teal, gold, silver, cream, rust, emerald).
- `formality` — 1–5 does not work. Use **two independent axes**: `formality` 1–5 *and* `dress_code` enum (`casual · smart_casual · business · festive_ethnic · formal_ethnic · black_tie · loungewear · activewear`). A mehendi outfit is formality 4, dress_code `festive_ethnic`. Collapsing these to one scale is why generic apps feel wrong in India.
- `seasons` — replace with `climate_bands`: `hot_dry · hot_humid · monsoon · mild · cold`. Bengaluru has no autumn.
- `warmth` 1–5 calibrated to a 16–34°C range, not 0–30°C.

**Exit criterion:** `config/taxonomy.yaml` is committed and reviewed. Take 30 real garments from your target user (or from Machani's catalogue), classify each by hand using only these enums, and confirm **zero** need for an "other" value. If you reach for "other" more than once, the taxonomy isn't done.

## Step 0.2 — Golden set collection (start now, runs in background)

Slow, so start immediately. 500 images, hand-labelled against the Phase 0.1 enums.

Composition — deliberately weighted toward failure:

```
120  clean flat-lay, single garment       (the easy case, your baseline)
 80  on-hanger
 80  worn, single garment visible
 60  worn, MULTI-garment frame (mirror selfie)   <-- the split test
 50  ethnic wear (saree drape, lehenga, kurta set, sherwani)  <-- hard test
 40  dark garment on dark background
 30  pattern on pattern
 20  folded / stacked
 20  low light / backlit
```

Store as `eval/golden/images/*.jpg` + `eval/golden/labels.jsonl`. Two labellers on a 50-image overlap; record inter-annotator agreement. If humans disagree more than 10% on a field, that field is unmeasurable and the model can't be blamed for it.

**Exit criterion:** 500 labelled images committed (LFS or a bucket, referenced by manifest), IAA recorded per field.

See [GOLDEN_SET_SPEC.md](../GOLDEN_SET_SPEC.md) for the full collection, labelling, consent and IAA spec.

## Step 0.3 — Accounts, keys, paperwork (0.5 day)

- Cloud account + a **second account for backups** (Phase 9 needs it; create it now so IAM is set up).
- Provider accounts: one VLM, one text LLM. **Start the DPA process today** — it takes weeks and it blocks nothing until Phase 4, at which point it blocks everything.
- Object storage bucket, versioning ON, public access blocked at account level.
- GitHub repo, branch protection, required checks placeholder.
- Legal: get an actual read on whether body photos are sensitive personal data under DPDP 2023. Blocks Phase 10, not earlier.

---

# PHASE 1 — Skeleton with tenancy (week 1)

Goal: a photo in the bucket, a row in the database, and a CI test that proves tenant A cannot see tenant B.

## Step 1.1 — Monorepo layout

```
ai-stylist/
├── config/taxonomy.yaml            # Phase 0 output — read at runtime
├── services/
│   ├── api/            FastAPI · routers, deps, schemas
│   ├── worker/         arq · jobs, stages, state machine
│   └── ml/             FastAPI · ONNX runtime, no DB access
├── packages/
│   ├── db/             SQLAlchemy models, Alembic, RLS helpers
│   ├── domain/         pure logic: scoring, slot rules, taxonomy loader
│   └── clients/        litellm, storage, weather — thin adapters
├── web/                Next.js
├── eval/               golden set + harness
├── infra/
│   ├── compose/        docker-compose.yml + .dev + .prod
│   ├── litellm/        config.yaml
│   └── k8s/            (empty until Phase 9)
└── .github/workflows/
```

`packages/domain` has **zero** imports from db, api, or clients. It's pure functions over dataclasses. This is what makes the scorer and slot rules unit-testable without a database, and it's the discipline that keeps the deterministic path independent of the AI path.

## Step 1.2 — Local stack

`infra/compose/docker-compose.yml`:

```yaml
services:
  postgres:            # pgvector/pgvector:pg16
  redis-queue:         # appendonly yes, maxmemory-policy noeviction
  redis-cache:         # maxmemory 512mb, maxmemory-policy allkeys-lru
  minio:               # S3-compatible, versioning enabled on bucket
  litellm:             # ghcr.io/berriai/litellm:main-stable, own postgres db
  api:                 # uvicorn --reload
  worker:              # arq
  ml:                  # uvicorn, model weights mounted from ./models volume
```

**Two separate Redis containers, not two logical dbs on one.** The spec says logical dbs; in local dev use two containers so an eviction bug is impossible to hide. In production, two logical dbs on a managed instance is fine — but configure `noeviction` on the queue db explicitly.

## Step 1.3 — First migration

Tables: `users`, `user_profile`, `garments`, `jobs`, `outbox`, `processed_keys`, `model_calls`, `audit_log`.

Enums come from `taxonomy.yaml` and are generated into the migration — **do not hand-type them twice**. Write a small generator so the YAML is the single source of truth:

```python
# packages/db/taxonomy_enums.py
# reads config/taxonomy.yaml -> emits CREATE TYPE ... AS ENUM (...)
# Alembic migration calls this. YAML is the source of truth, forever.
```

Every user-scoped table gets `user_id uuid not null` and `created_at`, `updated_at`.

## Step 1.4 — RLS, and the test that matters most

```sql
ALTER TABLE garments ENABLE ROW LEVEL SECURITY;
ALTER TABLE garments FORCE ROW LEVEL SECURITY;   -- applies to table owner too
CREATE POLICY tenant_isolation ON garments
  USING (user_id = current_setting('app.user_id')::uuid);
```

Session setup in a request-scoped dependency:

```python
await conn.execute(text("SET LOCAL app.user_id = :uid"), {"uid": user.id})
```

`SET LOCAL` — scoped to the transaction, so a pooled connection can't leak the previous request's identity. Getting this wrong with `SET` (session-scoped) plus PgBouncer is a real and severe cross-tenant bug.

**`tests/test_rls_isolation.py` — required CI check, never skippable:**

```python
async def test_no_cross_tenant_read(db, tenant_a, tenant_b):
    # insert 3 garments as A, 2 as B
    async with as_tenant(db, tenant_b):
        rows = await db.execute(select(Garment))          # no WHERE clause
        assert {r.user_id for r in rows} == {tenant_b.id}
        assert len(rows) == 2

async def test_no_cross_tenant_update(db, tenant_a, tenant_b):
    async with as_tenant(db, tenant_b):
        res = await db.execute(
            update(Garment).where(Garment.id == a_garment_id).values(...))
        assert res.rowcount == 0    # silently affects nothing — correct

async def test_orm_query_without_filter_is_still_safe(...):
    # the whole point: forgetting `.where(user_id==)` must not leak
```

That third test is the reason RLS exists rather than trusting the ORM. Write it first.

## Step 1.5 — Auth and presigned upload

- JWT: 15m access / 30d refresh, rotation, revocation list in Redis.
- `POST /uploads/presign` → `{upload_id, put_url, expires_at}`. Constrain content-type and max size **in the presigned policy**, not just in your handler — the client uploads directly to storage and never touches your API.
- `POST /garments/ingest {upload_ids[], Idempotency-Key}` → `202 {job_id}`. Idempotency via Redis `SETNX key → job_id`, TTL 24h; a repeat with the same key returns the original `job_id`, not a new job.

**PHASE 1 EXIT CRITERIA**
- [ ] `docker compose up` → healthy in <60s from clean
- [ ] All three RLS tests green, running in CI as a required check
- [ ] Presigned upload from a curl script → object in MinIO with versioning
- [ ] `POST /garments/ingest` twice with the same Idempotency-Key → same `job_id`
- [ ] Taxonomy enums in the DB match `taxonomy.yaml` (assert this in a test)

**DO NOT BUILD YET:** Kubernetes, CDN, WAF, canary deploys, OAuth providers, billing.

---

# PHASE 2 — Ingest vertical slice, zero AI (week 2)

Goal: upload a flat-lay photo, get a background-removed cutout in your wardrobe grid, in under 10 seconds — with a durable pipeline you can kill mid-flight.

## Step 2.1 — Transactional outbox + relay

```python
# packages/db/outbox.py
async def emit(conn, aggregate_id, event_type, payload):
    """MUST be called inside the caller's transaction. No autocommit."""
    await conn.execute(insert(Outbox).values(...))
```

Relay as its own arq task on a 250ms tick:

```sql
SELECT * FROM outbox WHERE sent_at IS NULL
  ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 100;
```

Publish to the Redis stream, then `UPDATE outbox SET sent_at = now()`. `SKIP LOCKED` means N relay replicas need no coordination.

**Test that proves it works:** open a transaction, insert garment + outbox, `kill -9` the API process before commit. Assert: no garment row, no outbox row, no job. Then do it after commit but before the relay ticks. Assert: garment exists, and the job runs when the relay comes back. That second test is the entire justification for the outbox — run it and see it pass.

## Step 2.2 — Job state machine

```python
# services/worker/state_machine.py
class IngestState(str, Enum):
    RECEIVED = "received"; VALIDATED = "validated"; MODERATED = "moderated"
    SEGMENTED = "segmented"; MATTED = "matted"; CLASSIFIED = "classified"
    TAGGED = "tagged"; EMBEDDED = "embedded"; DEDUPED = "deduped"
    COMPLETE = "complete"
    REJECTED = "rejected"; QUARANTINED = "quarantined"
    NEEDS_REVIEW = "needs_review"; DEGRADED_TAGGED = "degraded_tagged"
    DUPLICATE_SUSPECT = "duplicate_suspect"

STAGES = [validate, moderate, segment, matte, classify, tag, embed, dedupe]

async def run(job_id):
    job = await load(job_id)
    for stage in STAGES:
        if stage.enters_state <= job.state:   # already done, skip
            continue
        try:
            result = await with_retry(stage, job)      # per-STAGE retry
        except Terminal as e:
            return await transition(job, e.state, reason=str(e))
        except Exhausted as e:
            return await to_dlq(job, last_good=job.state, error=e)
        await transition(job, stage.enters_state, result)
```

**Per-stage retry, not per-job.** A VLM timeout must never re-run segmentation — that's how a transient provider blip becomes a 6× CPU bill. Backoff: base 2s, full jitter, max 3 attempts.

## Step 2.3 — Stages 1, 2, 5 (validate, moderate, matte)

- **validate**: magic-byte sniff (never trust extension or content-type), ≤12MB, `Image.MAX_IMAGE_PIXELS` guard against decode bombs, min side ≥ 200.
- **sanitise**: EXIF strip. Test explicitly that a photo with GPS EXIF comes out with none — this is a privacy control, so it gets a test.
- **moderate**: stub returning `pass` for now, but *wire the stage in already*. Retrofitting a gate before the third-party calls is harder than leaving a no-op in place.
- **matte**: `rembg` (u2net) → RGBA cutout, trim to content bbox, write to `cutouts/`.
- **persist**: garment row with placeholder taxonomy values + `state`, in one transaction with the outbox emit.

## Step 2.4 — Progress streaming and grid UI

`GET /jobs/{id}/events` (SSE) emitting state transitions. Next.js wardrobe grid rendering cutouts from signed URLs, with per-item state badges (`processing` / `ready` / `needs review`).

**PHASE 2 EXIT CRITERIA**
- [ ] Flat-lay photo → cutout visible in grid, **p95 under 10s**
- [ ] Crash-before-commit test passes (no orphan garment)
- [ ] Crash-mid-pipeline: restart worker, job resumes from last state, does not re-run completed stages
- [ ] EXIF GPS stripped (asserted in test)
- [ ] Poison image (corrupt JPEG) → 3 attempts → DLQ → alert fires, other jobs unaffected
- [ ] 50-photo batch upload completes, queue drains, no duplicate rows

**DO NOT BUILD YET:** segmentation, VLM, embeddings, recommendations.

---

# PHASE 3 — CV pipeline and the accuracy verdict (week 3)

Goal: multi-garment split from one photo, embeddings in pgvector, **and a measured accuracy number on the golden set**. This phase is where you find out whether the product is viable.

## Step 3.1 — `services/ml` inference service

FastAPI, ONNX Runtime, no database access at all. Endpoints:

```
POST /segment   {image_url}          -> {masks: [{slot_hint, mask_png, area_pct}]}
POST /matte     {image_url, mask?}   -> {cutout_png}
POST /embed     {image_url}          -> {vector: [768]}
GET  /models                         -> loaded model names + versions + checksums
```

Models load from a mounted volume, **not baked into the image**. Export SegFormer and FashionSigLIP to ONNX in a one-off script under `scripts/export_models.py`; check the ONNX files into a bucket, not git.

## Step 3.2 — Segmentation and the split

SegFormer-B2/B3-clothes (ATR, 18 classes) → per-class masks. Map ATR classes to your Phase 0 slots via an explicit table in `taxonomy.yaml`:

```yaml
atr_to_slot:
  Upper-clothes: upper_base
  Dress:         full_body
  Pants:         lower
  Skirt:         lower
  Hat:           head
  Left-shoe:     feet
  Right-shoe:    feet          # merge L/R into one garment
  Bag:           bag
  Scarf:         drape         # ⚠ closest ATR has to a dupatta
```

Reject masks < 2% of frame area or IoU-duplicated. Zero masks → `NEEDS_REVIEW` with a manual crop UI, never a silent failure.

**⚠ This is where the ethnic-wear risk lands.** ATR has never seen a saree drape. Expect `Scarf`/`Dress`/`Skirt` to fire in confusing combinations. Measure it on the 50 ethnic-wear golden images before deciding what to do. Options, in increasing cost: (a) accept manual crop for drapes and make that UI good; (b) add a binary drape-detector on top; (c) fine-tune SegFormer on a few hundred hand-masked drape images. **Do not pick one before you have the number.**

## Step 3.3 — Cheap classifier

No neural net needed. `classify` stage does:
- Colour: k-means in CIELAB on the cutout's non-transparent pixels → nearest of your 24 palette colours; second cluster → `secondary_colour` if >15% of area.
- Category/slot: the ATR mask class from segmentation already gives you this for free on worn photos. For flat-lay, a small logistic head on the FashionSigLIP embedding trained on ~2k labelled examples (or zero-shot text-probe against your subcategory list — good enough to start).

~15ms, zero cost. This is the stage that makes the two-tier extraction economics work.

## Step 3.4 — Embeddings and pgvector

Marqo-FashionSigLIP → `vector(768)`, cosine.

```sql
CREATE INDEX CONCURRENTLY garments_embedding_hnsw
  ON garments USING hnsw (embedding vector_cosine_ops)
  WITH (m = 16, ef_construction = 64);
CREATE INDEX ON garments (user_id, slot, is_active);
```

Note in a code comment: wardrobe retrieval filters to one tenant (≤400 rows) so the HNSW index is not load-bearing for that path. It exists for future cross-tenant style similarity. Don't let anyone "optimise" it away, and don't let anyone believe it's what makes wardrobe search fast.

## Step 3.5 — The eval harness ⚠ THE POINT OF THIS PHASE

```
eval/
├── golden/{images/, labels.jsonl}
├── run_eval.py          # -> per-field accuracy, confusion matrices, per-slice
└── baselines/           # committed JSON results, one per run
```

`python eval/run_eval.py --extractor-version v0.1` outputs per-field accuracy **sliced by golden-set category** (flat-lay / worn / multi-garment / ethnic / dark-on-dark / …). The overall number is nearly useless; the slice table is the product decision.

Wire it into CI as a required check with a floor. It will fail on day one — set the floor to your measured baseline and ratchet it up, never down.

**PHASE 3 EXIT CRITERIA**
- [ ] Mirror selfie → ≥3 correctly separated cutouts
- [ ] `run_eval.py` produces the slice table; baseline committed
- [ ] **Explicit go/no-go on ethnic wear** — segmentation accuracy on those 50 images, and a decision recorded on which of (a)/(b)/(c) you're taking
- [ ] Embeddings in pgvector; "find similar to this shirt" returns sane results
- [ ] `ml` service p95 < 800ms per image on the target instance
- [ ] Eval gate in CI with a floor

---

# PHASE 4 — VLM tagging and correction UI (week 4)

## Step 4.1 — LiteLLM gateway

Deploy it, and route **every** model call through it from the first line of code — including self-hosted embeddings, priced at zero. Uniform observability and a single egress path is the whole point, and retrofitting it later means touching every call site.

`infra/litellm/config.yaml`:

```yaml
model_list:
  - model_name: vlm-tagger
    litellm_params: {model: <provider>/<model>, api_key: os.environ/...}
  - model_name: reranker
    litellm_params: {model: <provider>/<model>}
  - model_name: embed-fashion              # self-hosted, budget-exempt
    litellm_params:
      model: openai/fashion-siglip
      api_base: http://ml:8000/v1
      input_cost_per_token: 0
      output_cost_per_token: 0
general_settings:
  database_url: os.environ/LITELLM_DB
  store_model_in_db: true       # hot-swap models without redeploy
litellm_settings:
  cache: true
  cache_params: {type: redis, host: redis-cache}
  callbacks: ["langfuse"]
  num_retries: 2
  fallbacks: [{"vlm-tagger": ["vlm-tagger-backup"]}]
```

One virtual key per tenant, created at signup, `max_budget` by plan, monthly reset. Mirror LiteLLM's spend log into your `model_calls` table via webhook or a nightly sync so cost joins to your own user/job data.

## Step 4.2 — Batched VLM tagging

Compose up to 6 cutouts into a labelled grid (`A1 A2 A3 / B1 B2 B3`), one call, strict JSON schema keyed by cell:

```python
SCHEMA = {"type":"object","properties":{"items":{"type":"array","items":{
  "type":"object","required":["cell","subcategory","material","formality",
                              "dress_code","warmth","fit","climate_bands",
                              "confidence"],
  "properties":{ ... enum-constrained from taxonomy.yaml ... }}}}}
```

Enums in the schema are **generated from `taxonomy.yaml`**, same source as the DB enums. A model returning a value outside the enum is then a schema violation, caught mechanically rather than corrupting a row.

Store the untouched response in `attributes_raw`. Set `extractor_version`. Per-field `confidence` → fields below threshold get `needs_review = true`.

## Step 4.3 — Correction UI

Every field editable. On save, the field name goes into `user_verified_fields` and is **never** overwritten by a backfill.

Emit `garment.field_corrected{field, from, to}` for every correction. This is simultaneously your live accuracy metric and free labelled training data — the two most valuable telemetry events in the product.

## Step 4.4 — Real moderation

Replace the Phase 2 stub. NSFW + non-garment classifier, local. Flag → `QUARANTINED`, audit entry, **no third-party call ever made**. Test with a non-garment image and assert zero outbound provider requests.

**PHASE 4 EXIT CRITERIA**
- [ ] Golden-set accuracy meets the Phase 0 target (≥92% on category + primary colour; state your own floor for material/formality)
- [ ] **Measured cost per garment ingested**, end to end, from `model_calls`
- [ ] Batching verified: 6 garments = 1 VLM call (assert call count in a test)
- [ ] Kill the VLM provider → ingest still reaches `DEGRADED_TAGGED`, item usable
- [ ] Exhaust a tenant's LiteLLM budget → cataloguing still works via zero-cost models
- [ ] Correction events flowing to the quality dashboard
- [ ] Non-garment upload → quarantined, zero outbound calls

---

# PHASE 5 — MVP ship (week 5)

Goal: one real wardrobe — the owner's — catalogued in full. No recommendations yet.

**REVISED 2026-09-15: single-user, from 20.** This was written as a startup
validation bar, and the product is a personal stylist for its owner. Twenty
strangers' wardrobes answered "will this work for people who are not me", which
is not a question this project is asking. The honest consequence is recorded in
the exit criteria below: n=1 measures whether the pipeline is CORRECT, never
whether it GENERALISES. Any accuracy number from here describes one person's
clothes, one camera and one reading of the taxonomy, and must be quoted that
way.

- **Dedupe**: perceptual hash + embedding cosine > 0.95 within tenant → `DUPLICATE_SUSPECT`, ask the user. Never auto-merge.
- **Wear log + laundry state**: `worn_on`, `needs_wash`, cost-per-wear.
- **Search + filters**: text (BM25 over `search_text`) + slot/colour/dress_code/climate filters + "find similar" via vector.
- **Observability, minimum viable**: OTel traces with a span per stage; Langfuse wired; 4 dashboards; and exactly **four alerts** — DLQ age > 1h, daily spend > 2× trailing mean, ingest success rate < 99%, API 5xx rate. Resist adding more; unactionable alerts train people to ignore pages.
- **Onboarding**: "start with your 20 most-worn" flow. Every comparison review says this is what separates users who stick from users who abandon.

**PHASE 5 EXIT CRITERIA**
- [ ] 1 user (the owner), **entire real wardrobe** ingested — not a sample. The count is whatever you own; "all of it" is the bar, because a wardrobe cherry-picked for photogenic items is the same self-selection that made 20 strangers worth asking for in the first place.
- [ ] p95 ingest-to-`CLASSIFIED` < 60s **on a real burst** — "under real load" is not measurable at n=1. Substitute: ingest the wardrobe in one sitting and read p95 off `/ops/dashboards`. This tests the queue, not concurrent tenants; RLS and multi-tenancy stay covered by the test suite, which does not need users.
- [ ] Correction rate per field measured on the owner's real wardrobe (compare to golden set — a big gap means the golden set isn't representative). **At n=1 this is a signal, not a statistic:** it catches a field that is systematically wrong, which is what the ≈20% trigger is for. It cannot distinguish "the model is bad at sarees" from "I own unusual sarees."
- [ ] Zero cross-tenant incidents
- [ ] Cost per user per month measured

**This is the real go/no-go.** If correction rate on real wardrobes is above ~20% on any field, fix ingestion before building recommendations. Everything downstream inherits these errors.

---

# PHASE 6 — Deterministic suggest pipeline (weeks 6–7)

No LLM in this phase. Prove you can produce good outfits with zero model calls.

## Step 6.1 — Context resolution

- Weather: Open-Meteo, **coordinates rounded to 2dp**, cached 1h. Use the forecast endpoint (`/v1/forecast`) with both current conditions and daily/hourly forecast in one call — `?date=tomorrow` needs tomorrow's forecast, and a 07:00 push needs the day ahead, not the reading at 07:00.
- Occasion: explicit user selection first. Calendar integration is Phase 8 — don't couple them.
- `warmth_target` / `formality_target` / `dress_code_target` from `(feels_like, precip, wind, occasion)`. Pure function in `packages/domain`, fully unit-tested.

## Step 6.2 — Candidate generation

```
1  Hard SQL filters: is_active, not in laundry, not worn in N days,
   |warmth - target| <= 1, dress_code compatible, climate_band matches
2  Anchor selection: 8-12 items, mixing style affinity with deliberate
   low-wear-count exploration slots
3  Per anchor, retrieve complements per slot via pgvector within tenant
4  Assemble against the Phase 0 slot rules — including REQUIRES edges
   (saree -> needs an upper_base)
-> 200-500 candidates
```

The slot-rule evaluator lives in `packages/domain/slots.py` and is table-driven from `taxonomy.yaml`. Property-test it: generate random garment sets, assert every accepted outfit satisfies every rule and every rejected one violates at least one.

## Step 6.3 — Deterministic scorer

```python
score = (0.30 * colour_harmony(items)        # ΔE in CIELAB, neutral anchor
       + 0.20 * formality_coherence(items)   # variance across items
       + 0.15 * weather_fit(items, ctx)
       + 0.20 * style_affinity(items, style_vector)   # zeros until P8
       + 0.10 * novelty(items, wear_log)
       + 0.05 * trend_alignment(items))      # zeros until P11
       * hard_penalty(items)
```

Weights in config, not code. Every sub-score returns 0–1 and is separately unit-tested. `score_breakdown` persisted as JSONB — you cannot debug ranking without it.

## Step 6.4 — Nightly precompute + distributed cron

`w-cron` fires nightly precompute per tenant, guarded by:

```sql
SELECT pg_try_advisory_lock(hashtext('nightly_precompute'));
```

Materialise the top ~200 scored outfits into `outfits`. Invalidation on wardrobe change or feedback, driven by outbox events.

**Test:** run three `w-cron` replicas simultaneously; assert the precompute job executes exactly once.

**PHASE 6 EXIT CRITERIA**
- [ ] `GET /suggestions` returns ranked outfits with **zero model calls**, p95 < 300ms
- [ ] Slot-rule property tests green, including composite ethnic garments
- [ ] Nightly precompute: 3 cron replicas → exactly 1 execution
- [ ] Candidate generation < 100ms for a 400-item wardrobe
- [ ] Internal blind eval: 50 outfits, ≥60% "would wear." Below that, fix the scorer — the LLM will not save a bad candidate set. _Revised 2026-09-15 with Phase 5: the raters were "you and 2 others". **Keep the 2 others.** This is the one place the single-user decision should NOT propagate — the rater is judging whether an outfit is wearable, not whose wardrobe it came from, and one rater grading suggestions built from their own clothes has no way to separate "this is a good outfit" from "this is what I would have picked anyway", which is exactly the bias the eval exists to detect. Two outside raters cost an hour and are the cheapest input in this project._

---

# PHASE 7 — LLM rerank and validator (week 8)

## Step 7.1 — Reranker

Top-8 candidates as structured JSON. **No images.** ~1.2k in / 300 out. Strict JSON schema output. Hard timeout at **1200ms** — below the 1500ms SLO, and **no retry inside the request**.

## Step 7.2 — Validator (the six assertions)

Implement all six from §C3 in order, each emitting `validator.reject{rule=...}`:

1. Schema parse → one repair retry with schema echoed → else fallback.
2. **Output garment IDs ⊆ input garment IDs** (exact set membership).
3. Every referenced garment still `is_active` and tenant-owned.
4. Slot legality re-checked against `taxonomy.yaml`.
5. Rationale ≤ 40 words, no URL, no price, no body-shaming term.
6. Confidence ≥ threshold else demote below deterministic order.

**The test that defines this phase:** feed the validator a hand-crafted LLM response containing a fabricated garment ID. Assert it is rejected, the metric increments with `rule="unknown_id"`, and the user receives the deterministic ranking. Then feed one with a valid ID from a *different tenant*. Same outcome.

## Step 7.3 — Rationale cache

Key: `(garment_set_hash, occasion_bucket, temp_bucket, precip_bool)`. **Bucket the temperature** — raw values give a 0% hit rate and blow the latency budget. Track hit rate as an SLI; alert below 50%.

**PHASE 7 EXIT CRITERIA**
- [ ] p95 `/suggestions` ≤ 1500ms with cache warm; ≤ 2500ms cold
- [ ] Kill the reranker provider → suggestions still return, non-5xx, template rationale
- [ ] Fabricated-ID and wrong-tenant-ID tests both pass
- [ ] Rationale cache hit rate ≥ 50% after a week of real traffic
- [ ] `validator.reject` rate < 2% overall
- [ ] Load test: 10× projected peak, no SLO breach

---

# PHASE 8 — Boards, feedback, V1 ship (weeks 9–10)

- **Compositor**: Pillow/Sharp flat-lay from real cutouts. Slot-aware layout, ~80ms, cached in object storage, CDN-served. This is the default visualisation — always pixel-accurate to the user's actual clothes.
- **Feedback capture**: `like / dislike / worn / dismissed / saved` + optional reason enum. Append-only `outfit_feedback`, the source of truth for everything derived.
- **Style vector**: EWMA α≈0.1 over embeddings of liked/worn outfits, decayed on dislike. Recomputable from the event log — write `scripts/rebuild_style_vectors.py` **in this phase** and test it, or you will accumulate unrebuildable state.
- **Preference facts UI**: typed table (`avoids: crop_tops`, `never: yellow`), user-visible and editable. Legibility buys trust faster than accuracy does.
- **Calendar → occasion**: OAuth, classify event title → `dress_code` with a confidence gate. Below threshold, fall back to default **and say so in the UI**. A confidently wrong occasion is worse than asking.
- **Daily push**: 07:00 local, `w-notify`.

**PHASE 8 EXIT CRITERIA**
- [ ] V1 in daily use by the owner for **4 consecutive weeks** — was "200 users", revised 2026-09-15 with Phase 5. Sustained daily use is the single-user substitute for a user count: it is the only way the feedback loop below accumulates enough events to mean anything, and it fails honestly if the product is not actually useful. Note this makes the bar SLOWER, not weaker — 200 users generate 5k feedback events in days, one user takes weeks, and Phase 11 gates on exactly that.
- [ ] Boards render < 200ms p95 from CDN
- [ ] `rebuild_style_vectors.py` reproduces live vectors from the event log (assert equality)
- [ ] **Wear-through rate measured** (suggested → actually worn) — the only quality metric that matters
- [ ] Wardrobe coverage trending up week over week (guards against ranking collapse)

---

# PHASE 9 — Production hardening (weeks 11–12)

Now, with real traffic data, the provisional numbers get retuned.

- **Kubernetes**: namespaces per the topology, **KEDA for queue-depth HPA** (native CPU-based HPA scales up after the burst has drained), PDBs, default-deny NetworkPolicy with LiteLLM as the sole egress. Weather and other allowlisted third parties must be added to the egress policy explicitly.
- **Retune from real data**: replace every `# PROVISIONAL` number — peak factor, pod counts, instance classes, budget ceilings — with what you measured in Phases 5–8.
- **SLOs**: implement §B1 in your monitoring stack with burn-rate alerts. Write the error-budget policy down and get whoever owns the roadmap to agree to it, in writing. An unagreed error budget is not a control.
- **DR**: nightly logical dump to the second cloud account. Then **do the restore drill and record the wall-clock time.** If you can't state the number, your RTO is fiction.
- **Erasure saga + export**: the 7-step `w-delete` saga, plus the separate body-photo revocation endpoint, plus `GET /me/export`. Test erasure by deleting a test account and then *proving* absence in Postgres, all S3 versions, CDN, and traces.
- **Migration policy**: expand→migrate→contract adopted; add a CI check that rejects blocking DDL on large tables.
- **Runbooks**: one per paging alert. Symptom, 3 likely causes, verification query, mitigation, rollback, escalation.
- **Game day**: kill the primary, exhaust a budget, open every breaker, fill the DLQ. Fix what surprises you.

**PHASE 9 EXIT CRITERIA** — status as of 2026-09-17; the detail is in the
dated build-status entry at the end of this file.
- [x] Restore drill completed, time recorded, under stated RTO — **2.8s**,
      verified object-by-object and mutation-checked
- [x] Erasure verified absent across all 5 systems — queried back, not asserted
- [x] Every paging alert has a runbook — `docs/runbooks.md`, every query run
- [x] Game day run; findings ticketed — 6 scenarios, 3 findings, all fixed and
      re-tested against live outages
- [ ] Error budget policy signed off — DRAFTED and deliberately **unsigned**
      (`docs/error-budget.md`); needs a decision, and two thirds of it cannot
      be measured yet
- [x] All `# PROVISIONAL` markers resolved or re-dated — 4 resolved (they were
      mislabelled), 12 re-dated with the measurement that would close each

---

# PHASE 10 — Try-on (week 13)

**Benchmark before you build.** Fork `Ionio-io/VTON-Pipeline`, run your *own* person × garment matrix — 10 bodies × 16 garments including sarees, kurtas and a sherwani. The published benchmark used Western garments; your routing table must come from your own grid.

Then: consent flow (separate record, timestamped, independently revocable) → `w-render` scale-to-zero with per-tenant concurrency 1 → router by category and pose with primary→secondary fallback → the 4-variant prompt ladder with the editorial framing second → quotas at the gateway.

**EXIT:** try-on works or degrades to a board, never errors. Per-render cost measured. Body-photo deletion verified end to end.

---

# PHASE 11 — Learning and trends (week 14+)

Only now, with ≥5k feedback events: Thompson-sampling bandit (85/15 exploit/explore), then a learned compatibility model (OutfitTransformer or MCN fine-tuned on Polyvore then on your feedback) replacing `colour_harmony + formality_coherence`, gated by feedback-replay eval. Trends last, on licensed or first-party sources only, capped at ≤10% of score.

**REVISED 2026-09-18: n = 500, from n = 1.** See "Sizing for production" at
the end of this document for how 500 was derived. The 5k gate is unchanged, and
that is the point — it was the user count that moved, not the gate.

**The 5k gate does not move.** A learned compatibility model fitted to a few
hundred events memorises recent choices and reports it as taste, and the
feedback-replay eval meant to catch that is fitted on the same thin data. At
n=1 the gate put Phase 11 a couple of years out; at n=500 it is a few days of
real use. **Do not compensate by lowering the gate** — that is the one change
that makes every number downstream of it meaningless, and
`stylist_domain.promotion.MIN_EVENTS_TO_PROMOTE` asserts it in a test.

**What the gate does and does not block, which the first reading got wrong.**
Phase 11 is three things and only one of them is gated on 5k events:

| | gated on 5k? | why |
|---|---|---|
| Thompson-sampling bandit | **no** | An online learner. It has no training phase: with no data every arm draws from Beta(1,1), which is exactly uniform exploration. It does not need 5k events to be correct, only to be confident — and it reports its own uncertainty by construction. It is also how the 5k events get collected in a form worth fitting a model to, because a purely greedy ranker only ever shows its own top pick and the log it produces teaches a model the ranker rather than the wearer. |
| First-party trends | **no** | Gated on a COHORT, not on feedback volume: `MIN_COHORT_USERS = 5`. Structurally impossible at n=1, which is why this moved with the user count rather than with the gate. |
| Learned compatibility model | **yes** | And additionally on Polyvore pretraining, which needs a GPU and a dataset licence this project does not have. |

So the bandit and trends are **built**; the learned model is **not**, and what
is built in its place is the GATE it has to pass — `may_promote`, which refuses
for every reason it should and says which one.

---

---

## Sizing for production — n = 500 (revised 2026-09-18, from n = 1)

The user count moved twice: 20 (startup validation) -> 1 (a personal stylist
for its owner) -> **500**. This records how 500 was derived, because a capacity
number with no derivation becomes load-bearing folklore inside a month — the
failure mode commitment 2 exists to prevent.

### What actually binds

Not ingest, and not the request path:

| Path | Measured | Binds at n=500? |
|---|---|---|
| Suggestions, warm from precompute | 6.4-22ms p95 | No. Served from a materialised table. |
| Suggestions, cold/live | 1.2-1.5s | No. It is the documented fallback, not the common path. |
| Ingest | ~25s/photo, `ML_MAX_CONCURRENCY=2` | No. One-time per garment; a 100-garment wardrobe is ~21 min of worker time, and onboarding is bursty rather than sustained. |
| **Nightly precompute** | **see below** | **Yes. This is the constraint.** |

### The derivation

Measured 2026-09-18 on the live stack: a full `nightly_precompute` over 306
tenants took **27.5s**, of which 11 tenants had wardrobes — **2.50s per active
tenant**. That figure is NOT the production cost, because rationale warming was
failing fast with depleted Gemini credits, and rationale warming is the
expensive part.

With real reranks at the measured **3.2-6.4s** per top-8, four occasions per
tenant, run sequentially:

| Rerank latency | Per tenant/night | 4h window | 6h window |
|---|---|---|---|
| 3.2s (best measured) | 13s | 1,125 tenants | 1,687 tenants |
| 6.4s (worst measured) | 26s | **562 tenants** | 843 tenants |

**500 is the worst-case 4-hour figure with a margin.** It is deliberately the
pessimistic corner of a measured range rather than the optimistic one: a
capacity number that only holds if the provider is fast is a number that fails
on the provider's bad night.

### What 500 buys, and what it costs

- **Phase 11 becomes reachable.** At ~3 feedback events per active user per day,
  500 users clear the 5,000-event gate in **3-4 days** of real use rather than
  the couple of years n=1 implied.
- **Trends become possible at all.** `MIN_COHORT_USERS = 5` is a privacy floor,
  not a quality threshold — a trend aggregated from fewer tenants is a report of
  what those tenants wore. At n=1 no trend can ever be published; at n=500 the
  floor is met while still meaning something.
- **Free-tier ceiling: $75/month.** 500 x `free_tier_monthly_budget_usd` of
  $0.15, enforced by LiteLLM on the virtual key rather than by feature code.
- **The blind eval keeps its two outside raters.** Unchanged from the n=1
  revision and for the same reason: a rater grading outfits built from their own
  wardrobe cannot separate "this is a good outfit" from "this is what I would
  have picked anyway".

### Beyond 500

The next ceiling is the same one: the nightly rationale warm, sequential per
tenant. Past ~560 it needs parallelising, which is the `w-render`/autoscaling
work §C2 defers to the capacity phase. **That work is not done**, so 500 is a
ceiling and not a waypoint — raising it is a code change, not a config change.

Tenancy itself is unaffected and always was: RLS is proven by the test suite
rather than by having tenants, which is why it was built that way in Phase 1.


## Weekly operating cadence

| When | What |
|---|---|
| Daily | Standup on the current phase's exit criteria — not on tasks |
| Weekly | Quality review: golden-set trend, correction rate by field, validator rejects, wear-through |
| Weekly | Cost review: spend per user vs budget, per-feature attribution |
| Phase end | Exit criteria walkthrough. **Unmet criterion = phase not done.** No parallel starts. |
| Monthly | Re-verify model availability and pricing; the landscape moves every few months |
| Quarterly | Restore drill · game day · dependency audit · taxonomy review against new user data |

## The three commitments that make this plan work

1. **Exit criteria are gates, not aspirations.** The failure mode is starting Phase 6 with Phase 4's accuracy unmeasured, then discovering in Phase 8 that recommendations are bad because tags are bad — after building three phases on the bad tags.

2. **Every provisional number is labelled.** `# PROVISIONAL: retune in P9` on every latency allocation, pod count and budget ceiling. Unlabelled invented numbers become load-bearing folklore within a month.

3. **Phase 0.1 is the hinge.** Get the taxonomy wrong and every subsequent phase inherits it — the enums, the slot rules, the segmentation mapping, the scorer, the validator, the golden set labels. It's one day of decisions that determines whether this product works for the people who'll actually use it.

---

## Build status

_Last updated 2026-09-15 — Phases 7-8 built; real Gemini tags live, and the user-count bar
revised from 20 users to the owner alone. See the dated entry at the end._

### Phase 0 — Decide and prepare · COMPLETE (0.1) / IN PROGRESS (0.2, 0.3)

**Step 0.1 is done.** [config/taxonomy.yaml](../config/taxonomy.yaml) is frozen at
v1.0.0 and validated by [scripts/validate_taxonomy.py](../scripts/validate_taxonomy.py)
(0 errors, wired as a required CI check). Four Type-1 decisions are recorded in the
file itself with their reasoning: saree as `drape` + required `upper_base`; two
independent formality/dress_code axes; climate bands replacing four-season enums;
accessory range raised 3→5 for festive jewellery.

An audit after freezing found six defects the original validator did not check for,
all since fixed — the significant one being that `climate_bands` was not a total
partition (30°C at 50% humidity, an ordinary Bengaluru afternoon, matched **no**
band, while 28°C/70% matched two). The validator now sweeps the whole
(temperature, humidity, precipitation) grid so that class of gap cannot recur.

**Steps 0.2 and 0.3 are owner-side and in progress** — the 30-real-garment
hand-classification check, golden-set collection (spec written, images pending;
gates Phase 3), and accounts/DPA/branch protection. Neither blocks Phase 1, and
0.2 is designed to run in the background until Phase 3.

### Phase 1 — Skeleton with tenancy · COMPLETE

All five exit criteria met and demonstrated, not asserted:

| Criterion | Evidence |
|---|---|
| `docker compose up` → healthy in <60s from clean | **12s**, exit 0, all 9 services healthy |
| Three RLS tests green, required CI check | 9 tests, plus a CI step that disables RLS and fails the build if the suite still passes |
| Presigned upload → object in storage with versioning | `scripts/verify_upload_e2e.py`, 14/14 against a live stack |
| Same `Idempotency-Key` twice → same `job_id` | verified over HTTP and against live storage |
| DB enums match `taxonomy.yaml` | 9 enum types, label-for-label, in creation order |

42 tests; `ruff`, `ruff format` and `mypy --strict` clean across 27 source files;
migration verified reversible (`downgrade base` → `upgrade head`).

**Three deliberate departures from the plan as written**, each because the plan's
text was wrong or unachievable rather than as a shortcut:

1. **`SET LOCAL app.user_id = :uid` does not work.** Postgres `SET` takes no bind
   parameters, so that snippet either errors or pushes you into interpolating a
   UUID into SQL — an injection sink on the single value that decides which
   tenant's data you can see. The parameterised form is
   `SELECT set_config('app.user_id', :uid, true)`.
2. **Presigned POST, not PUT.** Step 1.5 requires the size cap to live "in the
   presigned policy"; a presigned PUT URL cannot cap a body. `generate_presigned_post`
   carries a real `content-length-range` condition storage enforces before storing
   bytes. Verified: a 13MB upload against a 12MB policy is rejected with HTTP 400
   and never stored.
3. **Async Alembic on asyncpg**, so the project ships one Postgres driver rather
   than adding psycopg2 solely for migrations.

Two smaller shape changes: python packages are prefixed (`packages/stylist_db`,
not `packages/db`) to avoid generic top-level module names, and integration tests
use GitHub Actions service containers rather than testcontainers — same "real
Postgres and Redis, never a mock" intent, less fragility.

**Deliberately not built:** Kubernetes (P9), the outbox relay and ingest stages
(P2), models in `stylist_ml` (P3), a populated LiteLLM config (P4), and any web UI
(the grid is P2.4). The worker registers exactly one real task — a `ping` that
round-trips Postgres — and no placeholder pipeline stages, because a no-op that
looks like a pipeline is worse than an absent one.

#### What running it actually caught

Four bugs survived unit tests and were only exposed by `docker compose up` and a
real upload. Recorded because they argue for the plan's own "works from a clean
`docker compose up`" clause being part of the definition of done:

- **The worker never started.** arq refuses to boot with zero registered
  functions — the "no placeholder stages" choice left the container dead.
- **`redis_settings` declared as a `@staticmethod`.** arq reads it as an
  attribute and got a `staticmethod` object. mypy could not catch it (arq ships
  no stubs), and the error above masked it.
- **Presigned URLs were unusable by any real client.** The API signed
  `http://minio:9000`, the container-internal hostname. Fixed by splitting
  `S3_ENDPOINT_URL` (server-side) from `S3_PUBLIC_ENDPOINT_URL` (client-facing).
  This is a correctness issue, not just DNS: presigned GET signs the `Host`
  header, so the wrong endpoint fails signature validation.
- **A silently dead worker reported as started.** `compose up --wait` only waits
  for *running* on a service with no healthcheck. The worker now has one that
  reads arq's Redis health record, so a dead or hung worker fails visibly.

### Phase 2 — Ingest vertical slice, zero AI · COMPLETE

All six exit criteria met against a live stack:

| Criterion | Evidence |
|---|---|
| Flat-lay → cutout in the grid, p95 under 10s | **2.5–3.2s** end to end |
| Crash-before-commit leaves no orphan garment | rollback **and** a real `SIGKILL` mid-transaction |
| Crash mid-pipeline resumes, does not re-run completed stages | call-counted across a simulated worker restart |
| EXIF GPS stripped | asserted, with a guard test proving the fixture carries GPS |
| Poison image → DLQ → alert fires, other jobs unaffected | DLQ path + `dlq.job_parked` alert asserted |
| 50-photo batch drains, no duplicate rows | **58.9s**, 50 rows, 0 in DLQ, 0 unsent outbox events |

77 tests; `ruff`, `ruff format`, `mypy --strict` clean across 40 source files;
`npx tsc --noEmit` and `npm run build` clean for the web app with 0 npm
vulnerabilities. Re-runnable via `make verify`
([verify_ingest_e2e.py](../scripts/verify_ingest_e2e.py), 23 checks).

**On the corrupt-image criterion:** the plan says a poison image should reach
the DLQ after 3 attempts. It reaches `REJECTED` after ONE attempt instead, and
that is the correct behaviour: a corrupt file fails identically on every
attempt, so retrying it three times burns CPU and delays telling the user
something they can act on. The DLQ is for genuinely retryable failures — a
model service that is down, an S3 blip — and the state machine's DLQ path is
asserted separately with a stage that keeps failing.

**Shape decisions worth knowing:**

- **Matting runs in `services/stylist_ml`, not in the worker.** The plan puts
  matting in 2.3 and the ml service in 3.1; building the endpoint now avoids
  writing rembg into the worker and moving it a week later, and it matches the
  deployment topology either way — model inference scales on CPU-seconds,
  workers scale on I/O concurrency. The ml service holds no database or storage
  credentials, so the one process that touches user pixels structurally cannot
  read the wardrobe.
- **The relay enqueues arq jobs rather than publishing to a Redis stream.** The
  guarantee that matters is at-least-once delivery from a durable outbox into
  the job queue; arq refuses a duplicate `_job_id`, so deriving that id from the
  outbox row makes a re-delivered event a no-op.
- **Relay tick is 1s, not the plan's 250ms.** At 250ms that is 4 queries per
  second per replica forever, and a sub-second dispatch delay is invisible
  inside a 10-second ingest budget. Marked `# PROVISIONAL: retune in P9`.
- **`jobs.state` vs `garments.state`.** The job reaches `complete` (every stage
  Phase 2 defines has run); the garment stays at `matted`, because
  classification, tagging, embedding and dedupe have not. Calling the garment
  complete would be a lie the UI would repeat to the user.
- **Model weights load at ml startup, not lazily.** See below.

#### What running it caught, again

Six defects survived the unit suite and only appeared under `docker compose up`
plus a real upload. Recording them because they are the argument for the plan's
"works from a clean `docker compose up`" clause:

- **Read-your-own-writes was broken.** FastAPI runs a `yield` dependency's
  teardown AFTER the response is sent, so with the ingest transaction owned by
  the `TenantDB` dependency the 202 reached the client before the COMMIT — and
  a client polling the job id it was just handed got a 404. Writes now manage
  their own transaction inline. The regression test deliberately has no sleep,
  because any sleep hides it.
- **`enqueue_job(job_id=...)` silently did nothing useful.** arq reserves
  underscore-prefixed kwargs; `job_id=` was forwarded to the task as an
  argument it does not accept, so every ingest died with a `TypeError` *and*
  the duplicate-suppression never happened. It is `_job_id`.
- **A batch replay returned 1 job id instead of 10.** Per-photo idempotency
  keys were suffixed (`key:1`, `key:2`, …) while the replay query matched the
  bare key, so nine of ten photos vanished from the client's view with no error
  anywhere. The whole batch is now claimed under one key, ids and order intact.
- **The first ingest took 28s against a 10s budget** — a ~20s lazy ONNX model
  load landing on whichever user uploaded first. The model now loads at ml
  startup, and `/readyz` reports whether the session is *built* rather than
  whether the file exists, so traffic is gated until the pod can actually serve.
- **The state machine wrote to `jobs` without tenant context.** `jobs` is
  RLS-protected and the worker connects as `stylist_app` (NOBYPASSRLS), so
  every transition matched zero rows — silently, no error. `user_id` is now
  threaded from the arq job arguments and every write is tenant-scoped, which
  also means a worker bug that forgets a WHERE clause is contained by the same
  mechanism that protects the API.
- **Two stages claimed the same `completed_state`.** `matte` and `persist` both
  said `MATTED`, so the resume check skipped `persist` on every run — the
  pipeline would matte an image and never save it.

One more, caught by tooling rather than by running: the initial web scaffold
pulled a Next.js release with a published CVE. `npm audit --omit=dev
--audit-level=high` is now a CI step.

### Phase 3 — CV pipeline · STEPS 3.1–3.4 COMPLETE, 3.5 BLOCKED ON DATA

**162 tests**; `ruff`, `ruff format`, `mypy --strict` clean across 53 source
files; migration 0002 reverses cleanly.

| Step | State | Evidence |
|---|---|---|
| 3.1 ml inference service | done | `/segment`, `/matte`, `/embed`, `/models` on real weights: ~1.3s / ~1.8s / ~0.8s |
| 3.2 segmentation + split | done | one photo → 2 garments end-to-end in **7.6s** |
| 3.3 cheap classifier | colour done, pattern deferred | maroon top + denim trousers read back correctly |
| 3.4 embeddings + pgvector | done | `vector(768)`, HNSW index, `/garments/{id}/similar` |
| 3.5 eval harness | **runs, cannot measure** | exits 2 (not-measured) — no golden set |

**The phase cannot close.** Its exit criterion is not "the harness runs", it is
a NUMBER: segmentation accuracy on the 50 ethnic-wear images, plus a recorded
decision between (a) accepting manual crops for drapes, (b) adding a drape
detector, or (c) fine-tuning SegFormer. `eval/golden/labels.jsonl` does not
exist, so all three eval entry points exit **2** — deliberately distinct from 0
(passed) and 1 (floor breached), so CI can tell "unmeasured" from "bad". The
accuracy floors in `taxonomy.yaml` are currently unenforceable.

`pattern` is left NULL rather than guessed: it is `source: classifier_head`, a
logistic head over the FashionSigLIP embedding trained on ~2k labelled
examples — which come from the same golden set. A fabricated pattern label
would be indistinguishable from a real one downstream, and
`more_than_two_bold_patterns` is a scoring penalty that would then fire on
invented data.

**Deviations, each because the plan's text was unachievable or wrong:**

1. **Fetch, not export.** Both upstream repos publish official ONNX, pinned
   here by commit sha with verified sha256. An export path would need ~2GB of
   torch to reproduce someone else's artefact, and our export settings would
   become a second source of truth. `export_models.py` becomes necessary only
   if we fine-tune — option (c) above.
2. **Bytes in the body, not `{image_url}`.** URLs would mean giving the ml
   service object-store credentials — the exact blast radius "no DB access"
   exists to avoid — and an S3 round trip inside an 800ms budget.
3. **`/segment` filters nothing.** It reports what the model saw; the 2% floor,
   IoU dedupe, shoe merge and drape-ambiguity rule live in
   `stylist_domain.split`, pure and unit-tested in milliseconds. Worth seeing
   why: a real photo produced a 0.41% phantom "Dress" and a 0.00% "Bag".
4. **Tests now run against the compose Postgres** (pgvector/pgvector:pg16), not
   a local install. A stock Postgres has no pgvector, so `vector(768)` could
   not be created; this also removed a quieter local-PG-17-vs-prod-PG-16 drift.

#### Six more bugs that only running it exposed

- **RESUME WAS BROKEN, AND I HAD REPORTED IT AS VERIFIED.** Phase 2's
  "crash mid-pipeline resumes" criterion was tested with FAKE stages, so it
  only ever proved the state machine's control flow. Real stages read
  `ctx.scratch["sanitised_bytes"]`, which is empty after a restart — resuming a
  job at MODERATED raised `KeyError`. Stages now derive object keys from ids
  and fetch from storage, so any worker can run any stage with no handover.
- **An ml restart DLQ'd every in-flight ingest.** Three attempts with jittered
  backoff exhausts in ~6s; ml takes ~30s to build its sessions. A dependency
  being down is backpressure, not a verdict on the image, so `Unavailable` now
  waits on a 180s wall-clock budget WITHOUT consuming an attempt. Proven: the
  same scenario that produced `dlq=true, stage_attempts={"matte":3}` now
  completes in 19s.
- **ORM enums had no values.** `ENUM(name="slot", create_type=False)` lets
  SQLAlchemy WRITE `'lower'` and then throw `LookupError` on READ. Latent since
  Phase 1, invisible while every enum column was NULL; the first symptom was
  the wardrobe endpoint 500ing on a garment that had saved fine.
- **numpy scalars leaked into SQL.** `np.float64 < float` is `np.bool_`, which
  asyncpg rejects — surfacing as a DataError on an UPDATE, three layers from
  the cause.
- **Masked matting produced BLACK cutouts.** rembg zeroes RGB where its own
  alpha is 0, so widening the alpha with a segmentation mask revealed blanked
  pixels: a maroon top and denim trousers were both catalogued as `black`.
  Alpha and colour now come from different sources by design.
- **The SIGABRT flake returned, worse.** With three ONNX sessions instead of
  one, `162 passed` again shipped alongside exit 134. Each session is now
  released by whoever owns it; 4 consecutive runs clean.
- **The ml service collapsed under a 5-photo burst.** One process serving
  CPU-bound inference to 4 concurrent workers, with no concurrency limit: 4
  requests x 4 onnxruntime threads on 8 vCPUs, so every request slowed, client
  read timeouts fired, retries added load, and the queue climbed past 200 while
  jobs stalled at `segmented` with `{"matte": 2}` and no DLQ marker. Added the
  §C2 bulkhead it was missing — a concurrency semaphore that sheds with 503 +
  Retry-After rather than queueing forever, plus a thread budget (2 threads x 2
  slots) that fits the box. ReadTimeouts went from many to zero.
- **And the shedding did not work, because the error handler ate it.** Each
  endpoint wrapped inference in `except Exception`, and `HTTPException` IS an
  Exception — so the deliberate 503 was rewritten as a 500. The client saw a
  server error instead of backpressure, spent the image's retry budget on it,
  and DLQ'd the job. The load shedding was correct; the handler destroyed the
  signal. Verified after the fix: ml shed 4, the worker waited 4 times
  consuming no attempts, zero read timeouts, burst of 5 drained clean.

**One latency consequence worth stating plainly:** Phase 2's "under 10s" target
described a five-stage pipeline. Phase 3 adds segmentation, classification and
embedding — two more model calls — and warm runs now land at **7.4-8.3s** to
COMPLETE with a cold first request around 11s. The e2e check was moved onto
§B1's actual contract (reach >= CLASSIFIED within 60s) rather than loosened to
whatever today's number happens to be. Under a 5-photo burst on one ml pod,
per-photo wall clock is ~28s median — which is what `ml-inference 2-12 pods`
in View 2 exists to fix, and is a P9 capacity item, not a correctness one.

### Phase 4 — VLM tagging and correction UI · BUILT; 2 of 7 criteria need external inputs

**190 tests**; `ruff`, `ruff format`, `mypy --strict` clean across 60 source
files; migrations 0001-0003 all reversible; web app builds with 0 npm
vulnerabilities.

| Exit criterion | State |
|---|---|
| Batching: 6 garments = 1 VLM call | **verified by call count** (and 7 → 2, not 7) |
| Kill the VLM provider → `DEGRADED_TAGGED`, item usable | verified |
| Exhaust the budget → cataloguing still works | verified, and not retried |
| Correction events flowing to the quality dashboard | verified end-to-end |
| Non-garment / flagged upload → quarantined, **zero** outbound calls | verified by counting calls |
| Measured cost per garment ingested | **mechanism** verified, **number** needs a paid provider |
| Golden-set accuracy ≥92% on category + primary colour | **blocked** — no golden set |

Verified against the live stack, one photo through the whole chain
(`received → sanitised → moderated → segmented → matted → classified → tagged
→ complete`, ~40s):

| garment | subcategory | material | formality | dress_code | review |
|---|---|---|---|---|---|
| `upper_base` (maroon) | kurta | cotton, conf **0.55** | 3 | festive_ethnic | **yes** |
| `lower` (denim_indigo) | jeans | denim, conf 0.88 | 2 | casual | no |

`material` at 0.55 is under its taxonomy `review_below` of 0.60, so that
garment routed to review and the other did not — the confidence gate working on
real output rather than in principle.

**What is real and what is a stand-in.** The gateway, virtual keys, per-tenant
budgets, spend mirroring into `model_calls`, the grid batching, the
taxonomy-generated JSON schema, the parser, the enum validation, the SQL and
every degrade path are real and exercised. The MODEL is a stand-in: no provider
key exists because Phase 0.3's DPA is outstanding, so `vlm-tagger-mock` returns
a schema-valid response through the live gateway. `vlm-tagger` and a fallback
provider are configured and unset — which means the DEGRADED_TAGGED path is the
default locally and gets exercised constantly rather than only in a drill.

**Moderation is a real local model.** `AdamCodd/vit-base-nsfw-detector`, pinned
by commit sha with a verified digest, running in-VPC before every stage that
could export pixels — because satisfying this gate with a hosted moderation API
would upload the exact images it exists to keep off other infrastructure. An
ordinary garment photo scores 0.078 and passes. Two thresholds, not one: ≥0.90
quarantines (terminal, audited, no provider call), ≥0.60 flags for review but
still processes. A false quarantine is a worse product failure than a false
pass into review.

**Non-garment photos are deliberately NOT handled here.** A screenshot or a
photo of a dog is not a moderation problem and an NSFW classifier has no
opinion about it. Segmentation already covers it: no garment class above the
2% floor → NEEDS_REVIEW with a reason. A second, weaker check in the moderate
stage would duplicate that with worse information.

#### Findings from building it

- **`moderation` only reached the first garment.** The verdict is a property of
  the PHOTO, but moderate runs before segment — so garments discovered by the
  split were inserted with `{}` and looked permanently unmoderated. Any audit
  asking "was this image screened?" got the wrong answer for every garment but
  one. The split now copies the verdict.
- **The gateway's response cache silently masked a config change.** After
  changing the mock payload, tagging still wrote nothing: LiteLLM was correctly
  returning the cached previous response for a byte-identical request. The
  cache working as designed, and a real gotcha — an identical test image
  produces an identical grid produces a cache hit.
- **The LiteLLM healthcheck used `curl`, which that image does not have.** The
  gateway answered 200 on both health endpoints while compose reported it
  unhealthy and `up --wait` failed the entire stack over a working service.
- **An empty mock response is a worthless mock.** `{"items": []}` exercises the
  call but never the parse/validate/write path — so the first time that code
  would have run was against a paid provider. The mock now returns a
  schema-valid two-item payload with one field deliberately below its review
  threshold.

#### Two criteria that need something from outside the code

1. **Cost per garment.** The mechanism is verified — every call mirrors model,
   tokens, cost, latency and cache status into `model_calls`, joined to the job
   and user, which is what makes "cost per garment ingested" answerable at all.
   But the mock is priced at zero, so the measured number is `$0.00`. A real
   figure needs a provider key, which needs the DPA.
2. **Golden-set accuracy.** Unchanged from Phase 3: `≥92% on category +
   primary colour` cannot be evaluated without the 500 labelled images.

### Next: Phase 5 — MVP ship

Dedupe (phash + cosine > 0.95), wear log, search and filters, minimum-viable
observability, and the "start with your 20 most-worn" onboarding flow.

**Phase 5's exit criteria are the first that cannot be faked at all**: a real
wardrobe, fully catalogued, and correction rate measured on it. The plan calls
that "the real go/no-go" — if correction rate exceeds ~20% on any field,
ingestion gets fixed before anything is built on those tags. The
`/ops/correction-rate` endpoint and the in-app rate strip built in this phase
are what that decision will be read from.

_Revised 2026-09-15: the bar was 20 users and ≥2,000 garments. See Phase 5's
goal above for what n=1 does and does not buy._

Two things now sit on the critical path and neither is code: the
**golden-set images**, and the **owner's wardrobe photographed**. The provider
key is no longer one of them — see the 2026-09-15 entry below.

---

## Cross-phase verification pass (2026-09-10)

A full re-verification of Phases 0–4 together, rather than each phase against
its own criteria in isolation. Everything below is a measured result; where a
number could not be measured honestly it says so.

### What the pass found

Five defects, **none of them a wrong-output bug** — every ingest in every run
produced correct results (23/23 content checks). All five were in the seams:
operational behaviour, or the test harness itself.

**1. A skip is not a pass — 29 tests were never running.**
`pytest` exits 0 for skipped tests, and several fixtures skip when they cannot
reach a dependency. Run without the full env, the suite reported *163 passed,
29 skipped*; `tests/test_api_smoke.py` reported "PASS" with **all 17 of its
tests skipped** for want of redis. With the env complete it is **192 passed, 0
skipped**. Two causes, both now closed:

  - `conftest` defaults to `localhost:5432/stylist_test`, but the pgvector
    container publishes **55432**. The wrong-port Postgres has no pgvector, so
    the failure surfaced as `extension "vector" is not available` — which reads
    like a broken migration, not a misdirected connection.
  - `U2NET_HOME` unset falls back to the container path `/models/u2net`, absent
    on the host, so every matting test skipped with "u2net weights absent".

  Fixed by making `make test` set its own DSNs and env (it no longer depends on
  what happens to be exported), documenting `U2NET_HOME` in `.env.example`,
  adding a `make test-strict`, and making the gate runner **fail** when any
  test skips.

**2. An unreachable ml service was invisible to every probe.**
`ml`'s container healthcheck probes `localhost:8000` from *inside* the
container, so it passes while the container is detached from the network. The
worker has no probe. The api's `/readyz` checked Postgres and both redises but
not ml. Result: a container answering the host on its published port while
every worker call failed with `ConnectError` — undetected for ~30 minutes.
Fixed by probing ml from `/readyz` over DNS, on the worker's own path.
Deliberately **non-fatal** to readiness: ingest is built to absorb an ml
outage, so failing readiness would convert a designed degradation into a total
API outage. Two regression tests pin both halves.

**3. Head-of-line blocking under a dependency outage.** `Unavailable` sleeps
*in-process*, so a job waiting on down infrastructure holds its worker slot for
the full 180s budget. At `WORKER_MAX_JOBS=2`, two such jobs stall the whole
queue — observed directly: eight jobs DLQ'd ~240s apart, in sequence, rather
than together. The ml bulkhead caps inference concurrency but nothing caps
*waiting*. **Not fixed.** The correct shape is to release the slot and
re-enqueue with a delay, which resume already supports; that is a real change
to the state machine and wants its own step.

**4. An intermittent abort after a green run.** `tests/test_ml_inference.py`
could abort at interpreter shutdown with `recursive_mutex lock failed` *after*
reporting "10 passed" — exit 134 on a green suite. The existing fixture
released the sessions it owned, but matting's session is held by an
`lru_cache` on `matting._session`, so it outlived every fixture and its native
destructor ran during interpreter teardown. Its own docstring already stated
the rule ("each one has to be released by whoever owns it"); u2net's owner was
that cache, and nothing cleared it. Fixed with a module-scoped autouse fixture
that clears the cache while the interpreter is still alive. Verified over 6
runs including 3 under concurrent ingest load — though it is a race, so that
is evidence, not proof.

**5. `docker compose run` does not register the service DNS alias.** A one-off
container started that way answers the host on a published port but nothing
resolves the service name on the compose network. This is what produced (2)
during investigation, and it is worth knowing before debugging a "healthy but
broken" service.

### An investigation that produced no shippable change

The ml container's memory was suspected of unbounded growth. It is not
unbounded — it plateaus — but the first three attempts to measure it produced
contradictory numbers (single-photo latency ranging 8–85s for the *same*
config), and every one of those numbers was an artifact:

  - `docker stats` reports cgroup `memory.current`, which **includes
    reclaimable page cache**. Right after loading ~1GB of weights the cache
    term was ~400MB. `anon` from `memory.stat` is the number that matters.
  - `docker compose --force-recreate` is a **no-op** when the running container
    already matches the requested config, so a "config change" measured the
    old container.
  - in **zsh**, an unquoted scalar is not word-split: `DC="docker compose -f x";
    $DC ps` tries to execute the whole string as one filename. Every recreate
    in the first harness silently failed this way, with its stderr filtered out.
  - a DLQ backlog draining behind the measurement inflated the first run of
    every batch.

Once the harness asserted its own preconditions — fresh container, expected
env, live code, ready models, worker-visible DNS, drained queue — the result
was stable and is recorded in `docker-compose.yml`: the arena costs ~1GB of
steady state (3924 MiB vs 2917 MiB) at **no** latency cost, so it now defaults
off. The `mem_limit` stays 4g because the measured *peak* is 3938 MiB.

The lesson is not about onnxruntime. A harness that does not verify its own
assumptions will confidently produce numbers about a system it is not testing,
and those numbers are indistinguishable from real ones.

### Still not measurable

Unchanged, and unchanged for the same reason: **cost per garment** needs a
provider key (the mock is priced at zero, so the answer is `$0.00`), and
**golden-set accuracy** needs the 500 labelled images. The eval harness exits
**2** — not-measured — rather than reporting a pass, and the gate suite asserts
that exit code so a future change cannot quietly turn "not measured" into
"measured and fine".

### The latency "regression" that was not one

Worth recording because the wrong conclusion survived several rounds of
measurement and was stated as fact before being checked.

Symptom: end-to-end ingest measured 25–85s against a Phase 3 baseline of
7–8s, and it stayed slow even after the memory work, with the spread
narrowing but the absolute number staying ~25s. That looked like a real
regression and was reported as one.

It was not. Adding per-stage timing to the state machine answered it in one
run:

    pipeline_done total_ms=6976 breakdown={
      validate: 15, sanitise: 29, moderate: 1171, segment: 1764,
      matte: 2170, classify: 59, tag: 441, embed: 1261, persist: 66 }

The pipeline was ~7s the whole time — the baseline, unchanged. An isolated
single-photo ingest on a drained queue measures **6.0–6.6s**, accepted in
16–66ms; a later run with warm-up discarded gives a median of **6.1s** against
the 10s budget. Faster than the 8.37s the number was being compared against.

Every slow figure came from the measurement, not the system:

  - `verify_ingest_e2e.py` set `t0` ONCE before a burst and reported each job's
    offset from it as "per-photo wall clock". That is drain time, not
    per-photo latency, and the block ran *after* the SSE, duplicate and replay
    sections had already queued jobs — so at `WORKER_MAX_JOBS=2` the timed jobs
    waited behind them.
  - concurrent commands (a background A/B, a monitor, ad-hoc `docker stats`)
    loaded the same 8-vCPU box being measured.

Fixed by renaming the burst metric to what it measures (`burst drain: median
completion`), adding a separate isolated single-photo check asserted against
the 10s budget, and discarding one warm-up sample — the burst's trailing outbox
and arq work overlaps the first submission and inflated it to 17.7s against
6–7s for the rest.

Two things worth keeping from this:

**Per-stage timing is now permanent.** One `stage_done stage=<n> job=<id>
elapsed_ms=<n>` line per stage plus a `pipeline_done` breakdown. Before it,
"an ingest takes 25s" was where the investigation stopped: the ml endpoints
summed to ~6s of it and there was no way to attribute the rest. §B1's
per-stage SLOs are unenforceable without it.

**A benchmark that shares a machine with other work measures the other work.**
Both of this pass's wrong conclusions — the arena's "3x latency penalty" and
this regression — came from that, not from the system under test.

---

## Phase 5 — MVP ship (built 2026-09-11)

All five deliverables are built and verified. The exit criteria are a different
matter and are addressed at the end.

### Dedupe — two signals, and it never merges

Perceptual hash (dHash, 64-bit) **and** embedding cosine, because they fail in
opposite directions: the hash catches the same PICTURE re-uploaded, the
embedding catches the same GARMENT re-photographed. A match on either raises a
proposal.

Two decisions worth recording:

**The hash is of the CUTOUT, not the original.** One flat-lay can yield three
garments that share an original image, so hashing the original gives three
identical hashes and the stage proposes that every garment in a photo is a
duplicate of its neighbours. The cutout is the only image whose hash means
"this garment".

**It proposes; it never merges.** A match parks the garment at
`DUPLICATE_SUSPECT` with `duplicate_of` set, and asks. The asymmetry is the
argument: a wrong merge destroys a garment the user owns and they may never
notice, a wrong proposal costs one tap. Merging is a soft delete and the wear
log is reparented, because a user who logged three wearings against the
duplicate did wear the garment three times.

Measured dHash distances: JPEG re-encode **0 bits**, 50% resize **0**,
brightness +35% **7**, a different garment **29**. The threshold is 10, in the
empty space between, biased low because a false proposal interrupts while a
miss costs only a row the user can merge later.

### Wear log — rows, not a counter

Every question worth asking is about WHEN: most-worn (the onboarding ranking),
"not worn in 90 days", and cost-per-wear. A counter answers the last one badly
and the first two not at all, and it cannot be corrected — decrementing an
integer whose history is gone leaves you unable to say whether the new value is
right. One wearing per garment per day, enforced by a unique index rather than
by the handler: a double tap is the same fact twice, and cost-per-wear silently
halves.

Money is stored in **minor units as an integer**. Cost-per-wear divides it, and
binary floating point accumulates error across a wardrobe.

### Search — ts_rank, and why that is not BM25

The plan says "BM25 over `search_text`". Postgres does not implement BM25;
`ts_rank_cd` is a coverage-density rank with none of BM25's document-length
normalisation or saturating term frequency. Real BM25 needs ParadeDB or a
separate engine.

It does not need one **yet**, and the reason is the corpus: a garment's search
document is eight enum values, so every document is the same length and no term
repeats. The two things BM25 adds are both no-ops on documents shaped like
this. This becomes wrong the moment free text enters the document — a user's
own notes — and that is the trigger to revisit it, not a version number.

`search_text` is maintained by a trigger. A GENERATED column was the first
choice and Postgres rejects it: casting an enum to text is not immutable, and
every searchable field is an enum. Both approaches rule out the real risk,
which is recomputing search text at the call site and having one forgotten
UPDATE make a garment silently unfindable.

Facets exist so the UI never offers a filter that matches nothing — a
taxonomy-driven list offers 144 subcategories to someone with nine garments.

### Observability — and the bug that made it worthless

Four alerts, exactly as specified, each with a defined response: `dlq_age`,
`spend_spike`, `ingest_success`, `api_5xx`.

**The first version could not fire.** The alerts queried `jobs` directly. The
API runs as `stylist_app` — NOSUPERUSER, NOBYPASSRLS by design — and every
tenant table is FORCE RLS, so those queries returned zero rows with no error.
`count(*) = 0` reads as "the DLQ is empty"; a rate over no samples reads as
"nothing is failing". The alerts were live, green, and structurally incapable
of firing, which is the worst possible failure for monitoring because it is
indistinguishable from health.

Migration `0006` fixes it with SECURITY DEFINER functions rather than by giving
the API owner credentials. The function runs with the owner's rights, the API
holds none, and the whole privileged surface is seven functions returning
counts, rates and timestamps — no argument to any of them can yield a garment,
an image key or an email. Verified both directions: `stylist_app` can call the
aggregates and still reads **zero** raw rows.

A second instance of the same class: the `api_5xx` counter called `.pipeline()`
on the `CacheRedis` wrapper, which has no such method, and the middleware
swallows its own exceptions by design so telemetry can never fail a request.
The counter stayed empty and the alert could not fire. The counters now live on
the wrapper as real methods.

`tests/test_ops_alerts.py` asserts the FIRING direction for each alert, because
a test that only checks "does not fire" passes perfectly against monitoring
that is blind.

Two further guards, both about trust rather than correctness: a rate over fewer
than 20 samples never fires (2 failures in 3 is 67% and means nothing), and
`/ops/*` requests are excluded from the counters so a monitoring loop cannot
dilute the rate it measures.

**Tracing** is a span per stage, off unless `OTEL_EXPORTER_OTLP_ENDPOINT` is
set, degrading to a no-op when the SDK is absent. An ingest pipeline that
refuses to run because it cannot report on itself has its priorities backwards.
**Langfuse** is wired but deliberately NOT enabled by default: with the
callback on and no keys, LiteLLM logs an exporter error on every call, which
buries real errors. Enabling is two uncommented lines plus keys.

### Onboarding

`/wardrobe/most-worn`, defaulting to 20 — the plan's "start with your 20
most-worn". A wardrobe of 20 things you actually wear is useful immediately;
a half-catalogued closet of 200 is a chore with no payoff.

### Two bugs in earlier phases that Phase 5 exposed

**`persist` was overwriting garment-level decisions.** It reset every garment
to `matted` unconditionally, which silently discarded the `DUPLICATE_SUSPECT`
the dedupe stage had just set: the duplicate was detected, recorded, and then
presented as an ordinary garment with no question attached. The same
last-writer-wins shape as the `extractor_version` collision found during the
cross-phase pass.

**`docker compose run` and zsh scalars, again.** Two more measurement detours
came from the same two traps documented in the cross-phase pass. They are in
the docs because knowing about them did not stop me repeating them.

### Exit criteria — revised to n=1 on 2026-09-15

The user-count bar moved from 20 users to the owner alone; see Phase 5's goal
for the reasoning and the limits. What that changed and what it did not:

- [x] **Zero cross-tenant incidents** — RLS covers `wear_log`, verified by
      mutation (disabling FORCE fails the suite), and search is tenant-scoped.
      **Unaffected by n=1**, and deliberately so: tenancy is proven by the test
      suite, not by having tenants, which is why it was built that way in
      Phase 1.
- [ ] **Owner's entire wardrobe ingested** — needs photographs. This is now the
      single largest open item in the project.
- [ ] **p95 ingest-to-CLASSIFIED < 60s on a real burst** — the criterion was
      re-scoped from "under real load", which n=1 cannot produce. Measurable
      the moment the wardrobe is ingested in one sitting. Reference points on
      synthetic traffic: single photo **5.1s**, a burst of 10 drains in ~21.5s
      median.
- [ ] **Correction rate per field, owner's wardrobe** — endpoint and in-app
      strip exist. **No longer blocked on the provider key** (real Gemini tags
      landed 2026-09-15), only on the wardrobe. The old reading of 67% on
      `subcategory` was an artefact of the mock answering "kurta" for
      everything and should be discarded, not compared against.
- [ ] **Cost per user per month** — the per-call number is now REAL:
      **~$0.0023 per VLM call** measured against Gemini, at batch-of-1, which
      is the worst case since a 6-cell grid amortises the prompt. The monthly
      figure still needs a wardrobe to multiply it by. Note this number only
      became trustworthy after the `cache_hit` fix below — 40% of recorded
      spend was cached responses the provider never charged for.

**The correction-rate criterion is no longer gated on the DPA.**
Search and filters consume `subcategory` and `dress_code`, and with mock tags
they looked broken — every garment a `kurta`. That made the provider key a
prerequisite for Phase 5 being *usable*, not merely for closing Phase 4, and it
is the reason the key was the first thing unblocked. It is now live; see below.

---

## Phase 6 — Deterministic suggest pipeline (built 2026-09-11)

All four steps built. Four of the five exit criteria are met and measured; the
fifth needs real tags.

### 6.1 Context resolution

`resolve_context(occasion, feels_like_c, precip, wind)` is a pure function in
`packages/stylist_domain/context.py`. Every threshold comes from
`taxonomy.yaml` — `warmth` carries `feels_like_c_max` per level (34/30/26/21/16)
and all 18 occasions carry a `dress_code` and `formality_target`. Nothing is
invented in code, so tuning is a reviewed data change.

`feels_like`, not `temperature`: 30°C at 90% humidity and 30°C at 20% call for
different clothes, and this product's market spans both within one city.

Two details worth keeping:

  - The warmth table reads as CEILINGS, so the answer is the HIGHEST level
    whose ceiling is still at or above the reading. Taking the lowest
    qualifying level would leave you under-dressed on every cold day, and a
    single-value test would not catch it — hence a monotonicity test across
    45°C to -10°C.
  - `occasion` has no default. The same weather calls for very different
    clothes depending on whether you are interviewing or going to the gym, and
    a default would silently pick one.

Weather comes from Open-Meteo `/v1/forecast` with current and daily in ONE
call, coordinates rounded to 2dp (~1.1km). That rounding is a cache decision
and a privacy decision at once: at full precision every request is a unique key
so the 1h TTL never fires, and we stop sending a third party a location precise
enough to identify a building. No API key, so weather is off the DPA critical
path entirely.

### 6.2 Slot rules — and a gap in the taxonomy they exposed

The evaluator (`packages/stylist_domain/slots.py`) is table-driven from
`outfit_rules`. No rule is expressed in Python, because these encode product
decisions that will be argued about and should be settled by editing a reviewed
data file.

**Building it found a real bug.** `saree + choli_blouse + sandals` was
REJECTED. A saree sits in `drape`, so that outfit satisfied neither
`[upper_base, lower]` nor `[full_body]` — the single most important ethnic
outfit in the market this taxonomy was built for was unrepresentable. DECISION 1
says a saree "occupies `drape` and REQUIRES an `upper_base`", which implies
saree + blouse IS the complete form, but only the `requires` half was ever
encoded; the "covers the lower body" half was missing.

Fixed with `outfit_rules.lower_equivalent: [saree, half_saree]`, deliberately a
short list rather than the whole `drape` slot — a dupatta, stole or shawl
covers nothing, so treating every drape as lower-equivalent would accept
"dupatta + blouse + sandals" as complete. The count ADDS rather than
substitutes, so a saree worn over churidar is 2 and still fails. `lehenga_skirt`
is absent because it already lives in `lower`.

Verified in both directions, plus a 1,500-case property test cross-checked
against an INDEPENDENT re-implementation of the rules — re-calling `evaluate()`
to check `evaluate()` would pass against any bug it contains.

Candidate generation anchors on 8-12 items, three of them reserved for
LOW-WEAR exploration. A recommender that only ranks by affinity converges on
the six things you already wear, which is the problem this product exists to
solve.

### 6.3 The scorer, and the number that keeps it honest

Weights live in `config/scoring.yaml`, not code, because they will be tuned
against the blind eval and tuning must not need a deploy. They are validated to
sum to 1.0 at load: weights that do not sum to 1 make the total uninterpretable.

The scorer reports **`informative_weight`** per outfit — how much of the score
came from inputs that could actually separate one outfit from another. Measured
on the current data: **0.30 to 0.65 of 1.0**. With mock tags, `formality` and
`warmth` are identical across every garment, so those sub-scores compute
variance over a constant and rank nothing.

This exists because a bad suggestion must be attributable. Without it you
cannot tell a broken scorer from uniform input — the exact ambiguity that cost
this project a long detour during the cross-phase pass. `style_affinity` (0.20)
and `trend_alignment` (0.05) are present, weighted and reported at ZERO rather
than omitted: dropping them would mean the other four weights secretly sum to
0.75 and every score would be depressed for a reason no reader could see.

Rule-breaking outfits score exactly 0, not merely low — the plan says
forbidden combinations are "never surfaced", and a low score still surfaces
when the wardrobe is small.

### 6.4 Nightly precompute — and the same RLS trap, a second time

`pg_try_advisory_lock(hashtext('nightly_precompute'))`, and `try_` rather than
the blocking form on purpose: a replica that queued would run the entire job
again the moment the winner finished, turning three replicas into three
sequential runs instead of one.

**The driver could not see any tenants.** `SELECT DISTINCT user_id FROM
garments` as `stylist_app` against FORCE RLS returns zero rows with no error,
so the job reported `{"ran": true, "tenants": 0, "written": 0}` — a successful
run that did nothing, indistinguishable from a night when nobody owned any
clothes. Every `GET /suggestions` would have been empty forever with no failure
to point at.

This is the SECOND instance of the identical mistake; the Phase 5 alerts were
blind the same way. Fixed the same way, migration `0008`: a SECURITY DEFINER
function exposing only the set of user UUIDs that own an active garment. RLS
still prevents the app role from reading a single row belonging to any of them.
Verified both directions — 266 tenants visible through the function, **0** raw
garment rows.

### Exit criteria

- [x] **`GET /suggestions` ranked, zero model calls, p95 < 300ms** — measured
      **7.5ms** materialised, **58.1ms** generating live. Both paths exist
      because precompute cannot cover 18 occasions x 5 warmth levels x wet/dry
      per tenant, and a user picking "interview" on a cold day must not get an
      empty screen.
- [x] **Slot-rule property tests green, including composite ethnic garments** —
      1,500 random sets against an independent implementation, plus explicit
      saree/half-saree/lehenga/dupatta cases.
- [x] **3 cron replicas → exactly 1 execution** — verified; the two losers
      report `lock_held` rather than erroring.
- [x] **Candidate generation < 100ms for a 400-item wardrobe** — **3.8ms**.
      Flat at ~4ms for pools of 100, 400 and 1000 because `MAX_CANDIDATES=500`
      binds before pool size does. Measured against a fully-wearable synthetic
      pool, because the seeded wardrobes filter down to ~30 items and would
      have measured the wrong thing.
- [ ] **Internal blind eval: 50 outfits, ≥60% "would wear"** — BLOCKED, and not
      on code.

### Why the blind eval is blocked, concretely

A top-ranked suggestion from the seeded data reads:

    henley(grey_charcoal) + lehenga_skirt(black) + slides(denim_indigo)

That is nonsense, and it is nonsense because `scripts/seed_demo.py` assigns
`dress_code` and `formality` at random, so the hard filters pass garbage and
the scorer confidently ranks it. `informative_weight` on that outfit is 0.65 —
the score itself says a third of its basis was uninformative.

**Do not run the blind eval on seeded or mock-tagged data.** Rating 50 outfits
built from random attributes yields a meaningless number, and acting on it
means "fixing" a scorer that was never broken. The plan's warning — "the LLM
will not save a bad candidate set" — has a corollary: nor will fixing a scorer
that was fed noise.

The eval needs real tags, which needs the provider key. Everything else in
Phase 6 is done and measured.

**Update 2026-09-15: the provider key is live and this is now blocked on one
thing only — a real wardrobe to suggest from.** Real tags are landing (see
below), but tagging the *seeded* wardrobe would not help: `seed_demo.py`
garments have no photographs, so there is nothing for a VLM to look at. The
eval runs when the owner's own clothes are in the database, and not before.


### A bug Phase 6's gate run surfaced in Phase 5 (and Phase 4)

The Phase 5 e2e script started failing roughly **1 run in 4**, always on one
check: "a mis-tap can be undone". A `DELETE /garments/{id}/wear/{date}`
returned 204 and an immediate re-read still counted the deleted wearing.

Cause: every Phase 5 write handler took `TenantDB`, a `yield` dependency whose
transaction commits on teardown — and FastAPI runs teardown **after** the
response is sent. The client therefore had its 204 before the delete was
durable. `garments.py` documents this exact trap for ingest and fixes it by
owning the transaction inline; the Phase 5 handlers reintroduced it, and so had
Phase 4's `correct_field`, latent since it shipped — a correction could render
as "didn't save" on a fast connection.

Fixed in five handlers (`log_wear`, `unlog_wear`, `set_laundry`,
`resolve_duplicate`, `correct_field`): 1-in-4 failures became 8 of 8 clean.

**Two things worth keeping from this.**

First, the gate harness was hiding the diagnosis. On failure it printed
`tail -4` of the output, which for a 32-check script shows the last checks that
PASSED and never the one that failed. It now greps for the failing lines. A
harness that cannot show you the failure costs more than the bug.

Second, the pytest regression tests for this **cannot catch it**, and say so in
a comment. pytest drives the app in-process via httpx's `ASGITransport`, which
awaits the full response cycle including dependency teardown, so the race is
unreachable there — verified by mutation: reverting the fix leaves them green.
The real guard is the e2e script, which crosses a socket from another process.
A test that looks like a guard and is not is worse than no test, so the
limitation is written down rather than assumed away.

---

## Real tags, and the scope change to n=1 (2026-09-15)

Two things happened on the same day: the provider key went in and started
working, and the user-count bar dropped from 20 to one. They are recorded
together because the second is only defensible given the first — without real
tags, n=1 would have meant measuring nothing at all.

### The provider key is live

`GEMINI_API_KEY` is set, `VLM_MODEL=vlm-tagger`, and the `vlm-tagger` row routes
to `gemini/gemini-3.6-flash`. Verified end to end rather than assumed: a photo
ingests through `validate → sanitise → moderate → segment → matte → classify →
tag → embed → dedupe → persist` in **3.9s**, and the tags come back from Gemini.

`extractor_version` separates the eras cleanly, which is why it exists:

| version | rows | what |
|---|---|---|
| `seed-demo-v1` | 2420 | random attributes — never eval on these |
| `tag-vlm-v1` | 179 | the mock: `kurta` for everything |
| `tag-vlm-v2` | 34 | real Gemini |

**The DPA is still outstanding and this is still `gemini/`, not `vertex_ai/`.**
AI Studio's terms permit training on submitted content, and what is submitted is
photographs of someone's clothes. That is acceptable for development and for
eval against the golden set — it is NOT the production posture, and the note in
`litellm/config.yaml` stands: production changes the prefix, and the prefix is
the only line that changes.

### Two bugs that only real tags could expose

Both had been latent since Phase 4, both were invisible against the mock, and
both were found by looking at what the first real run actually wrote.

**1. Every garment was flagged for review — 17 of 17.** The VLM schema's
`confidence` object listed `properties` for all six fields but no `required`.
`properties` constrains the shape of a key that IS present; it does not demand
one. Gemini duly returned confidences for `subcategory` and `warmth` only, while
still emitting VALUES for all six. The review gate reads a missing confidence as
`0.0` — below every threshold — so everything routed to review.

A review queue containing the entire wardrobe is not a review queue, and the
correction rate read through it, which is Phase 5's go/no-go, would have been
noise. Fixed by requiring every key, derived from `vlm_fields()` so re-tiering a
field cannot silently drop it. Measured on identical input: **100% → 0%** review
rate, all six confidences present in all 17 rows.

The gate's conservative default is still correct — a missing confidence SHOULD
mean review — but it is no longer silent. `_parse` now reports `unscored` and
logs it, because the failure here was never the default; it was that nothing
anywhere said why the whole wardrobe was in the queue.

**2. `cache_hit` was false on every call ever made.** The client read
`x-litellm-cache-hit`; LiteLLM (1.100.1) does not emit that header. Measured
against the running gateway, the real signal is the presence of
`x-litellm-cache-key`:

    fresh call   3066ms   no  x-litellm-cache-key
    repeat       1.3ms    has x-litellm-cache-key

This mattered twice. **Cost:** a cached response still carries a full
`x-litellm-response-cost`, so hits were mirrored into `model_calls` at list
price — 21 real calls and 13 hits in one run, with **40% of recorded spend
attributed to calls the provider never charged for.** "Cost per garment
ingested" was measuring the wrong thing in the only phase that asks for it.
**And the Phase 7.3 SLI:** the rationale-cache hit rate alerts below 50%;
against a constant `false` it would have sat at 0% forever.

**This is the third monitoring signal in this project that was structurally
incapable of firing**, after the Phase 5 ops alerts (RLS returning zero rows to
a counting query) and the nightly precompute's tenant query (same cause). All
three share one shape: *a value read from an external system that silently
defaults to the reading that means "fine".* RLS returns no rows rather than
erroring; a missing header reads as `""` rather than raising. That is a class,
not three coincidences, and it is worth a rule — when a signal's absence and its
healthy value are the same value, assert the signal can fire, in the direction
that fires.

289 tests (was 286); `ruff`, `ruff format` and `mypy --strict` clean across 83
source files; `make verify` 24/24. All three new tests were checked by mutation:
reverting either fix fails the corresponding test.

### Why n=1

The 20-user bar was written as startup validation — "will this work for people
who are not me". This project is a personal stylist for its owner and is not
asking that question, so the bar now is the owner's own wardrobe, catalogued in
full.

**What this costs, stated plainly.** n=1 measures whether the pipeline is
CORRECT. It cannot measure whether it GENERALISES, and every accuracy or
correction number from here describes one person's clothes, one camera, and one
reading of the taxonomy. The ≈20%-correction-rate trigger still works for what
it was really for — catching a field that is systematically wrong — but it can
no longer distinguish "the model is bad at sarees" from "I own unusual sarees."

**What it does not change.** Tenancy is proven by the test suite and not by
having tenants, which is why RLS was built that way in Phase 1; the
cross-tenant criterion is unaffected. And the blind eval keeps its two outside
raters: a single rater grading outfits built from their own wardrobe cannot
separate "this is a good outfit" from "this is what I would have picked
anyway", which is the exact bias that eval exists to detect.

### What is actually left

Two things, and neither is code:

1. **Photograph the wardrobe.** This is now the largest open item in the
   project. It unblocks, in one step: Phase 5's ingest criterion, the
   correction rate, cost per month, and Phase 6's blind eval.
2. **The golden set.** Unchanged and still independent of user count — it is
   labelled data, not users, and `eval/golden/labels.jsonl` still does not
   exist. All three eval entry points still exit **2**.

---

## Phase 7 — LLM rerank and validator (built 2026-09-15)

All three steps built. **323 tests** (was 289); `ruff`, `ruff format` and
`mypy --strict` clean across 86 source files.

### 7.2 first, not 7.1

The plan lists the reranker first. The validator was built first anyway,
because it is what makes an LLM on a user-facing path acceptable at all —
building the caller first leaves a window where model output flows through
unguarded, and the validator is pure so it needs nothing that does not exist
yet.

`packages/stylist_domain/validator.py` implements §C3's six assertions in
order, stopping at the first failure. Order is part of the contract, not an
implementation detail: a response that breaks assertions 2 and 5 must report
`unknown_id`, because the reject metric is a regression signal and one that
reports whichever rule happened to be checked last says nothing about what
changed.

**The test that defines the phase** passes in both halves: a fabricated garment
id is rejected with `rule="unknown_id"` and the user gets the deterministic
ranking; a real, active, well-formed id belonging to **another tenant** fails at
the same assertion, because the input id set is per-request and another
tenant's garment was never in it. One assertion covers both, so there is no
separate cross-tenant path to forget, and RLS is not asked to do the
validator's job.

Three decisions worth recording:

- **Assertion 6 demotes, it does not reject.** An unsure model is not a lying
  model. Discarding five good rerank decisions because the sixth scored 0.4
  punishes honesty; demoted outfits keep their place but rank below the
  deterministic order.
- **Slot legality re-uses `slots.evaluate()`.** A second implementation could
  disagree with the generator's, and then the validator would reject outfits
  the deterministic path itself produced — a fallback that rejects its own
  fallback.
- **A seventh check the plan does not list: `unknown_outfit`.** Assertion 2
  stops invented garments but not novel COMBINATIONS of real ones. A
  recombination passes every id check and is often slot-legal, yet has never
  been through the scorer. Reranking reorders; it does not design.

Body-shaming terms (assertion 5) deliberately include `slimming`, `flattering`
and `hides` — ordinary fashion copy, every one of which presupposes the
wearer's body is a problem the garment solves. The classifier half of §C3's
"regex + small classifier" is **not built**, and that is a real gap rather than
a decision: a term list cannot catch a sentence that is cruel without using a
listed word.

### 7.1 — and the budget that does not exist

§7.1 specifies a 1200ms hard timeout inside §B1's 1500ms SLO. **It is not
reachable.** Measured against `gemini-3.6-flash`, three runs each:

| shape | latency | tokens |
|---|---|---|
| top-8, ids echoed, `reasoning_effort: low` | 5273-6369ms | ~2550 |
| same, `reasoning_effort: none` | 3930-5178ms | ~2470 |
| top-8, referenced by index (the plan's own token budget) | **2974-3618ms** | 1085 in / 370 out |

Reasoning is not the bottleneck; generation is. Even cut to exactly the
plan's "~1.2k in / 300 out", the floor is ~3.2s — 2.7x the budget.

**Resolution: the model call moved off the request path.** The nightly
precompute (6.4) now also reranks and writes rationales into the 7.3 cache, so
the morning request is a cache read. Measured end to end: `ranking_source:
"cache"`, **6.4-22ms**, real model rationales, zero provider calls. The
1200ms live path remains for contexts nobody precomputed, where it is a
DEGRADE SWITCH rather than a deadline — a user picking an unusual occasion
gets deterministic order in 1.3s instead of an empty screen.

`reasoning_effort: none` and index-based referencing are both measured wins
(~40% fewer output tokens) and are **not implemented** — they matter for cost
rather than for the request path now that the call is nightly, and echoing ids
keeps the validator's assertion 2 checking what it actually claims to check.

### Four bugs, three of them silent

**1. The tie-break that made the whole cache unreachable.** `suggest()` sorts
by `(-score, garment_set_hash)` precisely so the nightly job and the request
path agree — Phase 6 wrote that rule down. The SQL reading its output ordered
by `score DESC` alone. On real data scores tie constantly (five outfits at
0.717), so Postgres returned an arbitrary set: the nightly job reranked one
arbitrary top-8 and the request read a different arbitrary top-5. **Every**
rationale lookup missed, and §7.3's "hit rate ≥ 50%" would have been
unreachable for a reason no dashboard could show.

**2. Outfits the model omits were silently dropped.** The validator checks that
everything returned was sent; nothing checked the reverse. A model answering
with three of the eight it was given is schema-valid, passes all six
assertions, and deleted five wearable outfits from the user's list with no
error anywhere. Found only because a test fixture was accidentally larger than
the fake model's reply.

**3. `max_tokens` truncation, for the second time in this project.** A uuid
costs ~22 tokens, so eight outfits of three garments is ~530 tokens of pure id
echo before a word of rationale. At 1024 the JSON truncated mid-object and the
whole rerank was discarded as unparseable — presenting as the MODEL failing
rather than the ceiling being too low. `litellm/config.yaml` carries the
identical note for tagging at 2048.

**4. Requiring a FULL cache hit meant never using the cache.** The model
returns rationales for only ~3 of 8 outfits, so a context is never fully
cached; every request missed and made a live call. It only looked fast because
LiteLLM's own response cache was absorbing the repeat — a 7-day gateway TTL we
do not control, propping up a design that was quietly not working. The request
path now serves on ANY hit and templates the gaps; the nightly path passes
`serve_partial_cache=False` so it still fills them, or coverage would freeze
forever at whatever the first run produced.

### There is deliberately no reranker mock

Tagging has one. The reranker cannot: its response must echo per-request uuids,
so a fixed `mock_response` can only ever contain ids outside the input set.
Every local request would fail assertion 2 and pin `validator.reject` at 100%
with `rule="unknown_id"` — burying this phase's earliest quality signal, which
has a <2% exit criterion, under noise we manufactured. With no key the real row
simply fails and the suggestion degrades, so the ladder is the uncredentialed
default. The ACCEPT path is covered by 20 tests against a fake gateway.

### Cost, and why the nightly job checks the cache first

Four occasions x 30 nights x ~$0.003 is **~$0.36/tenant/month** against a
`free_tier_monthly_budget_usd` of **0.15**. Re-reranking nightly would exceed
the per-tenant budget by 2.4x and turn a feature into an outage (§B3). Because
`rerank()` reads the cache before calling, a tenant whose wardrobe and outfits
are unchanged costs zero provider calls until the 7-day TTL expires.

### Exit criteria

- [x] **Kill the reranker provider → suggestions still return, non-5xx,
      template rationale** — asserted for each failure mode separately
      (provider down, read timeout, unparseable JSON, fabricated id), because
      they reach the fallback through different code.
- [x] **Fabricated-ID and wrong-tenant-ID tests both pass** — and the metric
      increments with `rule="unknown_id"`.
- [x] **p95 `/suggestions` ≤ 1500ms warm** — **6.4-22ms** from cache. Cold,
      un-precomputed context: **1.2-1.5s**, degrading rather than reranking.
- [ ] **Rationale cache hit rate ≥ 50% after a week of real traffic** —
      NOW MEASURABLE, and currently **FAILING at 0.45 over 92 lookups**, which
      is the SLI doing its job on the first day it could. Cause: the model
      returns rationales for only ~3 of the 8 outfits it is sent, so the top-N
      a request reads is never fully covered. Fix the coverage, not the
      threshold. A week of real traffic still needs a real wardrobe.
- [ ] **`validator.reject` rate < 2% overall** — NOW COMPUTABLE. Reads
      `attempts=2 accepted=2 rejected=0` against a live stack; before, only
      rejections were counted and the rate had no denominator at all.
      Unmeasured until 20 samples, and reported as `measured: false` rather
      than as a pass.
- [ ] **Load test: 10x projected peak, no SLO breach** — deferred to P9 with
      the rest of the capacity work, which is where the plan puts retuning
      against real traffic. Not solvable now for a second reason: "projected
      peak" for one user is not a number that exists.

### The fourth instance, and this time I wrote it

Both rate criteria above were unmeasurable when first shipped, in two distinct
ways, and both are the shape this project has now recorded four times — a
signal that is live, green, and structurally incapable of answering its own
question.

**The reject rate had no denominator.** Only rejections were counted. That can
report a tally and never a RATE, and "< 2% overall" is a rate — so the
criterion could not have been evaluated however much traffic arrived. Every
validation is now counted, accepted ones included.

**The cache SLI counted requests, not lookups.** `hit` and `miss` were each
incremented once per request, so one request looking up 5 outfits and finding 1
reported a 50% hit rate against a true 20% — landing exactly on the threshold
the alert fires at. Both are now incremented BY COUNT, which is why
`incr_bucketed` gained an `amount`.

**And nothing read either counter.** No endpoint, no alert; the SLI existed
only in Redis. `GET /ops/rerank` now reads both over day buckets, shares its
key construction with the writer (`metric_keys`) so reader and writer cannot
drift, applies the same fewer-than-20-samples guard as the Phase 5 alerts, and
reports `measured: false` rather than `ok: true` over thin data — because
letting "not measured" become "measured and fine" is the exact failure the eval
harness exits 2 to avoid.

Kept separate from `/ops/alerts` deliberately: that endpoint carries the four
alerts §D3 specifies, with "resist adding more" written next to them. These are
exit criteria tracked toward a threshold, not pages anyone should be woken for.

`tests/test_rerank_metrics.py` (9 tests) asserts the FIRING direction and a
correct RATE for each, not merely that a counter moved — including that 1 hit
in 5 lookups reads as 0.2 and not 0.5.

---

## Phase 8 — Boards, feedback, style vectors (built 2026-09-15)

Four of six deliverables built. **370 tests** (was 323); `ruff`, `ruff format`
and `mypy --strict` clean across 91 source files; migration `0009` reverses
cleanly.

| Deliverable | State |
|---|---|
| Feedback capture | built — `POST /outfits/feedback`, append-only log |
| Style vector + rebuild script | built — replay verified against live state |
| Preference facts | built (API); **no UI yet** |
| Compositor / boards | built — 45ms compose, warm request 6-11ms |
| Calendar → occasion | **not built** — needs Google OAuth |
| Daily push | **not built** — needs a notification provider |

### The event log is the truth, and the database enforces it

`outfit_feedback` is append-only: `stylist_app` holds SELECT and INSERT and
nothing else, asserted in the failing direction by `tests/test_feedback_append_only.py`.

**The first version of that guarantee was fake.** Migration 0001 sets
`ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE
ON TABLES TO stylist_app`, so every table created afterwards arrives with the
full set attached. `GRANT SELECT, INSERT` adds nothing and removes nothing — it
READS like a restriction and is a no-op, and `UPDATE outfit_feedback` succeeded
as `stylist_app` with it in place. It needs a REVOKE. This matters beyond
tidiness: the style vector, the wear-through rate and everything Phase 11 will
learn are replays of this table, and one silent UPDATE makes the replay
disagree with live state for a reason nobody can reconstruct, because the
evidence is the thing that got overwritten.

A second correctness fix in the same migration: `feedback_reason` originally
carried an `other` value, which `tests/test_taxonomy_enums.py` forbids across
every enum in the database. The rule is right — an `other` bucket absorbs
everything that does not fit and then tells you nothing, while `reason` is
already nullable and NULL is the honest encoding of "unspecified".

### The style vector, and the exit criterion

`apply_event` is pure. The live handler and `scripts/rebuild_style_vectors.py`
are not two implementations that must agree; they are one called twice, which
is the only reason "the rebuild reproduces live vectors" is achievable at all.

Verified against the live stack: three events posted over HTTP, then
`rebuild_style_vectors.py --check` → *all 1 tenant(s) match the event log*.
Then the stored vector was corrupted to prove the check can fail:

    --check   DIVERGED (max abs diff 1.29e-01, 3 events in log)   exit 1
    rebuild   rebuilt 1 tenant(s) (1 had diverged before this run)
    --check   all match                                           exit 0

**One deviation from the plan's wording.** It says "assert equality"; this
compares with a **1e-5 tolerance**. The live vector round-trips through
pgvector's float4 once per event while the replay holds float64 throughout, so
they differ in the last bits BY CONSTRUCTION — bit equality would fail for a
non-bug, and a criterion that cannot pass teaches everyone to ignore it. 1e-5
is far tighter than any real divergence; the deliberate corruption above
registered 1.29e-01.

`feedback_tenants()` is a SECURITY DEFINER function for the same reason
`precompute_tenants()` is: the script enumerates tenants BEFORE it can set a
tenant context, and as `stylist_app` against FORCE RLS a plain
`SELECT DISTINCT user_id` returns zero rows with no error. It would have
reported "0 tenants, all consistent". Third instance of that trap here.

### Boards

`stylist_domain/board.py` is pure — bytes in, bytes out — so the layout is
testable without a bucket and the domain stays importable without
infrastructure. Slot-aware rather than a grid: an outfit reads as an outfit
when the top sits above the bottom and the shoes under both, and
`upper_layer` sits BESIDE `upper_base` rather than over it, because a board
whose job is "show me what I am wearing" must not conceal one of the things
being worn.

Deterministic, which is what makes caching by `garment_set_hash` sound: a cache
hit is not merely fresh enough, it is byte-identical to what a re-render would
produce.

A garment with a missing or corrupt cutout raises rather than being skipped.
Omitting it produces a PLAUSIBLE wrong answer — a three-garment outfit drawn as
two looks like a layout choice, so nobody investigates — and the board's entire
claim is accuracy to what you own.

Measured on the live stack:

| | |
|---|---|
| compose only (4 garments) | **45ms** (plan budgets ~80ms) |
| cold request (fetch cutouts + compose + store) | 629ms |
| warm request (cached) | **6-11ms** |
| PNG fetch, i.e. what a CDN serves | 4-47ms, ~69KB |

### The ordering bug, in its third and subtlest form

Boards and rationales are warmed nightly so the request path is a lookup. The
first version warmed the top-8 of `suggest()`'s in-memory ranking — and covered
only **3 of the 8** the endpoint actually serves.

`suggest()` ranks by a Python float; `outfits.score` is `NUMERIC(8,6)`. Two
outfits differing at the 7th decimal are DISTINCT in memory (so ordered by
score) and EQUAL once stored (so ordered by the hash tie-break). The two
sequences diverge, so five of every eight requests paid a 629ms cold render for
a board the nightly job had already rendered under a different hash.

Both warms now read the top-N back through `TOP_N_SQL` — the same query and
ORDER BY the request uses — so the two agree by construction rather than by two
orderings happening to coincide. After the fix: **8 of 8**, and the rationale
cache hit rate moved from **0.45 (firing)** to **0.50 over 72 lookups from a
cold cache**.

This is the same family as the Phase 7 tie-break bug and the Phase 6 note in
`suggest()` about ranking identically across runs. The lesson that generalises:
**anything that pre-computes for a reader must order by the reader's query, not
by its own.**

### A claim I made and had to withdraw

I reported ~2,273 garments with dangling `cutout_key`s, having compared 3,158
garments-with-a-key against 885 stored objects. That was wrong: `seed_demo.py`
SHARES cutout keys across garments, so there are only **746 distinct keys and
every one resolves**. Zero dangling. The compositor's fail-loudly behaviour is
still right, but it is justified by robustness rather than by a broken dataset,
and the code comments were corrected to say so.

### Exit criteria

- [x] **`rebuild_style_vectors.py` reproduces live vectors from the event log**
      — verified both directions, including that the check FAILS on a corrupted
      vector (exit 1) and passes after a rebuild (exit 0).
- [~] **Boards render < 200ms p95 from CDN** — warm request **6-11ms**, PNG
      fetch 4-47ms, both far inside the budget. Marked partial rather than met
      because "from CDN" is not measurable without a CDN; there is none in this
      stack, and MinIO is standing in for one.
- [ ] **Wear-through rate measured** — `GET /me/wear-through` computes it from
      the event log (distinct suggested outfits, not events, so indecision
      cannot inflate it) and returns `None` rather than `0.0` when nothing was
      suggested. Needs real use to produce a number.
- [ ] **V1 in daily use by the owner for 4 consecutive weeks** — needs the
      wardrobe photographed, still the largest open item in the project.
- [ ] **Wardrobe coverage trending up week over week** — not built; it is a
      time series over feedback and needs weeks of it to mean anything.

### Calendar → occasion (built once credentials arrived)

`config/calendar_rules.yaml` maps event titles to occasions as REVIEWED DATA,
not code — the same argument as `outfit_rules` and `scoring.yaml`. Deliberately
keywords rather than a model: the plan requires that a below-threshold match
"fall back to default AND SAY SO", which means the UI must show WHY. "Matched
'standup' → office_casual" is showable and correctable; "the model said 0.62"
is not. It also keeps calendar sync off the paid path and out of the 1500ms
budget entirely.

`GET /calendar/today` never returns a bare occasion: occasion, confidence,
`confident`, `is_fallback` and a renderable explanation arrive TOGETHER, so a
client cannot show the answer without the caveat in hand. The floor is 0.60 and
`casual_outing` sits at 0.55 on purpose — "Lunch" must not decide what you
wear. Several events in a day resolve to the DRESSIEST confident match, because
nobody changes between a standup and a wedding and being overdressed is the
recoverable error.

Security decisions worth keeping:

- **`state` is a signed, expiring, audience-scoped JWT.** The callback carries
  no bearer token — a browser arriving from Google has none — so `state` is the
  only thing identifying the user. All four rejection paths (forged, wrong key,
  expired, wrong audience) are asserted. The audience check matters
  specifically because our own access tokens share the signing secret.
- **Only titles leave Google.** The `fields` parameter requests `summary` and
  attendance status and nothing else, so attendees, descriptions and locations
  are never transmitted rather than received and discarded. The API returns
  `events_seen: 3`, never the titles.
- **Revocation records whether Google CONFIRMED it.** "We deleted our token" is
  not the same claim as "our access is gone", and §C5 asks the second one.
- **Scope is verified on return, not assumed** — Google lets a user uncheck
  scopes on the consent screen, and a narrower grant would otherwise surface as
  a 403 mid-sync with nothing pointing at the cause.

### Daily push (`w-notify`)

FCM HTTP v1, written against the API directly rather than via `firebase-admin`
(~20 transitive dependencies and a credential-discovery layer that reads
ambient environment). `pyjwt[crypto]` is the one dependency added, for the
RS256 service-account assertion.

**"07:00 local" is the whole design problem.** There is no single moment that is
07:00 — it happens 24+ times a day across zones — so the cron runs EVERY HOUR
and each run sends only to tenants for whom it is currently 07:00 where they
are. The zone comes from the DEVICE, falling back to `user_profile.timezone`:
local is a property of where the phone is, so a user who flies to London should
get their digest at 07:00 there.

**Sending twice is unforgivable, so the guard is in the database.** `push_send`
has a unique index on `(user_id, kind, sent_on)` with `sent_on` as the user's
LOCAL date, and the row is inserted BEFORE the send. A crash after the insert
costs one missed digest; a crash after sending but before recording costs a
duplicate. Only one of those gets the app uninstalled. The table is append-only
by REVOKE for the same reason `outfit_feedback` is — and for the same reason a
narrower GRANT would not have worked.

A token FCM reports as `UNREGISTERED` is disabled with the reason recorded,
not deleted and not retried: retrying costs quota every morning for a device
that no longer exists, and a deleted row cannot answer "why did my
notifications stop".

`push_tenants()` is the fourth SECURITY DEFINER function in this project for
the fourth instance of the same trap — a cross-tenant `SELECT` as `stylist_app`
against FORCE RLS returns zero rows with no error, so the sender would have
reported "nobody to notify", indistinguishable from success.

### Credential handling, after a near miss

A Firebase service-account JSON was pasted into a chat during this work. That
key was rotated. Two things changed as a result, both of which should have been
in place first:

1. `.gitignore` now covers `secrets/`, `*service-account*.json`,
   `*adminsdk*.json` and `*-firebase-*.json`. Google's default download name is
   `<project>-firebase-adminsdk-<hash>-<id>.json`, which matches no obvious
   pattern — verified that the exact filename is now ignored at the repo root.
2. The credential is referenced by PATH (`FIREBASE_CREDENTIALS_FILE`), never
   pasted into an env var. A multi-line PEM in an env var is mangled
   differently by every shell, .env parser and CI secret store, and the usual
   "fix" of stripping newlines produces an unparseable key and a stack trace
   three layers from the cause.

The refresh token in `calendar_link` and the service-account file are both
KNOWN GAPS for production: plaintext under RLS and a mounted file respectively.
Encrypting them needs a KMS and a key-rotation story, which is P9.

### What is still not built

**The preference-facts UI.** The API is there. The plan's argument is that
"legibility buys trust faster than accuracy does", and an endpoint nobody can
see buys neither — this is now the last unbuilt piece of Phase 8.

---

## Phase 9 — erasure, export, and the restore drill (built 2026-09-15)

Four of Phase 9's six exit criteria are engineering rather than traffic, and
three of those are now done. **456 tests**; `ruff`, `ruff format` and
`mypy --strict` clean across 104 source files; migrations 0012 and 0013 both
reverse cleanly.

### Erasure (§C5) — and it cannot be a `DELETE CASCADE`

A user's data is in Postgres, in object storage with versioning ON, in a model
provider's cache, in a Google Calendar grant and in Firebase's device registry.
No transaction spans those, so `erasure_request` is a resumable saga: seven
steps, each separately durable, completed steps recorded and SKIPPED on retry.

`DELETE /me?confirm=DELETE` returns 202 and the account is **already dead** —
`deleted_at` is set inside the request and `current_user` returns 410 from the
next call. Verified live. Steps 2-7 run against a 30-day SLA, retried twice an
hour, because the deadline is legal rather than operational.

Rows are deleted LAST: every earlier step reads them to find the objects,
tokens and keys to purge elsewhere. Deleting them first strands the objects
permanently — unreferenced, unerasable, invisible to the next audit.

**`delete_all_versions` is the load-bearing piece.** The bucket is versioned
(Phase 1 turned it on so a bad migration could not destroy a wardrobe), so
`delete_object` writes a delete MARKER and leaves the bytes recoverable. That
reads as deletion in review and is not erasure. It also aborts orphaned
multipart uploads, which `list_objects_v2` cannot see and storage bills for.

**`unpurgeable` is what makes the record honest.** No provider in this stack
offers a per-user cache purge, so every erasure discloses that rather than
claiming a clean sweep. §C5's words are "record what could NOT be purged (and
disclose it)"; a saga reporting success while a provider still holds the data
is worse than one that says so.

### Four bugs the erasure tests caught

**1. Step 2 silently did nothing.** `_purge_providers` reads `user_profile`,
`calendar_link` and `device_token` — all FORCE RLS — from a system session with
no tenant context. Every read returned ZERO ROWS WITH NO ERROR, so the virtual
key, the calendar token and the device tokens were all left LIVE while the saga
reported success. **Sixth instance** of that trap in this codebase.

**2. Append-only collided with Art. 17.** `outfit_feedback` (0009) and
`push_send` (0011) have DELETE revoked from `stylist_app` because they are the
logs every derived thing replays. Erasure must delete them. Granting DELETE
back would dissolve the guarantee for every code path to serve one, so row
deletion goes through a single SECURITY DEFINER `erase_user_rows()` — named,
auditable, and the only exception.

**3. A storage outage would have been recorded as permanent.** The first
version caught per-prefix failures and filed them as `unpurgeable`, confirming
an erasure with the objects still in the bucket. "Storage was down" is
transient and belongs in the retry path; conflating it with genuinely
unreachable data turns an outage into a documented lie.

**4. `processed_keys` has no user column at all** — it is
`(idempotency_key, consumer, created_at)`, and the key is a value the CLIENT
generated. It cannot be scoped and carries no personal data, so it is excluded
and REPORTED as `not_user_scoped` rather than silently skipped.

### Export, made asynchronous

The first version built the ZIP in memory inside the request. That works at
nine garments and cannot work at two thousand: the archive is every cutout the
user owns, so it grows with the wardrobe while the request timeout does not.

Now `POST /me/export` writes an `export_request` and an outbox event in ONE
transaction; the worker builds the archive **on disk** and uploads it;
`GET /me/export/{id}` mints a presigned link per request. Measured on the real
wardrobe: **19.7 MB, 418 entries, 400 images, CRC clean**.

  - **Credentials are redacted.** An export lands in a downloads folder, and a
    calendar refresh token or FCM registration token STILL WORKS. Asserted by
    checking the raw values appear nowhere in the archive bytes.
  - **Exports expire for real.** §C5 says "7-day link", but a presigned URL
    expiring only stops new downloads — the ZIP stays in the bucket, a complete
    second copy of the wardrobe, i.e. exactly what erasure works to remove. An
    hourly sweep deletes the object (all versions). A failed sweep leaves the
    record so the next run retries; marking it deleted would mean nothing ever
    tries again.
  - **One in flight per user**, enforced by a partial unique index rather than
    a handler check, because a double-tap on a slow connection is two
    concurrent requests and a handler check races itself.

### The aborted-transaction trap, three times in one day

After a constraint violation or a failed statement, the Postgres transaction is
dead and every subsequent command raises `InFailedSQLTransactionError`. Three
separate places recovered from an error by querying inside the session that had
just failed:

  - the erasure saga's error handler (masked every real error with a confusing
    one about transaction state)
  - the export builder's failure path
  - `POST /me/export`'s duplicate lookup — which returned **500 instead of
    409**, measured against the running stack

All three now use a fresh session. The rule: *recovery must not depend on the
transaction that failed.*

### The restore drill — the number, and what it is not

    make backup          logical dump -> object storage
    make restore-drill   restore into a scratch DB and VERIFY it

Measured, twice, on the live 47 MB database:

| | |
|---|---|
| dump | 9.0 MB in **1.3s** |
| **restore** | **2.8s** |
| verified | 20,421 rows / 21 tables, 14 policies, 132 functions, 12 enums, 2 extensions, 14 FORCE RLS tables |

**A drill that only restores proves nothing.** `pg_restore` exiting 0 means the
file was replayed, not that the database works, so the drill compares the
scratch database against the source object by object. Row counts are the
obvious check and the least likely to catch a real problem; the ones that
matter are the policies (a restore that drops them yields a database where
every tenant sees every other tenant, and it looks completely healthy) and the
SECURITY DEFINER functions the precompute, the erasure saga and the ops alerts
all depend on.

**Mutation-checked, because a drill that cannot fail is a ritual.** Dropping one
policy and emptying one table from a restored copy:

    wear_log: 10963 -> 0
    policies: 14 -> 13

Both detected.

**The drill also found a real DR hazard.** `pg_restore` exited 1 with
`unrecognized configuration parameter "transaction_timeout"` — the host's
`pg_dump` is **17.10** against a **16.15** server. Benign in that direction (one
ignored SET) and data-losing in the reverse. Nobody checks tool versions during
an incident, so the drill now prints them every run and warns on a major
mismatch.

### What the number is NOT

2.8s is a **floor on the RTO, not the RTO**. The drill restores from local
storage, on the same machine, into the same Postgres instance. It does not
exercise a second region, a cold instance, DNS, or — the largest term by far —
the time to NOTICE. It is reported as a floor in the script's own output so it
cannot be quoted as an RTO by someone reading only the number.

**The backup is in the same blast radius as the data.** §C5 says "nightly
logical dump to the SECOND CLOUD ACCOUNT"; there is one account, so this
survives a bad migration and not a compromised or deleted account. That is a
real gap, printed by `make backup` on every run rather than left in a document.

### Exit criteria

- [x] **Erasure verified absent across systems** — asserted by querying back,
      not by the saga reporting success. Rows, object versions, provider grants
      and the audit record are each checked independently.
- [x] **Restore drill completed, time recorded** — **2.8s**, verified, and
      mutation-checked so the verification can fail.
- [x] **Every paging alert has a runbook** — `docs/runbooks.md`, one per
      alert, each with symptom / three likely causes / verification query /
      mitigation / rollback / escalation.

      **Every query and command in it was executed against the live stack**, by
      exit code rather than by eye. A runbook whose query errors at 03:00 is
      worse than no runbook: it costs the minutes you had and teaches whoever
      hit it that the document is decoration.

      It was also walked against a LIVE FIRING alert rather than a hypothetical
      one — `dlq_age` and `ingest_success` were both firing during the write-up
      — and step 1 ("read `stage_attempts`, it names the stage") produced the
      cause in one query: `{"segment": 3}` /
      `IndexError: tuple index out of range`, the flat-lay bug fixed earlier
      the same day.

      Five runbooks, not four. The fifth is `erasure_sla`, which is not one of
      §D3's alerts and pages louder than any of them: the others cost money or
      latency, that one has a regulator behind it. §C5's "alert at 7 days, page
      at 25" is the only deadline in this product that is not ours to move.

      `/ops/rerank`'s two SLIs are deliberately NOT pages, and the runbook says
      so. §D3's instruction is "resist adding more; unactionable alerts train
      people to ignore pages", and a cache hit rate does not need anyone woken
      up.
- [x] **Game day run; findings ticketed** — six scenarios against the live
      stack on 2026-09-15. Three passed as designed; three produced findings,
      two of which are real and one of which is a judgement call. All services
      restored, 456 tests green afterwards.

      **Passed as designed**

      - **ml killed.** `/readyz` reported `ml: unreachable` and still returned
        **200** — deliberately non-fatal, because failing readiness would turn
        a designed degradation into a total API outage. Suggestions and
        wardrobe reads unaffected. An ingest started during the outage waited
        at `sanitised` with `attempt 1 not consumed`, did NOT enter the DLQ,
        and **resumed to `classified` on its own** when ml came back. That is
        the whole backpressure design working end to end.
      - **LiteLLM killed.** Suggestions returned 200 with the ranking degraded.
        The paid path going down costs a rationale, not a screen.
      - **Three poison images.** All three reached `rejected` with **0 in the
        DLQ** and a user-visible reason ("file is not a JPEG, PNG, WebP or HEIF
        image (checked by magic bytes)"). Healthy work kept flowing. Confirms
        the Phase 2 decision to reject a corrupt file after ONE attempt rather
        than burning three.

      **FINDING 1 — a database outage is indistinguishable from a bug.**
      With postgres stopped, `/readyz` correctly returned **503**, but every
      request returned **500**: `/suggestions`, `/auth/login`, all of it. A
      dependency being down is a 503 — "try again" — while a 500 says "we have
      a bug". They are different instructions to a client, to a retry policy
      and to whoever is paged. Worse, the `api_5xx` alert cannot tell them
      apart, so a database outage and a bad deploy produce the identical
      signal. The runbook anticipated the symptom ("5xx on every endpoint at
      once") but the status code should carry that distinction itself.

      **FINDING 2 — the compose healthcheck reports healthy while every
      request fails.** It probes `/healthz`, which does not touch the database.
      With postgres down the container stayed `Up (healthy)` for the entire
      outage. `/readyz` does the right thing, so a Kubernetes readiness probe
      would pull the pod — but nothing in the compose stack is watching it, and
      "healthy" is what a human glances at first.

      **FINDING 3 (judgement call) — `redis-cache` is fatal to readiness and
      probably should not be.** With it stopped, `/readyz` returned **503**
      while `/suggestions` returned **200** — the rationale cache is an
      optimisation and the endpoint degrades to template text without it. This
      is the exact argument already made for ml and recorded in the
      cross-phase pass: failing readiness on a degradable dependency converts a
      degradation into a total outage. `redis-queue` is arguable the other way
      (without it no ingest can be enqueued, though presign still returns 200,
      which is its own half-working state worth a look).

      **All three findings are FIXED and re-tested against live outages**
      (`tests/test_game_day_fixes.py`):

      | | before | after |
      |---|---|---|
      | Postgres down | every request **500** | **503** + `Retry-After: 5` |
      | Postgres down | container `Up (healthy)` | container `Up (unhealthy)` |
      | redis-cache down | `/readyz` **503** | `/readyz` **200**, cache in `dependencies` |

      **And fixing finding 1 produced a finding of its own.** Registering the
      handler for SQLAlchemy's `OperationalError` and `InterfaceError` looked
      correct, passed a structural test, and STILL returned 500 to every
      request. Re-testing against a stopped Postgres showed why: the exception
      reaching the handler was a raw
      `socket.gaierror: [Errno -2] Name or service not known` — DNS failing
      before a connection exists, so there was nothing for SQLAlchemy to wrap.
      `socket.gaierror` and `ConnectionError` are now registered too;
      `OSError` deliberately is not, because a missing file or a full disk is
      not "retry shortly".

      That is the game day's real lesson in miniature: the structural test
      passed, and only re-running the outage showed the fix did not work.

      **Finding 2's re-test also needed patience rather than a fix.** The
      healthcheck is 3s x 20 retries, so it takes 60s to flip; a 35s
      observation reported it as still broken when it was already correct.

      **A seventh finding, about the process rather than the system:** the
      `zsh` unquoted-scalar trap that the cross-phase pass and Phase 5 both
      already document bit again during this exercise — `DC="docker compose
      ..."; $DC ps` fails silently. Knowing about it has now failed to prevent
      it three times, which is an argument for a checked-in helper rather than
      a note.
- [x] **Error budget policy signed off** — **SIGNED 2026-09-17 by Suraj Kumar,
      CLAUSE 3 ONLY** (`docs/error-budget.md`). The criterion is "signed off",
      not "a policy exists", and §B1's own line is that an unagreed error
      budget is not a control.

      **Clause 3 (data durability) binds today. Clauses 1 and 2 are agreed in
      principle and DORMANT** — they are written against a burn rate nothing
      computes, and a clause with no trigger is not a control. They activate
      automatically when burn-rate alerting lands; no re-signing is required,
      because the commitment was made and only the trigger was missing.

      Marking this `[x]` is therefore a PARTIAL claim, stated as one. The box
      is ticked for the clause that is enforceable and explicitly not for the
      two that are not.

      Drafting it surfaced why it cannot honestly be signed in full yet:

      - **Three of six SLIs are measurable today** (suggestion availability,
        suggestion latency, ingest success/completeness). **Tag accuracy is
        not** — it needs the 500-image golden set, and all three eval entry
        points still exit 2. **Data durability is partial** — the restore drill
        passes, but backups sit in the SAME storage account as the data.
      - **There is no burn-rate alerting.** The four §D3 alerts are threshold
        alerts; nothing computes budget consumption over a rolling 30 days. So
        "freeze at 50% consumed" has no number that ever reaches 50%, which is
        exactly the decoration §B1 warns about.
      - At n=1 with no traffic, clauses 1 and 2 will not bind for months. The
        clause that can bite today is **data durability**, and it is the one
        worth agreeing to now because that failure does not need traffic.

      The draft recommended signing the durability clause now and deferring the
      other two until there is a measurable burn rate — the only option that is
      both agreed and true. **That is what was decided**, on 2026-09-17.

      **What the signature now obliges:** clause 3 is signed over a gap that is
      still open. Backups are logical dumps into the SAME storage account as
      the data they protect, so they survive a bad migration but not a deleted
      or compromised account; §C5 asks for a second account and there is one.
      Signing over that gap is deliberate — the clause is what makes closing it
      a priority rather than a wish — and **closing it is the first piece of
      work this signature obliges.**
- [x] **All `# PROVISIONAL` markers resolved or re-dated** — done 2026-09-17.
      16 in code and config; **4 resolved, 12 re-dated, 0 still saying "retune
      in P9"**.

      **The four resolutions are the interesting part: they were MISLABELLED.**
      Each said "retune against real percentiles" while the comment directly
      beneath it recorded the A/B that produced the number —
      `MAX_CONCURRENT_INFERENCE=2` and `ORT_INTRA_OP_THREADS=2` were measured
      during the Phase 3 burst work ("at 4x4 the service thrashed and every
      request read-timed-out; at 2x2 the same burst drained with zero"), and
      `WORKER_MAX_JOBS` is deliberately MATCHED to the ml figure rather than
      independently tunable. A number with an A/B behind it is a result, and
      leaving it marked provisional trains everyone to read the marker as
      decoration.

      A blanket find-and-replace would have moved the goalposts on all 16 and
      hidden that.

      The remaining 12 each now carry **what specifically would resolve it**
      rather than a phase number: "p95/p99 per ml endpoint over a week of real
      ingests", "peak concurrent connections per service under real load",
      "false-quarantine and false-pass rates on real photos", "~100 real
      feedback events". A marker that names its measurement can be closed by
      whoever takes that measurement; one that names a phase just waits.

**Kubernetes is deliberately not started.** The plan's own warning is that
deploying it before there is traffic is "the most common way this project dies
at 80% done", and nothing in the remaining criteria needs it.

---

## Cross-phase fix — head-of-line blocking (fixed 2026-09-18)

**488 tests**; `ruff`, `ruff format` and `mypy --strict` clean across 107 source
files. Migration `0014_deferred_waits`, verified reversible.

### The bug

`Unavailable` — a dependency reporting "not up yet" — was handled by sleeping
IN-PROCESS for up to `UNAVAILABLE_BUDGET_SECONDS` (180s). Right about the
backpressure, wrong about where to wait. arq runs `WORKER_MAX_JOBS=2`, so two
jobs waiting on a down `ml` occupied both slots and the queue stopped draining
for every other tenant. §C6 asks for *"workers backoff; queue absorbs"* and
this absorbed nothing.

Known and unfixed since the cross-phase pass on 2026-09-10. It surfaced twice
on 2026-09-17 while working on something else: once as a `TimeoutError` when the
180s sleep outlived arq's own job ceiling, and once as ten parked jobs starving
a test of both slots.

### The fix

A job waits in-process only up to `IN_PROCESS_WAIT_BUDGET_SECONDS` (10s), then
raises `Deferred` and re-enqueues itself with a delay, releasing the slot.

**Why not defer immediately?** A re-enqueue costs a Redis round trip, a job
dispatch and a reload of the job row. An `ml` pod finishing a model load is back
inside 1-2s, and deferring every transient hiccup turns one slow ingest into
three queue hops. 10s absorbs the common case and hands anything longer to the
queue, which is the thing that scales.

### The two traps, both real

**The budget has to survive the re-enqueue.** If each round started at zero, a
permanently-down dependency would defer FOREVER and never reach the DLQ —
trading a stalled queue for an invisible infinite retry, which is worse because
nothing alerts on it. Hence `jobs.dependency_wait_s`, persisted with the delay
about to be spent outside the process so the accounting is identical whether
the wait happened in the worker or in the queue. Same class of bug as
`stage_attempts`: in-process retry state that resets when the process changes.

**arq dedupes on `_job_id`.** Reusing the relay's `outbox-{id}` returns `None`
as already-completed and the job never runs again. A bare `defer-{job_id}` works
once and is deduped from the second round on — the same bug, one round later.
So the id carries `jobs.deferrals`, making each round distinct while still
collapsing a duplicate delivery of the same round.

### Verified against a running stack

`ml` stopped, three photos ingested — which under the old behaviour meant two
jobs holding both slots and the third starved:

```
email                       state       deferrals  waited_s  dlq
defer0-4d905e@example.com   classified          5      71.0    f
defer1-67e2d6@example.com   classified          5      71.0    f
defer2-861e63@example.com   classified          4      57.0    f
```

All three progressed concurrently, each holding a slot ~6s per round
(`6.15s ← defer-79d2bbdb-...-1:ingest_photo`), and all three resumed and
completed their remaining stages when `ml` came back. None DLQ'd during a
recoverable outage.

**One process note worth recording:** the first verification run showed the OLD
log format, because `scripts/dc up -d worker` on an unchanged container is a
no-op and does not restart the Python process. Mounted source is not reloaded
source. `scripts/dc restart worker` is required, and the runbook now says so.

---

## Phase 10 — try-on: consent, render path, and the degrade (built 2026-09-17)

**477 tests**; `ruff`, `ruff format` and `mypy --strict` clean across 107
source files.

### A correction: the benchmark gates the ROUTER, not the render path

The first pass at this phase built the consent half and refused to build the
render path, on the grounds that Phase 10 opens with **"Benchmark before you
build"** — a 10-body x 16-garment grid including sarees, kurtas and a sherwani,
because "the published benchmark used Western garments; **your routing table
must come from your own grid**".

**That reasoning was wrong, and the distinction is worth recording because it
recurs.** Two different tables were being conflated:

| | maps | needs the grid? |
|---|---|---|
| ROUTING table | garment category -> which model | **yes** — a quality judgement |
| PROFILE table | model -> how that model is called | no — documented fact |

The second was read from each provider's own `/info` endpoint on 2026-09-17
and verified live. And the render path is the grid's **prerequisite**, not its
competitor: there is no way to run a 10x16 grid without a working render call.
Refusing to build it meant the benchmark could never be run at all.

### What is built, and what is still deliberately absent

**Built:** exactly ONE provider (`VTON_PROVIDER`), called over the Gradio HTTP
API. **Absent:** the router, the primary->secondary fallback, and per-category
model selection. Those are the parts that genuinely need the grid.

Three providers are profiled, all verified live:

| provider | Gradio | categories | note |
|---|---|---|---|
| `leffa` | 5 | upper / lower / dresses | the default — only one with both an explicit garment type and `dress_code` weights |
| `idm-vton` | 4 | upper only | VITON-HD; takes no category argument at all |
| `ootdiffusion` | 5 | upper / lower / dresses | `process_dc`; `process_hd` has no category |

The `/gradio_api` prefix is **detected at runtime**, not configured: it is
present on Gradio 5 and absent on Gradio 4, both are live today, and guessing
wrong turns every call into an opaque 404.

**A saree is not rendered.** `SLOT_TO_CATEGORY` maps `upper_base`,
`upper_layer`, `lower` and `full_body`, and deliberately omits `drape`. Every
candidate model was trained on VITON-HD or DressCode — both Western catalogues
— and a saree is not upper-body, not lower-body and not a dress. Mapping it to
`dresses` would return a confident, wrong picture of the owner's own body.
Which category (if any) serves a saree is exactly what the grid is for. This is
the single clearest illustration of why the plan demanded its own benchmark.

**The render is asynchronous.** 30-120s on shared hardware, queued behind other
users. It is enqueued through the OUTBOX (`tryon.requested` -> `render_tryon`),
so the request that asked for it and the job that does it share one
transaction — a direct enqueue could outlive a rolled-back transaction and send
a body photo to a third party for an outfit that was never saved.

**Consent is re-read inside the job**, not trusted from the enqueueing request.
A revocation landing in the queue window must stop the render.

**Renders go to their own `tryon/` prefix** and that prefix is in the erasure
saga. A render of the owner's body wearing their clothes is more sensitive than
either of its inputs, and it is the prefix easiest to forget because nothing
the user uploaded lives there.

### What is measured, and what still is not

`VTON_API_TOKEN` is **required in practice and currently unset**. All three
candidate Spaces run on ZeroGPU (`zero-a10g`), which rejects anonymous
programmatic calls in under a second with an empty error body — measured, not
assumed. Until a token is set, every try-on degrades to the board, which is the
exit criterion's required behaviour but not the feature.

So: **no render has been produced on this account**, per-render cost and
latency remain unmeasured, and the grid remains ungated work. What changed is
that the path to running it now exists.

### What IS built, and why this half first

The safety half does not depend on the benchmark, and it has to be right
BEFORE any image is sent anywhere:

  - `POST /me/body-photos/presign` — uploads under their own `body/` prefix
  - `POST /me/body-photos` — records the photo AND the consent in one
    transaction
  - `GET /me/body-photos` — consent state, never the object key
  - `POST /outfits/{hash}/tryon` — **always 200**, degrades to the board
  - a per-day quota, so the most expensive call in the product has a ceiling

A body photograph is not a photograph of a shirt. It identifies a person, it
is the most sensitive thing this system stores, and under try-on it would
leave our infrastructure for a generative model. Consent is therefore an
EXPLICIT flag on the request, not an implication of having uploaded —
"they uploaded it, so they must have agreed" is the reasoning that makes
consent a formality.

### Its own prefix, and the gap that created

Body photos upload under `body/` rather than `originals/`, so consent,
revocation and erasure can each target them without touching the wardrobe.

**That separation is exactly what would let an account erasure walk past
them.** The saga's prefix list did not include `body/` — nor `exports/`, added
in Phase 9 — so a deleted account would have left both behind: invisible,
because nothing else ever lists those prefixes. Both are now in the list, and
`tests/test_tryon.py` asserts the saga's source covers every prefix
`presign_upload` can write, so adding a prefix and forgetting the saga fails a
test rather than shipping.

### The degrade is the feature

"Works or degrades to a board, NEVER errors" is an exit criterion, and every
reason a render cannot happen returns 200 with the board and a stated reason:
no consent, no provider, quota exhausted, render failed. Verified live:

    POST /outfits/{hash}/tryon
    -> HTTP 200  rendered=false
       reason: "no body photo consented; add one to enable try-on"
       board_url: present

A board is pixel-accurate to clothes the user owns; a render is a guess about
how they would look. When the guess is unavailable the accurate picture is the
better answer, not an error page — which is why the board is the default
visualisation and try-on the enhancement, not the other way round.

The single genuine 404 is an outfit that does not exist: we can neither render
nor board it, and a 200 there would hide a client bug behind a degrade message.

### A fake that was quietly wrong

`FakeObjectStore` had no `delete_all_versions` at all, so every body-photo
revocation returned **502** in tests. It also did not accept the new `prefix`
parameter — a fake that silently ignores one would let `prefix="body"` pass
every test while production filed body photos under `originals/`, dissolving
the separation consent and erasure both depend on. Both mirrored now.

### Exit criteria

- [x] **Body-photo deletion verified end to end** — revocation deletes all
      versions and leaves the account untouched (§C5's requirement); erasure
      covers the `body/` prefix, asserted against the saga's own source.
- [~] **Try-on works or degrades to a board, never errors** — the DEGRADE half
      is built and verified; "works" needs a provider, which needs the
      benchmark.
- [ ] **Per-render cost measured** — needs renders, which need the benchmark.

**The benchmark is the gate, and it needs photographs.** Not the owner's
wardrobe alone — ten bodies and sixteen garments, which is a materially larger
ask than everything else currently waiting on a camera.

---

## Phase 11 — learning: bandit, trends, and a gate (built 2026-09-18)

**512 tests**; `ruff`, `ruff format` and `mypy --strict` clean across 113
source files. Migrations `0015_trend_signal` and `0016_bandit_arm`, both
verified reversible.

### Before Phase 11: 25% of the scoring weight was dead

Phase 11 replaces `colour_harmony + formality_coherence` with a learned model
"gated by feedback-replay eval". That gate compares a challenger against the
deterministic scorer — so the champion was checked first, and the champion was
running at 75%.

| Sub-score | Weight | State before |
|---|---|---|
| `style_affinity` | 0.20 | **Returned 0.0 unconditionally**, including when handed a style vector. `score_outfit` was never called with one. |
| `trend_alignment` | 0.05 | Returned 0.0 — Phase 11's own stub. |

Phase 8 built the style vector (EWMA, replayable, `user_style_vector`) and the
feedback router wrote it on every reaction. **Nothing ever read it.** A
populated table, a tested pure function, a weight in config — and no path from
any of them to a ranking. The same recurring failure in this codebase under a
new name, and the test suite was holding it in place: a test asserted
`"Phase 8" in detail`, i.e. asserted the term was UNWIRED, and passed for three
phases while a fifth of the score did nothing.

Wiring it threaded the vector and per-garment embeddings through
`load_wardrobe` -> `suggest` -> `score_outfit` via ONE loader
(`load_style_vector`) shared by the nightly precompute and both live paths,
because three loaders is three chances for the precompute to serve a ranking
the request path would not reproduce.

Cosine is **clamped at zero**, not rescaled: `(cos+1)/2` hands every outfit
0.10 of free score for being merely orthogonal to the user's taste, and lets an
outfit they demonstrably dislike still outscore nothing.

### The feedback loop was open, and had been since Phase 6

`relay.py` carried this comment from Phase 6: *"Phase 6+ will add:
feedback.recorded -> invalidate_precompute."* It was never built.

So feedback moved the style vector and the bandit posterior IMMEDIATELY, while
suggestions came from a materialised precompute whose stored breakdown was
computed whenever the nightly job last ran. The user reacted, the system
learned, and nothing changed until tomorrow. For Phase 11 that is not a latency
nit — a bandit whose arms take effect nightly explores once a day regardless of
what it is told.

Built as an outbox event so the enqueue commits with the reaction it describes.
Measured end to end: `style_affinity` went from 0.0 ("3 events, needs 10") to
**0.505** ("cosine +0.505 from 13 events") **10 seconds** after one tap.

### Thompson-sampling bandit (85/15)

Arms are DRESS CODES — eight values, so 5k events is ~600 per arm. It learns a
per-kind correction to the scorer ("this user likes festive_ethnic more than the
weights predict") and only reorders what the scorer already produced; it cannot
invent an outfit the scorer rejected.

**Seeded from (user, date), and that is load-bearing.** A stochastic bandit
otherwise breaks the invariant that the precompute and the request path produce
identical rankings. Consequences, all wanted: pull-to-refresh does not
reshuffle, the two paths agree exactly, and exploration happens across days —
the honest cadence for something opened once a morning.

Seeds are HASHED, not concatenated: `random.Random` seeded with nearby integers
gives nearby first draws, so two users with adjacent ids would explore in
lockstep and the cohort would collect half the information it thinks it does.

Position 0 is never explored — the top of the list is the product's promise.
`dismissed` and `saved` move neither counter: the style vector can afford a
weak signal because it moves a direction, but a Beta counter is a claim about
probability.

### First-party trends, with a k-anonymity floor

"Licensed or first-party sources only, capped at <=10% of score". There is no
licensed feed, so the signal is our own wear logs: values worn above their own
recent baseline. Weight 0.05, inside the cap.

**`MIN_COHORT_USERS = 5` is a privacy control, not a quality threshold.** A
trend aggregated from two tenants is a report of what those two wore this
fortnight; at n=1 it is the owner's wardrobe handed back as a trend.
`users_contributing` is stored so the floor is auditable rather than trusted.

Two bugs found while building it, both worth recording:

1. **The aggregation read zero rows and reported success.** The first version
   used `system_session()`, whose own docstring says tenant-scoped tables
   return zero rows there. `{'published': 0, 'suppressed': 0}` is also what a
   correctly-working job with no trends returns. Seventh instance of the
   pattern in this codebase, written minutes after the module docstring warning
   about exactly this. Fixed with a `SECURITY DEFINER` function per 0006's
   precedent, and the job now reports `examined`, so "no trends" and "no data"
   can never look identical again.
2. **Every score saturated at 1.0.** `(velocity-1)/(SATURATION-1)` against a
   constant of 2.0 published 156 of 175 rows at exactly 1.0 — a constant
   contributes nothing to a ranking, so the term was dead again in a new way.
   The constant was the real fault: "which absolute velocity is high" cannot be
   answered without the data, so it was a magic number wearing a PROVISIONAL
   label. Scores are now a value's position among the other risers of the SAME
   FIELD — self-calibrating, no threshold, always discriminating, with the
   absolute gate still excluding anything flat or falling.

`unknown` is excluded: it is what a field holds when tagging could not tell, so
a rising `unknown` is a rising tagging gap, and scoring it would reward the
system for garments it failed to identify.

### The learned compatibility model is NOT built, and the gate is

It cannot be. The gate is >=5k feedback events, there are 3 real ones, and
Polyvore pretraining needs a GPU and a dataset licence this project does not
have. Building it anyway produces precisely what the plan warns about —
something that memorises one person's recent choices and reports it as taste,
evaluated by a replay eval fitted on the same thin data.

What IS built is the part that has to exist first, and is testable today:

- **`eval/replay_feedback.py`** — pairwise accuracy over (preferred, rejected)
  pairs from real history. PAIRS, not events, is the denominator: a user who
  only taps "like" produces zero pairs however many events they generate, and
  reporting 100% there certifies a scorer nobody tested. Scored WITHOUT the
  style vector, because the vector is derived from those very reactions and
  including it would let the scorer see its own label.
- **`stylist_domain.promotion.may_promote`** — refuses for every reason it
  should and says which one: under the 5k gate, no ordered pairs, replayed on
  different histories, near-chance accuracy, or a margin under 2 points
  ("shown indistinguishable, not better").

Run against the live database: 13 events, 27 ordered pairs, **0.519** pairwise
accuracy — barely above chance, which is the honest answer for near-random test
feedback. The eval does not invent skill.

### What Phase 11 still needs

Real feedback from real users. The bandit and the trends are live and will
improve with use; the learned model waits on 5,000 events, and at n=500 that is
3-4 days of real traffic rather than the couple of years n=1 implied.

