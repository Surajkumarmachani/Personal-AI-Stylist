# Personal AI Stylist — Production Architecture Specification

**Version** 1.0 · September 2026 · Owner: AI/ML Engineering
**Status** Design review candidate

> **Note on this file.** The diagrams below were redrawn in plain ASCII when
> this document was committed to the repo; the original used Unicode
> box-drawing characters that did not survive transfer. All labels, numbers
> and structural relationships are preserved exactly.

---

## 0. What was wrong with the previous diagram

The prior diagram was a **logical component view**. It was correct as far as it went and it is reproduced, corrected, as View 3 below. What made it not a production design:

| Missing | Why it changes the design, not just the docs |
|---|---|
| Deployment topology | Worker counts, node pools and GPU scheduling determine whether your pipeline stages can be separate services at all |
| Latency budget | "sync <2s" is an aspiration until you allocate milliseconds per stage; the allocation is what forces nightly precompute |
| Capacity model | Queue depth and worker concurrency determine whether ingest is 2 pods or 40, and whether GPU is serverless or reserved |
| Trust boundaries | Which pixels cross which network boundary determines your DPA list, your KMS design and your privacy policy |
| Consistency contract | "write row + enqueue job" is not atomic. Without an outbox you silently drop ingests under crash |
| Deletion pipeline | Erasure is a distributed saga across Postgres, S3, CDN, vector index and provider caches. It is a first-class subsystem |
| DR (RPO/RTO) | Backup strategy determines whether originals live in one bucket or two, in one region or two |
| ML lifecycle | Model version pinning + backfill path determines that `extractor_version` must be on every row and that re-index must be a job, not a script |
| SLOs + error budget | Without these there is no objective definition of "working", so there is no basis for degrade decisions |
| Environments & release | Migration policy (backward-compatible for one release) constrains every schema change you make from day one |

A production design pack is **five views plus nine specs**. Everything below.

---

# PART A — VIEWS

## View 1 — System context and trust boundaries

```
+================= UNTRUSTED =========================================+
|  Mobile app (RN)      Web PWA (Next.js)      Admin console          |
+==============================+======================================+
                               |
                               | TLS 1.3 · JWT (15m access / 30d refresh)
                               v
+============ TRUST BOUNDARY 1 =======================================+
|  EDGE          WAF · L7 rate limit · bot detection · CDN            |
|                signed URLs only (<= 15 min), no public objects       |
+==============================+======================================+
                               |
                               v
+============ TRUST BOUNDARY 2 =======================================+
|  PRIVATE VPC — no public ingress except via edge                    |
|                                                                     |
|  +--- app subnet ----------+   +--- data subnet (no egress) ------+  |
|  | API · workers · ML svc  |   | Postgres · Redis · object store  |  |
|  +------------+------------+   +----------------------------------+  |
|               |                                                     |
|               | egress via NAT + allowlist only                     |
+===============+=====================================================+
                |
                v
+============ TRUST BOUNDARY 3 =======================================+
|  THIRD PARTIES — each requires a signed DPA before first call       |
|                                                                     |
|  PIXELS LEAVE   -> VLM provider (garment cutouts — no faces)        |
|                 -> VTON providers (body photo + garment) — highest  |
|                    risk; zero-retention endpoint mandatory          |
|                                                                     |
|  NO PIXELS       · Reranker LLM (structured JSON only)              |
|                  · Weather (Open-Meteo — coarse lat/lon, 2dp)       |
|                  · Calendar (OAuth, event titles only, never bodies)|
|                  · Trend feeds (licensed, outbound only)            |
|                  · Push (ntfy/APNs/FCM), SMTP, Stripe               |
+=====================================================================+

SELF-HOSTED IN VPC (no pixels leave for these):
  SegFormer-clothes · rembg/BiRefNet · Marqo-FashionSigLIP · compositor
```

**The design rule this view encodes:** segmentation, matting and embedding are self-hosted *specifically* so that the common path never sends a user's photo off your infrastructure. Only two stages export pixels, both are optional, and the higher-risk one (body photo → VTON) is behind explicit per-user consent. Round weather coordinates to 2dp — full precision plus a timestamp is a home address.

---

## View 2 — Deployment topology

