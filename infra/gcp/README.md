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

## Order emails: auto-add purchases (optional, $0)

There are three ways in. All of them feed the same pipeline:

| | You need | What the user does |
|---|---|---|
| **Connect Gmail** (one click) | the Google OAuth client Calendar already uses | presses "Connect Gmail" |
| **A. Gmail inbox** (forwarding, no domain) | one dedicated Gmail account | forwards to `yourinbox+<token>@gmail.com` |
| **B. Your own domain** (forwarding) | a domain on Cloudflare | forwards to `orders-<token>@yourdomain.com` |

### Connect Gmail (reads order emails straight from the user's inbox)

This uses the **same OAuth client and the same redirect URI** (`/calendar/callback`)
as Calendar, so there's nothing new to register. It needs two switches in the Google
Cloud project that owns the OAuth client (for you: *My First Project*,
`eastern-gravity-498407-a0`):

1. **APIs & Services → Library → Gmail API → Enable.**
2. **Google Auth Platform → Data Access → Add or remove scopes →** tick
   `.../auth/gmail.readonly` → **Update → Save.**
3. While the app is in **Testing** (Google Auth Platform → Audience), add each person
   who will connect under **Test users**, up to 100.

The worker searches each connected inbox every 15 minutes. It only looks for emails
from known stores with an order-related subject, outside Promotions, and fetches
nothing else. On the first run it looks back 30 days.

**Google's rules for this permission.** `gmail.readonly` is a *restricted* scope:
- **In Testing:** it works only for listed test users, and Google expires their
  connection after **7 days**. The Profile page then shows "Reconnect Gmail".
- **For the public:** you have to publish the app. That needs Google's verification
  plus an annual third-party security assessment (CASA), roughly $500–$4,500 a year.
  Until then, forwarding (A or B below) is the way to open this to everyone.

Disconnecting Gmail while Calendar is connected deletes only the Gmail token. Google
keeps one grant per app, so revoking it would also disconnect Calendar.

### A. Gmail inbox (no domain needed)

1. Create a Gmail account used **only** for this, e.g. `yourstylist.orders@gmail.com`.
2. In that account, go to **Google Account → Security**, turn on **2-Step Verification**,
   then open **App passwords** and create one (name it "stylist"). Copy the
   16-character password.
3. Store the app password as a secret. Paste it, then press Ctrl-D:
   ```bash
   gcloud secrets create inbound-gmail-app-password --project=<PROJECT_ID> --data-file=-
   ```
4. Set `INBOUND_GMAIL_ADDRESS=yourstylist.orders@gmail.com` in `config.env`, then run
   `infra/gcp/deploy.sh vm`.

The worker checks the inbox every minute over IMAP, and marks each email read once
it's been handled. Gmail delivers every `+token` address to that one inbox, and the
token says which user the email belongs to. Gmail's API isn't used, so there's no
Google verification or security assessment to go through. Keep this account for the
app alone: anyone with its password can read every user's forwarded orders.

### B. Your own domain (Cloudflare)

Users forward order emails from Myntra, AJIO, Amazon and other stores to a private
address (`orders-<token>@<INBOUND_EMAIL_DOMAIN>`, shown on their Profile page), and
each item joins their wardrobe with the store's photo, brand, price and size.
Shipping and delivery emails for the same order don't add duplicates, and
returns and cancellations take the item out again.

```
Store email ──(Gmail filter auto-forwards)──► Cloudflare Email Routing ──► Email Worker
   ──POST /inbound/email?key=…──► api (stores email) ──outbox──► worker (reads it with
   the ORDER_EMAIL_MODEL, fetches the photo, adds the garment)
```

Cloudflare receives the mail for free. SendGrid's inbound parse would also work
(the webhook accepts the same fields), but it now needs a paid plan of at least
$19.95/month.

**Needs:** a domain whose DNS is on Cloudflare. It can be a cheap separate domain
used only for this; the free Cloudflare plan is enough.

1. **Cloudflare → your domain → Email → Email Routing → Enable.** Cloudflare adds its
   own MX and SPF records.
2. **Deploy the worker:**
   ```bash
   cd infra/cloudflare/email-worker
   npm install
   npx wrangler login
   npx wrangler deploy
   ```
3. **Give the worker the webhook URL:** run `npx wrangler secret put INBOUND_URL` and
   paste `https://<API address>/inbound/email?key=<secret>`. Print the secret with:
   ```bash
   gcloud secrets versions access latest --secret=inbound-email-secret --project=<PROJECT_ID>
   ```
   (`bootstrap.sh` creates it. Re-run bootstrap if the secret is missing.)
4. **Email Routing → Routing rules → Catch-all address → Send to a Worker →
   `stylist-order-emails` → Save.**
5. Set `INBOUND_EMAIL_DOMAIN=<that domain>` in `config.env`, then run `deploy.sh vm`.

Each user then opens **Profile → Add purchases automatically** and follows the
Gmail steps there. Gmail's forwarding-confirmation code comes through this same
path, so it appears on that page for the user to enter in Gmail.

**What gets accepted:** email from known store domains
(`packages/stylist_shop/order_email.py → KNOWN_STORES`), plus anything the user
forwards by hand from their own account email. Anything else is logged as
"ignored" on the Profile page. Raw email bodies are deleted as soon as they've
been read.

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
