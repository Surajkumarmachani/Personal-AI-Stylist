# Personal AI Stylist

A wardrobe cataloguing and outfit recommendation system, designed for Indian and
Western mixed wardrobes. Photograph your clothes, get them catalogued
automatically, get outfit suggestions that account for weather, occasion and
what you actually wear.

**Status:** Phase 10 (partial) — a photo is split into garments, cut out, coloured,
moderated in-VPC, tagged through the LiteLLM gateway, embedded in pgvector,
checked for duplicates, and every field is correctable with the correction
locked against future backfills. You can log wears, track laundry and
cost-per-wear, and search or filter the wardrobe.

Suggestions are now **reranked by an LLM behind a hard validator** (Phase 7).
Six assertions from the architecture's §C3 sit between the model and the user —
the load-bearing one checks output garment ids against input ids as an exact
set, so a model cannot dress you in a garment you do not own, or in one
belonging to somebody else. Every rejection falls back to the deterministic
ranking, so killing the provider degrades the rationale and never the ranking.

The model call happens in the **nightly job, not on your request**: a rerank
takes 3-6s against a 1500ms budget, so rationales are precomputed into a cache
and the morning request reads them in ~7-22ms.

It also suggests outfits deterministically: weather and occasion resolve to warmth,
formality and dress-code targets, candidates are assembled against
table-driven slot rules (a saree needs a blouse; a dress and trousers is two
outfits), and a six-term deterministic scorer ranks them with **zero model
calls**. `GET /suggestions` serves the nightly precompute in ~7ms, or generates
live in ~58ms when the requested context was never precomputed.

Tagging now runs against **real Gemini**, not the mock, so cost per garment is a
real number (~$0.0023 per call, batch-of-1) and corrections mean something.

Two things still need something other than code: **the owner's wardrobe
photographed** — which is what Phase 5's go/no-go, the correction rate, the
monthly cost and the outfit blind eval are all waiting on — and the **golden
set**, which is labelled data rather than users and still does not exist, so
accuracy remains unmeasured and the eval harness exits 2.

This is a **single-user** product and the plan was revised to match on
2026-09-15: the bar was 20 users and ≥2,000 garments, and it is now one real
wardrobe, catalogued in full. That measures whether the pipeline is correct, not
whether it generalises — see
[build status](docs/implementation-plan.md#build-status) for what that buys and
what it costs.

## Quickstart

Requires Docker and Python 3.12+. Nothing else — Postgres, Redis, MinIO and the
LiteLLM gateway all come up in containers, and no provider API key is needed
(tagging runs against a deterministic mock by default).

```bash
# 0. A local Python env. The containers carry their own dependencies; this is
#    for `make models`, `make test` and `make verify`, which run on the host.
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev,ml]"

# 1. Model weights, once (~176MB). NOT baked into any image: weights are data.
make models

# 2. Bring up the stack (postgres+pgvector, redis x2, minio, litellm, ml,
#    worker, api) and wait until every healthcheck is green.
make up
```

That prints the local URLs:

| what | where |
|---|---|
| API docs (Swagger) | http://localhost:8080/docs |
| API readiness — incl. ml reachability | http://localhost:8080/readyz |
| ml service readiness | http://localhost:8081/readyz |
| MinIO console | http://localhost:9001 — `minioadmin` / `minioadmin` |
| LiteLLM gateway | http://localhost:4000 |

Ports are deliberately non-standard (Postgres **55432**, Redis **63790/63791**)
so the stack cannot collide with anything else running locally.

```bash
# 3. The web UI (separate terminal). Port 3100, not 3000.
cd web && npm install && npm run dev     # -> http://localhost:3100
```

### Try it end to end

Upload a photo of clothing through the web UI and watch it get catalogued —
split into garments, cut out, colour-extracted, moderated, tagged, embedded and
checked against what you already own. Every field is editable, and an edit is
permanent: it is recorded in `user_verified_fields` and the tag stage checks
that column in SQL, so no backfill or model upgrade can overwrite it.

Upload the **same photo twice** to see dedupe: the second copy is flagged as a
possible duplicate and you are asked. It is never merged automatically — a
wrong merge destroys a garment you own and you may never notice, while a wrong
question costs one tap.

Tap **Worn today** on any item to build the wear log. Cost-per-wear appears
once a garment has a purchase price, and the most-worn ranking is what the
onboarding flow ("start with your 20 most-worn") is built on.

**A caveat worth knowing:** without a provider key, tagging runs against a
deterministic mock that answers `kurta` for everything. Colours, cutouts,
segmentation, embeddings and dedupe are all real work on your photos — only the
category-ish fields are placeholders. Set `GEMINI_API_KEY` and
`VLM_MODEL=vlm-tagger` in `.env` for real tags; the stack still starts, and the
whole gateway path still runs, with neither.

Note that `vlm-tagger` points at `gemini/`, which is Google AI Studio, whose
free-tier terms permit training on what you submit — and what you submit is
photographs of your clothes. Fine for development; production moves the row to
`vertex_ai/`, which is what the outstanding DPA is about.

Or drive it from the command line against the running stack:

```bash
# The exit-criteria scripts: upload path, then the full ingest pipeline.
make verify
```

### Development

```bash
make test          # full suite — sets its own DB/redis/S3 env (332 tests)
make test-strict   # same, but surfaces anything SKIPPED (a skip exits 0)
make check         # taxonomy + lint + typecheck + test
make logs          # follow all container logs
make down          # stop
make reset-db      # destroy and recreate all local data
```

`make test` sets its own DSNs on purpose. A bare `pytest` targets
`localhost:5432`, where the pgvector extension is absent, and the failure looks
like a broken migration rather than a misdirected connection.

### Expected performance

On an 8-vCPU Docker VM with ~5.8GB, a single photo completes end to end in
**~6s** (10s budget). A burst of 10 drains in ~26-44s: `ml` runs 2 concurrent
inferences and the worker 2 concurrent jobs, matched on purpose so neither
starves the other. `make verify` asserts the single-photo budget.

If ingests are slow, check whether something else is loading the same box
before suspecting the pipeline — the per-stage breakdown in the worker log
(`stage_done` / `pipeline_done` lines) attributes the time directly:

```bash
docker compose -f infra/compose/docker-compose.yml logs worker | grep pipeline_done
```