```
                        +--------------+
                        |  Cloudflare  |  WAF · CDN · signed URLs
                        +------+-------+
                               |
                        +------v-------+
                        |   Ingress    |  ALB / nginx-ingress, TLS term
                        +------+-------+
                               |
+==============================v======================================+
|  KUBERNETES (EKS/GKE)                                               |
|                                                                     |
|  ns: edge          +----------------------------------------------+  |
|                    | api          3-20 pods  HPA on RPS+p95       |  |
|                    | 500m/1Gi · PDB minAvailable 2                |  |
|                    +----------------------------------------------+  |
|                                                                     |
|  ns: workers   -- separate Deployment + HPA per queue --            |
|                    +----------------------------------------------+  |
|                    | w-ingest   2-40  HPA on queue depth          |  |
|                    |            2 CPU / 4Gi (CPU seg+matte)       |  |
|                    | w-render   0-10  HPA on queue depth          |  |
|                    |            scale-to-zero, GPU or ext API     |  |
|                    | w-suggest  2-8   nightly precompute + adhoc   |  |
|                    | w-cron     1     LEADER-ELECTED (see §C4)    |  |
|                    | w-notify   1-4                               |  |
|                    | w-delete   1     erasure saga, low prio      |  |
|                    +----------------------------------------------+  |
|                                                                     |
|  ns: ml            +----------------------------------------------+  |
|                    | ml-inference  2-12  ONNX Runtime, FastAPI    |  |
|                    |   SegFormer-B2 · BiRefNet · FashionSigLIP    |  |
|                    |   node pool: CPU-optimised (c6i) default;    |  |
|                    |   g5.xlarge pool w/ taint if p95 > budget    |  |
|                    |   model files from PVC, NOT baked in image   |  |
|                    +----------------------------------------------+  |
|                                                                     |
|  ns: platform      +----------------------------------------------+  |
|                    | litellm       2 pods (stateless gateway)     |  |
|                    | langfuse      1 pod  (LLM tracing)           |  |
|                    | otel-collector DaemonSet                     |  |
|                    +----------------------------------------------+  |
|                                                                     |
|  NetworkPolicy: default-deny. ml + workers may reach Postgres/Redis |
|  and litellm. ONLY litellm may egress to the internet via NAT.      |
+=====================================================================+
        |                     |                       |
+-------v--------+  +---------v--------+  +-----------v---------+
| Postgres 16    |  | Redis / Valkey   |  | S3 / R2             |
| primary + 1 RR |  | AOF, 2 replicas  |  | originals (IA@90d)  |
| pgvector+HNSW  |  | queues + cache   |  | cutouts (hot)       |
| PITR 7d, RLS   |  | SEPARATE dbs for |  | renders (hot, 30d)  |
| managed        |  | queue vs cache   |  | versioning ON       |
+----------------+  +------------------+  +---------------------+
```

**Three non-obvious choices worth defending in review:**

1. **`ml-inference` is a separate service, not a library in the worker.** Segmentation models scale on GPU/CPU-seconds; workers scale on I/O concurrency. Coupling them means you buy GPU to wait on S3. This is the split Immich uses for the same reason.
2. **Model weights come from a PVC, not the container image.** A 600MB CLIP checkpoint in your image makes every deploy slow and every model swap a rebuild. Weights are data.
3. **Separate Redis logical databases for queue and cache.** A cache eviction policy (`allkeys-lru`) will silently eat your job queue. This has taken down production systems.

---

## View 3 — Logical components (corrected)

