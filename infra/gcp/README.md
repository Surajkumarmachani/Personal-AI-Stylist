# Deploying: backend on Google Cloud, frontend on Vercel

```
 browser / installed desktop app (PWA)
        │
        ▼
 Vercel (web/)  ──NEXT_PUBLIC_API_BASE──►  Cloud Run: stylist-api  (public)
                                               │  Direct VPC egress
            ┌──────────────────────────────────┼─────────────────────────┐
            ▼                  ▼               ▼                         ▼
   Cloud Run: stylist-ml  Cloud Run:     Memorystore ×2          Cloud SQL (PG16)
   (internal, GCS FUSE    stylist-litellm (queue: noeviction,    stylist + litellm DBs
    mount of weights)     (internal)       cache: allkeys-lru)
            ▲                  ▲               ▲                         ▲
            └──────── Cloud Run worker pool: stylist-worker (arq) ───────┘
 Cloud Storage: uploads bucket (S3 API + HMAC key), models bucket
```

| compose service | Google Cloud |
|---|---|
| `api` | Cloud Run service, public |
| `worker` | Cloud Run **worker pool** (arq listens on no port) |
| `ml` | Cloud Run service, internal, 4 vCPU / 8 GiB, weights on a read-only GCS mount |
| `litellm` | Cloud Run service, internal, image = upstream + baked `config.yaml` |
| `migrate` | Cloud Run job, then `scripts/set_app_role_password.py` |
| `postgres` | Cloud SQL for PostgreSQL 16, reached over the Cloud SQL socket |
| `redis-queue` / `redis-cache` | two Memorystore instances (eviction policy is per instance) |
| `minio` | Cloud Storage, through its S3-compatible API |
| `.env` / `secrets/` | Secret Manager |

## First deploy

Prerequisites: `gcloud` logged in (`gcloud auth login`), a project with
billing enabled, and model weights downloaded locally
(`python scripts/download_models.py`).

```bash
cp infra/gcp/config.env.example infra/gcp/config.env   # fill it in
infra/gcp/bootstrap.sh          # ~15 min, one time; safe to re-run
# add the optional third-party secrets it prints (Gemini, VTON, Firebase…)
infra/gcp/deploy.sh models      # verify checksums, sync weights to GCS
infra/gcp/deploy.sh all         # build → migrate → deploy
```

`deploy.sh` finishes by printing the API URL. Then:

1. **Vercel.** Import the repo and set **Root Directory = `web`**. Add these
   environment variables (Production and Preview):
   - `NEXT_PUBLIC_API_BASE` = the API URL
   - `NEXT_PUBLIC_FIREBASE_API_KEY`, `NEXT_PUBLIC_FIREBASE_PROJECT_ID`,
     `NEXT_PUBLIC_FIREBASE_SENDER_ID`, `NEXT_PUBLIC_FIREBASE_APP_ID`,
     `NEXT_PUBLIC_FIREBASE_VAPID_KEY`, copied from the root `.env`

   These are inlined at build time, so redeploy on Vercel after changing any of them.
2. **Google OAuth client.** Add `<API URL>/calendar/callback` as an authorized
   redirect URI.
3. **Firebase console → Authentication → Authorized domains.** Add the Vercel domain.
4. Put the Vercel URL in `WEB_ORIGINS` and `WEB_BASE_URL` in `config.env`. If it
   changed, re-run `bootstrap.sh` (for the bucket CORS) and
   `deploy.sh services` (for the API CORS).
5. Make yourself an admin so `/ops` works:
   `infra/gcp/deploy.sh admin --email you@example.com`

## Everyday

```bash
infra/gcp/deploy.sh all                      # ship the current commit
TAG=<git sha> infra/gcp/deploy.sh services   # roll back to an earlier build
infra/gcp/deploy.sh admin --list             # who can see /ops
```

**From GitHub instead of a laptop.** Set `GITHUB_REPO=owner/name` in
`config.env` and re-run `bootstrap.sh`. It creates a keyless deploy identity
(Workload Identity Federation) and prints three variables to add to a GitHub
*environment* named e.g. `production`. Then go to **Actions → Deploy → Run
workflow**. The workflow refuses to deploy a commit whose CI run didn't pass,
and finishes with a smoke check against `/health/ready`.

**Staging.** Use a separate GCP project, with its own `infra/gcp/staging.env`,
then `GCP_CONFIG=infra/gcp/staging.env infra/gcp/bootstrap.sh` (and the same
prefix for `deploy.sh`). A separate project means staging can't reach
production's secrets or data.

Images are tagged with the git SHA. A build made from a dirty tree is tagged
`<sha>-dirty-<timestamp>` so it can't be mistaken for the commit.

## Verify after the first deploy

- `curl <API URL>/health/ready`: every dependency reports ok. Use this and
  **not** `/readyz`: Cloud Run reserves some paths ending in `z` on public
  URLs, so the z-paths are for compose and in-container probes only.
- **Upload a garment from the Vercel site.** This is the one step to actually
  test by hand. Uploads use a presigned **POST** with a size policy
  (`packages/stylist_clients/storage.py`), signed with SigV4 against Cloud
  Storage's S3 API. If uploads 403, check `S3_REGION` (set to `auto`) and the
  bucket CORS before anything else.
- Worker: `gcloud run worker-pools logs read stylist-worker --region=$REGION`
  shows `worker started, environment=production`.

## What production refuses to do

- **Start with a credential from git.** With `ENVIRONMENT` set to anything other than `local`, the
  api and worker won't boot on the MinIO keys, the local LiteLLM master key,
  the local app-role password, or the dev JWT secret
  (`services/stylist_api/settings.py`). The new revision fails to start and
  never takes traffic, instead of going live with broken uploads.
- **Build the frontend without an API.** A Vercel *production* build fails if
  `NEXT_PUBLIC_API_BASE` is unset, instead of shipping a site that points at
  localhost.
- **Log unstructured text.** `LOG_FORMAT=json` gives Cloud Logging a real
  severity on every line, so you can alert on `severity>=ERROR`.

## Gotchas

- **`gcloud run worker-pools` fails with `No module named 'grpc'`.** Homebrew
  installs of gcloud ship without it. `deploy.sh` detects this and prints the fix.
- **CORS on Vercel preview URLs.** Every preview deploy gets a new hostname,
  and the API allows only exact origins. Point previews at a separate
  staging backend, or add the specific preview origin.
- **Cost floor.** `min-instances=1` on api, ml (4 vCPU) and litellm, plus
  Cloud SQL and two Memorystore instances, means the stack costs money even when
  idle. That's deliberate: ml takes about 20 s to load its models, and scaling
  to zero would put that delay on some user's first request. Lower
  `--min-instances` in `deploy.sh` for a staging project.
- **DB connections.** `DB_POOL_SIZE=5` × up to 8 api instances, plus the worker,
  stays under Cloud SQL's default of 100 connections. Raise both together.
