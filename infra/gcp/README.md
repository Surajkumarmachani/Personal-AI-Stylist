# Deploying: backend on Google Cloud (~$35/month), frontend on Vercel

```
 browser / installed desktop app (PWA)
        │
        ▼
 Vercel (web/, free Hobby plan) ──NEXT_PUBLIC_API_BASE──►  https://api.<ip>.sslip.io
                                                               │
 ┌─────────────── Compute Engine VM: stylist-vm (e2-medium, 4 GB) ─────────────┐
 │  Caddy (HTTPS) → api ─┬─ Postgres (pgvector)   worker (arq) ─┐              │
 │                       ├─ redis-queue (noeviction)             │              │
 │                       ├─ redis-cache (LRU)                    │              │
 │                       └─ litellm ──► OpenRouter / Gemini      │              │
 └───────────────────────────────────────────────────────────────┼──────────────┘
                                                                 │ ID token
                                           Cloud Run: stylist-ml (private, scales to 0)
 Cloud Storage: uploads (S3 API + HMAC) · models · backups (30 days) · deploy bundle
```

| What | Where | ≈ per month |
|---|---|---|
| Postgres, both Redis, litellm, api, worker, Caddy | 1 × e2-medium VM, `asia-south1-a` | $28 |
| VM disk (30 GB) + daily snapshots (7 days) | Compute Engine | $3 |
| Static IP | Compute Engine | $3.60 |
| ml | Cloud Run, min 0 / max 2 instances, private | $0–3 |
| Uploads, models, backups, images | Cloud Storage / Artifact Registry | ~$1–2 |
| **Total** | | **~$35** |

Everything local compose runs is here except MinIO (replaced by Cloud Storage)
and ml (moved to Cloud Run, where it is billed only while working).

## First deploy

Prerequisites: `gcloud auth login`, a project with billing, and model weights
downloaded locally (`.venv/bin/python scripts/download_models.py`).

```bash
cp infra/gcp/config.env.example infra/gcp/config.env   # fill it in
infra/gcp/bootstrap.sh     # ~5 min: buckets, secrets, IP, firewall, the VM
# add third-party keys to Secret Manager (bootstrap prints the command)
infra/gcp/deploy.sh models # verify checksums, upload weights for ml
infra/gcp/deploy.sh all    # build images → deploy ml → update the VM
```

`deploy.sh all` finishes by printing the API address (`https://api.<ip>.sslip.io`). Then:

1. **Vercel**: import the repo with **Root Directory = `web`**. Set
   `NEXT_PUBLIC_API_BASE` to the API address and add the five
   `NEXT_PUBLIC_FIREBASE_*` values from the root `.env`. Redeploy whenever
   these change, because they're baked in at build time.
2. **Google OAuth client**: add `<API address>/calendar/callback` as a redirect URI.
3. **Firebase → Authentication → Authorized domains**: add the Vercel domain.
4. Put the real Vercel domain in `WEB_ORIGINS`/`WEB_BASE_URL`, then re-run
   `bootstrap.sh` (bucket CORS) and `deploy.sh vm` (API CORS).
5. `infra/gcp/deploy.sh admin --email you@example.com` to get `/ops` access.

## Everyday

```bash
infra/gcp/deploy.sh all                  # ship the current commit
TAG=<git sha> infra/gcp/deploy.sh vm     # roll back to an earlier build
infra/gcp/deploy.sh status               # container status on the VM
infra/gcp/deploy.sh logs worker          # recent logs for one service
gcloud compute ssh stylist-vm --zone=asia-south1-a   # a shell on the VM
```

Logs also go to **Cloud Logging** (Console → Logging) as JSON with real severities.

## What runs where on the VM

`/opt/stylist/` holds `docker-compose.yml`, `Caddyfile`, `app.env` (non-secret)
and `.env`, which is rendered from Secret Manager on every deploy (root-only).
`update.sh` runs on each deploy: it pulls images, runs migrations, then
restarts the containers. The containers restart by themselves after a reboot.
The VM's startup script (`infra/vm/vm-setup.sh`) installs Docker on first
boot and adds 2 GB of swap.

**Backups**: a nightly `pg_dump` at 03:00 IST goes to the backups bucket
(kept 30 days), plus daily disk snapshots (kept 7 days).

## Verify after the first deploy

- `curl https://<API address>/health/ready`: every dependency reports ok. The first
  call can take about 60 s because it wakes ml. Use this path, **not** `/readyz`:
  Cloud Run reserves some paths ending in `z`.
- **Upload a garment from the Vercel site.** This is the one flow to test by hand:
  a presigned POST, signed with SigV4, against Cloud Storage's S3 API. If uploads
  return 403, check the bucket CORS first.

## Trade-offs of the $35 plan

- **Everything but ml is on one VM.** If the VM goes down, the app is down
  until it's back; it restarts itself after a reboot, and the data is in the
  backups above. There's no automatic failover.
- **ml cold starts.** The first photo after a quiet period waits ~30–60 s while
  ml starts and loads its models. Ingest runs in the background, so the user
  sees "processing" rather than an error.
- **The api container's healthcheck is liveness (`/healthz`).** Readiness
  would call ml every few seconds and keep it awake, which costs ~$80/month.
- **Vercel Hobby is non-commercial.** Move to Pro ($20/month) once you charge users.
- **Outgrowing it**: resize the VM (`VM_MACHINE=e2-standard-2`, ~$55) before
  reaching for managed Postgres or Redis.

## Deploying from GitHub (optional)

Set `GITHUB_REPO=owner/name` in `config.env` and re-run `bootstrap.sh`. It
creates a keyless deploy identity (Workload Identity Federation) and prints
three variables for a GitHub environment named `production`. Then use
**Actions → Deploy / GCP Setup / GCP Admin → Run workflow**. Deploy refuses
commits whose CI run didn't pass.

## Gotchas

- **`No module named 'grpc'` from gcloud.** Homebrew's gcloud is missing it:
  `python3 -m venv ~/.gcloud-py && ~/.gcloud-py/bin/pip install grpcio`, then
  `export CLOUDSDK_PYTHON=~/.gcloud-py/bin/python CLOUDSDK_PYTHON_SITEPACKAGES=1`.
- **The first `gcloud compute ssh`** creates an SSH key and signs in through
  OS Login. That's normal, and it only happens once.
- **Vercel preview URLs** aren't in the CORS list; only exact origins are allowed.
- **Try-on**: a `gradio.live` `VTON_BASE_URL` expires after about 72 hours.