```
CLIENT --1. POST /uploads/presign--> API --> returns presigned PUT + upload_id
       --2. PUT bytes-------------------------> OBJECT STORE --> CDN
       --3. POST /garments/ingest {upload_ids, Idempotency-Key}--> API
       --4. SSE /jobs/{id}/events (progress, partial results)

+---------------------------------------------------------------------+
|  API GATEWAY  JWT · tenant resolve · Idempotency-Key (Redis SETNX)   |
|               per-tenant quota · RPS limit · OpenAPI spec is truth   |
+-----+------------------+-------------------+------------------------+
      | WARDROBE         | STYLIST           | PROFILE/BILLING/ADMIN
      +--------+---------+---------+---------+---------+
               |                   |                   |
+--------------v-------------------v-------------------v-------------+
|  POSTGRES 16 + pgvector  -- RLS ENFORCED, app.tenant_id per txn    |
|  garments · outfits · outfit_feedback · wear_log · trend_signals   |
|  jobs · outbox · model_calls · audit_log · erasure_requests        |
+---^-------------------------------------------------------^--------+
    |                                                       |
    |   +------- TRANSACTIONAL OUTBOX (§C1) -------------+   |
    |   | single txn: INSERT garment + INSERT outbox     |   |
    |   | relay polls outbox -> Redis stream -> mark sent|   |
    |   +----------------------+-------------------------+   |
    |                          |                             |
+---+--------------------------v------+  +-------------------+---------+
| INGEST PIPELINE (async, per photo)  |  | SUGGEST PIPELINE (sync)     |
| state machine, §A5                  |  | latency budget, §A4         |
+-------------------------------------+  +-----------------------------+
| 1 validate  magic bytes, <=12MB,    |  | 1 context resolve (cached)  |
|             decode-bomb guard       |  |     weather · calendar ·    |
| 2 sanitise  EXIF strip (GPS!)       |  |     trend_signals           |
| 3 moderate  NSFW/non-garment gate   |  | 2 candidate READ from       |
|             -- reject before any    |  |     precomputed outfits     |
|                third-party call     |  | 3 deterministic score       |
| 4 segment   SegFormer -> N masks    |  | 4 RERANKER LLM (strict JSON)|
| 5 matte     BiRefNet -> cutouts     |  | 5 VALIDATOR (§C3)           |
| 6 classify  local: cat+colour       |  | 6 compose board (~80ms)     |
| 7 vlm tag   batch 6:1, JSON schema  |  | -- every stage has a        |
| 8 embed     FashionSigLIP 768d      |  |    fallback; none can 5xx   |
| 9 dedupe    phash + cosine >0.95    |  +--------+--------------------+
|10 persist   txn + outbox            |           |
|11 invalidate precompute + cache     |  +--------v---------+
+------------+------------------------+  | VIZ ROUTER       |
             |                           | 1 composite  $0  |
             |         +-----------------| 2 VTON (async)   |
             |         |                 | 3 generative(last|
             |         |                 +--------+---------+
+------------v---------v--------------------------v-----------------+
|  LiteLLM GATEWAY (the ONLY egress path to model providers)        |
|  virtual key per tenant · hard budget + daily/monthly reset ·     |
|  spend log -> model_calls · fallback routing · response cache ·   |
|  model list in Postgres (hot-swappable) · Langfuse callback       |
|  zero-cost models exempt from budget -> degrade path stays open   |
+---+---------+----------+---------------+------------------+-------+
    v         v          v               v                  v
  VLM     Reranker  Embeddings*    VTON providers       Image gen
                    (*self-hosted, (FASHN · Kling ·     (last resort)
                     routed for     CatVTON · NanoBanana)
                     uniform obs)

+--- FEEDBACK LOOP ---------------------------------------------------+
| like/dislike/worn/dismiss -> outfit_feedback (append-only, source   |
|   of truth) -> w-suggest: style_vector EWMA + preference facts +    |
|   Thompson-sampling bandit -> invalidate precompute -> re-nightly   |
| ALL derived state is recomputable from the event log. No exceptions.|
+---------------------------------------------------------------------+

+--- ERASURE SAGA (§C5) ----------------------------------------------+
| DELETE /me -> erasure_requests -> w-delete: Postgres rows -> S3     |
| objects (all versions) -> CDN purge -> vector rows -> provider cache|
| revoke -> Langfuse trace scrub -> audit entry -> confirm <= 30d     |
+---------------------------------------------------------------------+
```

---

## View 4 — Critical path with latency budget

`GET /suggestions?date=tomorrow` — **SLO p95 ≤ 1500ms, p99 ≤ 3000ms**

```
  ms  cumulative  stage                                 fallback if over
----  ----------  ----------------------------------    -----------------
   8       8      TLS + edge + WAF                      —
  12      20      JWT verify + tenant resolve (Redis)   —
  15      35      quota check                           fail open, log
  40      75      context: weather (cached 1h) +        stale-while-
                  calendar (cached 15m) + trends (24h)  revalidate
  60     135      candidate READ (precomputed, <=200)   on miss -> live
                                                        generate (+400ms)
  55     190      deterministic score + top-8 select    —
 900   1,090      RERANKER LLM (p95, ~1.2k in/300 out)  1200ms HARD
                  -- 70% served from rationale cache    TIMEOUT -> return
                     at ~15ms                           deterministic
                                                        order + template
  80   1,170      VALIDATOR + board compose (cache hit  regenerate async,
                  serves at ~10ms)                      serve placeholder
  40   1,210      serialise + CDN headers               —
----  ----------
       1,210      p95 with 290ms headroom
```

**What this budget forces, and this is the point of writing it down:**

