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
                                          P5 MVP SHIP -- 20 users
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

Goal: 20 real users cataloguing real wardrobes. No recommendations yet.

- **Dedupe**: perceptual hash + embedding cosine > 0.95 within tenant → `DUPLICATE_SUSPECT`, ask the user. Never auto-merge.
- **Wear log + laundry state**: `worn_on`, `needs_wash`, cost-per-wear.
- **Search + filters**: text (BM25 over `search_text`) + slot/colour/dress_code/climate filters + "find similar" via vector.
- **Observability, minimum viable**: OTel traces with a span per stage; Langfuse wired; 4 dashboards; and exactly **four alerts** — DLQ age > 1h, daily spend > 2× trailing mean, ingest success rate < 99%, API 5xx rate. Resist adding more; unactionable alerts train people to ignore pages.
- **Onboarding**: "start with your 20 most-worn" flow. Every comparison review says this is what separates users who stick from users who abandon.

**PHASE 5 EXIT CRITERIA**
- [ ] 20 users, ≥2,000 garments ingested
- [ ] p95 ingest-to-`CLASSIFIED` < 60s under real load
- [ ] Correction rate per field measured on real data (compare to golden set — a big gap means your golden set isn't representative)
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
- [ ] Internal blind eval: you and 2 others rate 50 outfits; ≥60% "would wear." Below that, fix the scorer — the LLM will not save a bad candidate set.

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
- [ ] 200 users on V1
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

**PHASE 9 EXIT CRITERIA**
- [ ] Restore drill completed, time recorded, under stated RTO
- [ ] Erasure verified absent across all 5 systems
- [ ] Every paging alert has a runbook
- [ ] Game day run; findings ticketed
- [ ] Error budget policy signed off
- [ ] All `# PROVISIONAL` markers resolved or re-dated

---

# PHASE 10 — Try-on (week 13)

**Benchmark before you build.** Fork `Ionio-io/VTON-Pipeline`, run your *own* person × garment matrix — 10 bodies × 16 garments including sarees, kurtas and a sherwani. The published benchmark used Western garments; your routing table must come from your own grid.

Then: consent flow (separate record, timestamped, independently revocable) → `w-render` scale-to-zero with per-tenant concurrency 1 → router by category and pose with primary→secondary fallback → the 4-variant prompt ladder with the editorial framing second → quotas at the gateway.

**EXIT:** try-on works or degrades to a board, never errors. Per-render cost measured. Body-photo deletion verified end to end.

---

# PHASE 11 — Learning and trends (week 14+)

Only now, with ≥5k feedback events: Thompson-sampling bandit (85/15 exploit/explore), then a learned compatibility model (OutfitTransformer or MCN fine-tuned on Polyvore then on your feedback) replacing `colour_harmony + formality_coherence`, gated by feedback-replay eval. Trends last, on licensed or first-party sources only, capped at ≤10% of score.

---

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

_Last updated 2026-09-09._

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

### Next: Phase 3 — CV pipeline and the accuracy verdict

Segmentation (multi-garment split), embeddings in pgvector, and **a measured
accuracy number on the golden set** — the phase that decides whether the product
is viable. `services/stylist_ml` already exists with `/matte` and a model
registry endpoint; Phase 3 adds `/segment` and `/embed` alongside them.

Two things to handle when it lands:

1. **Inserting `SEGMENTED` between `MODERATED` and `MATTED` changes what
   `STATE_ORDER` means for jobs already parked at `MATTED`** — they would skip
   segmentation entirely. Drain the queue first, or backfill in-flight jobs.
   This is the one place the state machine is not self-migrating, and it is
   flagged in the code.
2. **Phase 0.2 is the blocker, not the code.** The golden set has a spec and no
   images. `run_eval.py` cannot produce the ethnic-wear verdict until those 500
   labelled images exist, and that verdict is what decides between accepting
   manual crops for drapes, adding a drape detector, or fine-tuning SegFormer.
