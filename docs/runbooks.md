# Runbooks

One per paging alert, per Phase 9. Each gives a symptom, three likely causes,
a verification query, mitigation, rollback and escalation.

**Every query here has been run against the live schema.** A runbook whose
query errors at 03:00 is worse than no runbook: it costs you the minutes you
had, and it teaches whoever hit it that the document is decoration.

## Before anything else

```bash
curl -s localhost:8080/ops/alerts -H "Authorization: Bearer $TOKEN" | jq
```

That returns all four alerts with `firing`, the value, the threshold and a
one-line response. Start there — it is faster than any query below and it tells
you whether the thing that paged you is still true.

**Two guards that change how you read a rate.** A rate over fewer than **20
settled samples never fires**, because 2 failures in 3 requests is 67% and
means nothing. And `/ops/*` requests are excluded from the 5xx counters, so a
monitoring loop cannot dilute the rate it measures.

A shell for the database, used throughout:

```bash
PSQL='docker compose -f infra/compose/docker-compose.yml --env-file .env exec -T postgres psql -U stylist_owner -d stylist'
```

`--env-file .env` is load-bearing. Without it compose reads `infra/compose/.env`
(which does not exist), every `${VAR}` interpolates to empty, and you will debug
a service that is not configured the way you think it is.

---

## 1. `dlq_age` — a job has been stuck for over an hour

**Symptom.** `ops_dlq_stats()` reports `oldest_seconds > 3600`. Work has stopped
for at least one user and they have no idea why.

**Likely causes, in the order they actually happen:**

1. **A dependency is down and jobs exhausted their retries.** The `ml` service
   or the LiteLLM gateway. `Unavailable` waits on a 180s wall-clock budget
   without consuming an attempt, so this means it was down for longer than that.
2. **A poison image.** A corrupt file fails identically on every attempt. These
   should reach `rejected` after ONE attempt rather than the DLQ — if one is in
   the DLQ, the classification of the error is wrong, not the retry count.
3. **Head-of-line blocking.** `Unavailable` sleeps IN-PROCESS, so at
   `WORKER_MAX_JOBS=2` two jobs waiting on down infrastructure stall the whole
   queue. This is a KNOWN, UNFIXED issue (cross-phase pass, 2026-09-10): the fix
   is to release the slot and re-enqueue with a delay. Symptom is jobs DLQ'ing
   in sequence ~240s apart rather than together.

**Verify:**

```sql
SELECT id, state, dlq_at, stage_attempts, left(last_error, 160) AS err
FROM jobs WHERE dlq_at IS NOT NULL ORDER BY dlq_at LIMIT 10;
```

`stage_attempts` names the stage that failed. That is the answer most of the
time — read it before anything else.

**Mitigate.**
- Dependency down: fix the dependency, then re-enqueue. Resume is designed for
  this — stages derive their inputs from object keys, so any worker can pick up
  any job with no handover.
- Poison image: the job is correctly terminal. Tell the user; do not retry.
- Head-of-line blocking: restart the worker to clear in-process sleeps.
  `docker compose ... restart worker`. This is a mitigation, not a fix.

**Rollback.** Nothing to roll back — the DLQ is a symptom. If a deploy caused
it, roll that back and re-enqueue.

**Escalate** if the DLQ is growing while dependencies are healthy. That means a
stage is failing on content rather than infrastructure, and the retry budget is
being spent on something retrying cannot fix.

---

## 2. `spend_spike` — today's model spend is over 2× the trailing mean

**Symptom.** `ops_spend_stats(7)` shows `today > 2 * trailing_mean`. Does not
fire with fewer than 2 days of history, so a new deployment cannot page you on
day one.

**Likely causes:**

1. **A retry storm.** A provider erroring intermittently while the gateway
   retries twice per call. Spend rises with no matching rise in garments.
2. **A backfill.** A bumped `EXTRACTOR_VERSION` re-tags every garment by
   design. Expected, and it should still be checked rather than assumed.
3. **The response cache stopped working.** A prompt or schema change makes
   every request unique. Watch for `cache_hit` false on calls that used to hit.

**Verify:**

```sql
SELECT model_name, count(*) AS calls, round(sum(cost_usd), 4) AS usd,
       sum((cache_hit)::int) AS cached
FROM model_calls WHERE created_at > now() - interval '24 hours'
GROUP BY 1 ORDER BY 3 DESC;
```

Then compare against the work actually done:

```sql
SELECT count(*) FROM garments WHERE created_at > now() - interval '24 hours';
```

**Calls far exceeding garments is the retry-storm signature.** Cost per garment
is ~$0.0023 at batch-of-1 and lower when batched 6 to a grid.

**Mitigate.** Per-tenant budgets are enforced by LiteLLM on the virtual key, so
a runaway tenant stops on its own and degrades to `DEGRADED_TAGGED` — the user
keeps a complete, searchable wardrobe. If it is systemic, set
`VLM_MODEL=vlm-tagger-mock` to stop spending entirely while you investigate;
cataloguing continues, only AI tags pause.

**Rollback.** If a prompt or schema change caused a cache miss storm, revert it.
The gateway's model list is `store_model_in_db: true`, so swapping a model is
an UPDATE rather than a deploy.

**Escalate** if spend is rising and `model_calls` shows nothing unusual. That
means the spend is not coming from this application.

---

## 3. `ingest_success` — success rate below 99%

**Symptom.** `ops_ingest_stats(24)` reports `ok / (ok + dead) < 0.99` over at
least 20 SETTLED jobs. In-flight jobs are excluded deliberately: counting them
makes the rate dip during every burst, which is the one moment you least want a
spurious page.