- The LLM is 74% of the budget. It gets a **hard timeout below the SLO**, not a retry. Retrying inside a request is how you turn a p95 miss into a p99 catastrophe. Retry belongs in the async plane.
- Candidate generation cannot be in the request. At 200 candidates × real scoring you are at 400–800ms before the LLM even starts. Hence nightly precompute — that arrow in the diagram is a consequence of this table, not a nice-to-have.
- Rationale cache hit rate is load-bearing. At 0% hit rate you miss the SLO. Measure it as an SLI, alert if it drops below 50% (which means your cache key is too specific — usually someone added raw temperature instead of a temperature bucket).

---

## View 5 — Ingest state machine

Every ingest job is a durable state machine, not a function call. States persist to `jobs`.

```
                    +----------+
                    | RECEIVED |
                    +----+-----+
                         |
                    +----v------+   invalid    +----------+
                    | VALIDATED |------------->| REJECTED |  terminal,
                    +----+------+              +----------+  user-visible
                         |                                   reason
                    +----v------+   flagged    +-----------+
                    | MODERATED |------------->| QUARANT.  |  terminal,
                    +----+------+              +-----------+  audit, NO
                         |                                    third-party
                    +----v------+                             call
                    | SEGMENTED |  0 masks --> NEEDS_REVIEW (manual crop UI)
                    +----+------+
                         |
                    +----v------+
                    |  MATTED   |
                    +----+------+
                         |
                    +----v-------+   <-- PARTIALLY USEFUL FROM HERE:
                    | CLASSIFIED |       row is written, item appears in
                    +----+-------+       wardrobe with coarse tags
                         |
              +----------v------+  VLM down / budget hit
              |  TAGGED (vlm)   |-----------------> DEGRADED_TAGGED
              +----------+------+                   (retryable later by
                         |                           backfill job)
                    +----v-----+
                    | EMBEDDED |
                    +----+-----+
                         |
              +----------v------+  duplicate found
              |    DEDUPED      |-----------------> DUPLICATE_SUSPECT
              +----------+------+                   (ask user, don't guess)
                         |
                    +----v-----+
                    | COMPLETE |  --> emit outbox: invalidate_precompute
                    +----------+

RETRY: exponential backoff + full jitter, base 2s, max 3 attempts,
       per-stage (not per-job) — never re-segment because the VLM failed.
FAILURE: -> DLQ with the last successful state. Alert if DLQ depth > 10
       or DLQ age > 1h. Replayable from the recorded state.
```

**Two properties that make this production rather than a script:**

- **Partial usefulness.** The garment becomes visible in the wardrobe at `CLASSIFIED`, not `COMPLETE`. The user sees their shirt in 3 seconds with coarse tags, and material/formality fill in a few seconds later. Waiting for the whole chain to show anything is the single most common ingest UX mistake.
- **Moderation gates before any third-party call.** A rejected upload never leaves your VPC. This is both a policy requirement and a cost control.

---

# PART B — NON-FUNCTIONAL SPECS

## B1. SLOs and error budget

| SLI | Definition | SLO | Budget @ 30d |
|---|---|---|---|
| Suggestion availability | non-5xx on `/suggestions` | 99.5% | 3h 36m |
| Suggestion latency | p95 `/suggestions` | ≤ 1500ms | 5% of reqs |
| Ingest success | reach ≥ `CLASSIFIED` within 60s | 99.0% | 1% |
| Ingest completeness | reach `COMPLETE` within 10m | 97.0% | 3% |
| Tag accuracy | category + primary colour on golden set | ≥ 92% | CI gate |
| Try-on success | render or explicit graceful decline | 98.0% | 2% |
| Data durability | garment rows + originals | 99.999999% | — |

**Error budget policy.** Budget >50% consumed → feature freeze, reliability work only. Budget exhausted → no non-critical deploys until the window rolls. This is the mechanism that stops "production" from decaying; without it the SLO table is decoration.

**Deliberately *not* an SLO:** recommendation *quality*. It's a product metric (7-day like-rate, wear-through rate, wardrobe coverage) tracked on a dashboard with alerts, but it must not gate deploys — you'd never ship an experiment.

## B2. Capacity model

Baseline: 50k MAU, 10k DAU, 150 garments/user, 1.2 suggestion requests/DAU/day, 5% try-on.

