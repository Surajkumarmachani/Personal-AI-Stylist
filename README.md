# Personal AI Stylist

A wardrobe cataloguing and outfit recommendation system, designed for Indian and
Western mixed wardrobes. Photograph your clothes, get them catalogued
automatically, get outfit suggestions that account for weather, occasion and
what you actually wear.

**Status:** Phase 4 built — a photo is split into garments, cut out, coloured,
moderated in-VPC, tagged through the LiteLLM gateway, embedded in pgvector, and
every field is correctable with the correction locked against future backfills.
Two exit criteria need external inputs: a real cost-per-garment number needs a
provider key (DPA outstanding), and accuracy needs the 500-image golden set.
See [build status](docs/implementation-plan.md#build-status).

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
split into garments, cut out, colour-extracted, moderated, tagged and embedded.
Every field is editable, and an edit is permanent: it is recorded in
`user_verified_fields` and the tag stage checks that column in SQL, so no
backfill or model upgrade can overwrite it.

Or drive it from the command line against the running stack:

```bash
# The exit-criteria scripts: upload path, then the full ingest pipeline.
make verify
```

### Development

```bash
make test          # full suite — sets its own DB/redis/S3 env (192 tests)
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