**Likely causes:**

1. **One stage failing systematically.** A model service degraded, a changed
   response shape, weights that failed to load.
2. **Bad input at scale.** A batch of unusual photos — screenshots, very dark
   images, non-garments. Segmentation routes these to `NEEDS_REVIEW`, which is
   correct behaviour and not a failure.
3. **Storage or database pressure.** Stages fetch from object storage on every
   resume; an S3 blip surfaces as scattered failures across unrelated stages.

**Verify:**

```sql
SELECT state, count(*) FROM jobs
WHERE created_at > now() - interval '24 hours' GROUP BY 1 ORDER BY 2 DESC;
```

Which stage:

```sql
SELECT jsonb_object_keys(stage_attempts) AS stage, count(*)
FROM jobs WHERE dlq_at IS NOT NULL AND created_at > now() - interval '24 hours'
GROUP BY 1 ORDER BY 2 DESC;
```

Per-stage timings, which answer "is it slow or is it broken":

```bash
docker compose -f infra/compose/docker-compose.yml --env-file .env \
  logs worker | grep pipeline_done | tail -5
```

**Mitigate.** A single failing stage usually means its dependency. Check
`/readyz` — it probes `ml` over DNS on the worker's own path, and is
deliberately NON-FATAL to readiness because ingest is built to absorb an ml
outage. A failing ml service does not stop the API.

**Rollback.** If a deploy changed a stage, roll it back. Migrations are
reversible and verified as such; a schema change is not usually the cause here.

**Escalate** if the failures are spread evenly across stages. That is
infrastructure, not code.

---

## 4. `api_5xx` — server error rate above 1%

**Symptom.** Over 1% of requests in the last 15 minutes returned 5xx, across at
least 20 requests.

**Likely causes:**

1. **An unhandled exception on a hot path.** Counted even when Starlette turns
   it into a 500 after the middleware returns — that case is exactly what the
   alert exists for.
2. **The database is unreachable or out of connections.** `db_pool_size=20` on
   the API. Symptom is 5xx on every endpoint at once.
3. **A dependency being surfaced as a 500 instead of a degrade.** This has
   happened: the ml service's deliberate 503 was rewritten as 500 by an
   `except Exception` handler, and the client spent the image's retry budget on
   what was meant to be backpressure.

**Verify:**

```bash
docker compose -f infra/compose/docker-compose.yml --env-file .env \
  logs api | grep -A 20 Traceback | tail -40
```

Then the counters, which are a property of the SERVICE rather than of whichever
replica answers:

```bash
curl -s localhost:8080/ops/alerts -H "Authorization: Bearer $TOKEN" | jq '.alerts[] | select(.alert=="api_5xx")'
```

Readiness, including ml reachability:

```bash
curl -s localhost:8080/readyz | jq
```

**Mitigate.** Roll back the deploy. If it is the database, check connection
count before restarting anything — restarting the API while the pool is
exhausted makes it worse.

**Rollback.** `docker compose ... up -d` on the previous image. Migrations are
expand-then-contract, so the previous version runs against the current schema.

**Escalate** if 5xx persists after a rollback. That is infrastructure.

---

## 5. `erasure_sla` — a deletion request is past its deadline

**Not one of §D3's four**, and it pages louder than any of them. The others cost
money or latency; this one has a regulator behind it. §C5: alert at 7 days, page
at 25, against a 30-day SLA.

**Symptom.** `drain_erasures` logs `erasure SLA BREACHED for N request(s)`.

**Likely causes:**

1. **A provider revocation keeps failing.** Google or the LiteLLM gateway
   unreachable. The saga retries twice an hour and never marks itself `failed`,
   because terminal would abandon a legal deadline.
2. **Object storage is rejecting the version purge.** The bucket is versioned,
   so erasure enumerates and deletes every version — a permissions change breaks
   that specifically, while ordinary reads and writes keep working.
3. **The saga is not running at all.** Worker down, or the cron not registered.

**Verify:**

```sql
SELECT id, state, attempts, sla_deadline, left(last_error, 200) AS err
FROM erasure_request WHERE state NOT IN ('confirmed', 'failed')
ORDER BY sla_deadline;
```

What has and has not completed:

```sql
SELECT id, completed_steps, counts, unpurgeable FROM erasure_request
WHERE state NOT IN ('confirmed', 'failed');
```

**Mitigate.** Read `last_error` and fix the underlying system; the saga resumes
from `completed_steps` and skips what is done, so there is nothing to undo
before retrying. To force a run:

```bash
docker compose -f infra/compose/docker-compose.yml --env-file .env exec -T worker \
  python -c "import asyncio,os;from stylist_db.session import init_engine;
from stylist_worker.erasure import drain_erasures;
init_engine(os.environ['DATABASE_URL']);print(asyncio.run(drain_erasures({})))"
```

**Rollback.** None. Erasure is not reversible and must not be.

**Escalate IMMEDIATELY at 25 days.** This is the one alert where the deadline
is not ours to move. Note that the user's account is already disabled from step
1 — what is late is the purge, not the user-visible deletion.

---

## What is deliberately NOT a page

`/ops/rerank` carries two Phase 7 SLIs — the rationale cache hit rate and the
`validator.reject` rate. They are **exit criteria tracked toward a threshold,
not pages**. §D3's instruction is "resist adding more; unactionable alerts train
people to ignore pages", and neither of those needs anyone woken up.

Check them in the morning:

```bash
curl -s localhost:8080/ops/rerank -H "Authorization: Bearer $TOKEN" | jq
```

`measured: false` means too few samples to say anything — which is NOT the same
as healthy, and is reported separately for that reason.