```
SUGGEST  10k DAU × 1.2 = 12k req/day; peak factor 8× in the 07:00-09:00
         local window -> ~2.7 RPS peak. At 900ms p95 and 40 concurrent
         slots/pod -> 3 pods handles peak with 2x headroom. HPA 3-20.
         (Peak factor is the whole story here: this app is a morning app.
          Provision for the commute, not the mean.)

INGEST   Onboarding is the burst: a new user uploads 30-150 photos in one
         sitting. 200 signups/day × 60 photos = 12k photos/day, but
         arriving in bursts of 60.
         Per photo CPU: segment 400ms + matte 300ms + classify 15ms
                        + embed 60ms = 0.8s CPU
         12k × 0.8s = 2.7 CPU-hours/day. Trivial in aggregate.
         BUT: a 60-photo burst must clear in <3 min for the UX to hold
         -> 60 × 0.8s = 48 CPU-s -> 4 concurrent workers = 12s. Fine.
         50 simultaneous onboarders -> 40 workers. Hence HPA 2-40 on
         QUEUE DEPTH, not CPU. CPU-based HPA is always too slow for
         bursty queues; you scale up after the burst has drained.

VLM      12k photos / 6 (batch) = 2k calls/day = ~0.02 RPS. Never the
         bottleneck. Batching is purely a cost lever here.

RENDER   10k DAU × 5% = 500 try-ons/day, bursty. External API at ~20s
         each -> 0-10 pods scale-to-zero, queue-backed, never synchronous.
         Per-tenant concurrency cap of 1 (see bulkheads, §C2).

POSTGRES 50k users × 150 = 7.5M garment rows. HNSW index on 768d ->
         7.5M × 768 × 4B = ~23GB of vectors. This is the number that
         decides your instance class: needs ~32GB RAM to keep the index
         warm. db.r6g.2xlarge. Note vectors are queried WITHIN a tenant
         (<=400 rows), so recall is trivially high — the index exists for
         cross-tenant style-similarity features, not wardrobe search.

REDIS    queue depth peak ~5k jobs × ~2KB = 10MB. Cache: rationales +
         boards + context = ~2GB. cache.m7g.large, AOF on for queue db.
```

**Scale trigger table** — what breaks first, and at what number:

| At | Symptom | Action |
|---|---|---|
| 500k users / 75M vectors | HNSW index exceeds RAM, p99 vector latency spikes | Partition garments by tenant hash; or drop the global index and keep per-tenant brute force |
| 2k RPS suggest | Connection pool exhaustion | PgBouncer transaction pooling; read replica for candidate reads |
| >100 render RPS | External VTON rate limits | Self-host CatVTON on reserved GPU for the majority case |
| Multi-region users | Latency + data residency | Regional cells, tenant pinned to a home region |

## B3. Cost governance

Enforce at the gateway, not in feature code. LiteLLM virtual key per tenant.

| Control | Value | Enforced where |
|---|---|---|
| Free-tier monthly LLM+VLM budget | $0.15/user | LiteLLM hard budget, monthly reset |
| Free-tier try-on renders | 3/month, 1/day | API quota + LiteLLM key budget |
| Paid-tier renders | 50/month, 5/day | same |
| Global daily spend circuit breaker | 2× 7-day trailing mean | cron watchdog → disables render queue, pages on-call |
| Zero-cost model exemption | self-hosted models `input_cost_per_token: 0` | LiteLLM skips budget checks → **degrade path survives budget exhaustion** |

That last row is the design trick: a user who exhausts their budget still gets full cataloguing and deterministic outfit boards, because those run on models the gateway prices at zero. Budget exhaustion becomes a feature downgrade, not an outage.

---

# PART C — CORRECTNESS AND FAILURE

## C1. Consistency: the transactional outbox

The bug in the previous diagram: `INSERT garment` then `redis.enqueue(job)` are two operations. Crash between them and the garment exists with no processing, forever, silently. Or enqueue-then-insert, and the worker reads a row that isn't there.

```sql
BEGIN;
  INSERT INTO garments (...) RETURNING id;
  INSERT INTO outbox (aggregate_id, event_type, payload, created_at)
    VALUES (:garment_id, 'garment.ingested', :payload, now());
COMMIT;
-- separate relay process:
--   SELECT * FROM outbox WHERE sent_at IS NULL
--     ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT 100;
--   publish to Redis stream; UPDATE outbox SET sent_at = now();
```

Guarantees: **at-least-once delivery** with the DB as source of truth. Combined with idempotent consumers (§C3) this gives effective exactly-once. `FOR UPDATE SKIP LOCKED` lets you run N relay replicas with no coordination.

Apply the same pattern to every state change that must trigger work: feedback → invalidate precompute, garment delete → erasure saga, subscription change → quota update.

## C2. Bulkheads — blast radius containment

| Bulkhead | Limit | Prevents |
|---|---|---|
| Queue per workload | separate Deployment + HPA per queue | An onboarding burst starving daily suggestions |
| Per-tenant concurrency | ingest 4, render 1 | One user with 2,000 photos monopolising the pool |
| Per-provider circuit breaker | 5 failures / 30s → open 60s, half-open probe | A degraded VTON provider consuming all render workers on timeouts |
| Redis logical separation | db0 queue (AOF, noeviction), db1 cache (LRU) | Cache eviction silently deleting jobs |
| Connection pool per service | api 20, workers 10, ml 5 | Worker burst exhausting Postgres connections and taking down the API |
| Global spend breaker | 2× trailing mean | A retry storm producing a five-figure invoice |

## C3. Validator contract

The validator is the reason you can put a stochastic model on a user-facing path. It is not a lint step, it is a **hard gate with a deterministic fallback**. Assertions, in order:

1. Response parses against the JSON schema. Invalid → one repair retry with the schema echoed → still invalid → deterministic ranking + template rationale.
2. **Every garment ID in the output ⊆ the set of IDs sent in the input.** Not a subset check on names — an exact ID set check. This is the assertion that makes "invented clothing" structurally impossible rather than prompt-dependent.
3. Every referenced garment is still `is_active` and owned by the requesting tenant (guards against a stale precompute after a delete).
4. Slot legality: exactly one of `{top+bottom}` or `{one_piece}`, ≤1 outerwear, exactly 1 footwear, 0–3 accessories.
5. Rationale ≤ 40 words, contains no URL, no price, no medical/body-shaming term (regex + small classifier).
6. Confidence ≥ threshold, else demote below deterministically-ranked results.

**Every validator rejection is a logged metric**, tagged by rule. `validator.reject{rule="unknown_id"}` trending up means your prompt or model regressed — this is your earliest and cheapest quality signal, and it fires before users complain.

Idempotency for consumers: every job carries `idempotency_key`; workers `INSERT ... ON CONFLICT (idempotency_key) DO NOTHING` into a processed-keys table inside the same transaction as their effect. Replay is then free.

## C4. Distributed cron

`w-cron` runs at replica 1 but must survive its own death without double-running the nightly precompute (which would double your VLM bill and corrupt candidate state).

```
Postgres advisory lock, per job name:
  SELECT pg_try_advisory_lock(hashtext('nightly_precompute'));
  -- acquired -> register and run; not acquired -> skip silently
  -- lock auto-releases on connection loss, so a crashed leader
  -- hands off automatically on the next tick
```

Same pattern as Immich's `DatabaseLock`. Do not use a Redis lock for this — Redis lock expiry semantics under partition are exactly where double-execution lives.

## C5. Erasure saga (DPDP 2023 / GDPR Art. 17)

Deletion is a distributed transaction across seven systems and cannot be a `DELETE CASCADE`. It is a compensating saga with a durable record.

```
DELETE /me -> erasure_requests(id, user_id, requested_at, state)
              state machine, resumable, <= 30 day SLA:

  1 SOFT_DELETED     users.deleted_at set; all auth revoked;
                     API returns 410 immediately (user-visible done)
  2 PROVIDER_PURGED  revoke LiteLLM virtual key; request cache purge
                     from each provider with a retention window;
                     record what could NOT be purged (and disclose it)
  3 TRACES_SCRUBBED  Langfuse/OTel: delete or pseudonymise spans
                     containing user content
  4 OBJECTS_DELETED  S3: delete ALL VERSIONS (versioning is on — a
                     plain delete leaves a recoverable version, which
                     is not erasure) + abort multipart uploads
  5 CDN_PURGED       purge by cache tag, verify with a probe request
  6 ROWS_DELETED     garments, outfits, feedback, wear_log, vectors,
                     jobs, outbox — in FK order, batched
  7 CONFIRMED        immutable audit_log entry (retained: legally
                     required, contains no personal data beyond the
                     pseudonymous user id + timestamps)

Any step fails -> retry with backoff, alert at 7 days, page at 25 days.
Body photos have a SEPARATE, immediate deletion endpoint that runs
steps 2, 4, 5 only — users must be able to revoke that consent without
deleting their account.
```

Also required and commonly forgotten: **export** (`GET /me/export` → async job → signed ZIP with CSV + all images, 7-day link). It's a portability obligation under both regimes and it's the feature competitors advertise because so few ship it.

## C6. Failure matrix

| Failure | Detection | Automatic response | User sees |
|---|---|---|---|
| VLM provider down | breaker opens | ingest completes at `DEGRADED_TAGGED`; backfill job retries hourly | Item appears with coarse tags + "refining…" |
| Reranker LLM down/slow | 1200ms hard timeout | deterministic ranking + template rationale | Suggestions, slightly blander copy |
| All VTON providers down | breaker opens | render queue paused, jobs held not failed | "Try-on is busy, we'll notify you" |
| Postgres primary failover | managed HA, ~60s | API 503s behind retry-after; workers backoff; queue absorbs | Brief retry |
| Redis queue loss | AOF + replica | outbox relay re-publishes unsent rows | Slower ingest, nothing lost |
| Object store degraded | 5xx rate | uploads rejected with retry-after; reads from CDN unaffected | Can browse, can't add |
| Tenant spend exhausted | gateway 429 | zero-cost path only | Full wardrobe + boards, no new AI tags/renders |
| Poison job | 3 attempts → DLQ | DLQ alert, job quarantined | Single item flagged for review |
| Model quality regression | golden set in CI + `validator.reject` rate | CI blocks; in prod, rollback pinned model ID via gateway config (no redeploy) | Nothing |

**The invariant across the whole table: no AI dependency failure produces a 5xx.** Every branch degrades to something a user can use. That is the difference between "we use AI" and "we ship AI."

## C7. Disaster recovery

| Asset | RPO | RTO | Mechanism | Verified by |
|---|---|---|---|---|
| Postgres | 5 min | 1 h | Managed HA + PITR 7d + nightly logical dump to a **second cloud account** | Quarterly restore drill to a scratch env, timed |
| Object store | 15 min | 4 h | Versioning + cross-region replication for originals; cutouts/renders are **derived — regenerate, don't replicate** | Semi-annual regeneration drill |
| Redis | 1 h | 15 min | AOF + replica; queues rebuildable from outbox | Failover test |
| Secrets | — | 15 min | External secrets operator + KMS, versioned | Rotation drill |
| Full region loss | 1 h | 8 h | IaC re-apply + DB restore in secondary region | Annual game day |

**An untested backup is not a backup.** The restore drill is a scheduled calendar item with a named owner and a recorded wall-clock time. If you can't state last quarter's restore time, your RTO is fiction.

---

# PART D — LIFECYCLE

## D1. ML model lifecycle

```
REGISTRY   Every model has: name, version, task, checksum, eval scores,
           approval status. Stored in Postgres (system_config), served
           to workers per-job — never read from env vars, never baked
           into images. Swapping a model is an UPDATE, not a deploy.

PROMOTION  candidate -> shadow (5% mirrored traffic, results logged not
           served) -> canary (5% served, guarded by validator-reject and
           latency SLIs) -> 25% -> 100%. Auto-rollback on SLI breach.

EVAL GATE  Golden set (500 hand-labelled garments incl. hard cases: dark-
           on-dark, pattern-on-pattern, folded, on-hanger, low light,
           multi-garment frame). Required CI check. No regression on
           per-field accuracy, ever.
           Ranker: Polyvore FITB accuracy + CP-AUC offline, plus REPLAY
           of held-out user feedback — would this ranker have surfaced
           the outfit they actually wore?

BACKFILL   Extractor change -> every garment carries extractor_version.
           A version bump enqueues a re-extraction backfill (low
           priority, budget-capped, resumable). This is why the raw VLM
           output is stored in attributes_raw: reprocessing must never
           require re-calling the provider.
           user_verified_fields are NEVER overwritten by a backfill.

DRIFT      Weekly: distribution of predicted attributes vs trailing
           90 days; per-field user-correction rate. Correction rate is
           your live accuracy metric — it is labelled data arriving free.
           Alert on a >20% relative increase.
```

## D2. Environments, CI/CD, release

```
local   - docker compose, seeded fixtures, LiteLLM pointed at Ollama ($0)
dev     - shared cluster, real providers, synthetic tenants, budget $50/mo
staging - production-identical topology, anonymised data subset,
          load test target (2× projected peak), migration rehearsal
prod    - blue/green for API; rolling for workers; canary for models

PIPELINE  lint (ruff, mypy --strict, tsc --noEmit)
       -> unit + integration (testcontainers: real Postgres + Redis)
       -> RLS CROSS-TENANT LEAK TEST  <-- required, non-negotiable
       -> golden-set eval gate
       -> contract test (OpenAPI spec vs generated clients)
       -> container scan + SBOM
       -> migration dry-run against a staging snapshot
       -> deploy staging -> smoke -> canary prod 5% -> 100%
       -> auto-rollback on error-rate or p95 SLI breach

MIGRATION POLICY (this constrains every schema change from day one)
  Expand -> migrate -> contract, across three releases:
    R1  add nullable column / new table, dual-write
    R2  backfill, switch reads
    R3  drop the old column
  No blocking DDL on tables >1M rows. CREATE INDEX CONCURRENTLY only.
  Every migration must be safe to run while the previous release is
  still serving traffic — because during a rolling deploy, it is.

FLAGS   Every AI feature behind a flag with a per-tenant kill switch.
        try_on_enabled, trend_boost_enabled, llm_rerank_enabled.
        Killing llm_rerank_enabled must leave a working product.
```

## D3. Observability and operations

```
TRACES   OpenTelemetry, one trace per request and per job. Span per
         pipeline stage. Trace ID returned in every error response and
         surfaced in the UI so support can jump straight to it.
LLM      Langfuse: prompt, response, model, version, tokens, cost,
         cache status, validator outcome, linked to the OTel trace.
METRICS  RED per endpoint; queue depth + oldest-message age per queue;
         DLQ depth + age; per-provider error rate and p95; spend vs
         budget; cache hit rates; validator rejects by rule.
LOGS     Structured JSON, tenant_id + trace_id on every line.
         NEVER log image bytes, presigned URLs, or raw VLM output.

DASHBOARDS
  1 SLO burn-down (the one the on-call looks at first)
  2 Pipeline health: stage latencies, queue depths, DLQ
  3 Cost: spend by tenant/model/feature vs budget
  4 Quality: golden-set accuracy trend, correction rate by field,
    validator rejects, 7-day like-rate, wear-through, wardrobe coverage

PAGING (wake someone up)
  SLO fast burn (2% budget in 1h) · DLQ age >1h · spend >2× trailing
  mean · Postgres replication lag >30s · erasure request >25 days
TICKETING (business hours)
  golden-set accuracy drop >2pp · correction rate up >20% ·
  cache hit rate <50% · single provider breaker open >15m

RUNBOOKS - one per paging alert, each with: symptom, 3 likely causes,
  verification query, mitigation, rollback, escalation. A page without
  a runbook is an incident that lasts as long as the on-call's memory.
```

---

## Design review scorecard

Where this pack now stands. Use it as the checklist.

| Dimension | Before | Now | Evidence |
|---|---|---|---|
| Component decomposition | ✔ | ✔ | View 3 |
| Deployment topology | ✘ | ✔ | View 2 |
| Latency budget | ✘ | ✔ | View 4 |
| Capacity + scale triggers | ✘ | ✔ | §B2 |
| SLOs + error budget policy | ✘ | ✔ | §B1 |
| Consistency guarantees | ✘ | ✔ | §C1, C3 |
| Failure containment | ⚠ partial | ✔ | §C2, C6 |
| DR with tested RPO/RTO | ✘ | ✔ | §C7 |
| Security + trust boundaries | ⚠ mentioned | ✔ | View 1 |
| Privacy/regulatory as a subsystem | ✘ | ✔ | §C5 |
| ML lifecycle + eval gates | ✘ | ✔ | §D1 |
| Release + migration policy | ✘ | ✔ | §D2 |
| Observability + runbooks | ⚠ named | ✔ | §D3 |
| Cost governance | ⚠ estimated | ✔ | §B3 |

## The four things that make this production rather than a diagram

1. **The transactional outbox.** Without it you silently lose work under crash, and no amount of retry logic fixes it because the intent was never durably recorded.
2. **The validator's exact-ID-set assertion.** It converts "the LLM might hallucinate a garment" from a prompt-engineering hope into a structural impossibility. It is what licenses putting a stochastic model on a user-facing path.
3. **The degrade ladder with zero-cost model exemption.** Every AI dependency can fail and the product still works. Budget exhaustion becomes a downgrade, not an outage.
4. **The error budget policy.** It's the only mechanism that stops a production system from decaying back into a prototype, because it makes reliability work mandatory rather than virtuous.
