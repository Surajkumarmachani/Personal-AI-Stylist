# Shared names and helpers for bootstrap.sh and deploy.sh. Sourced, not run.
#
# Every resource name lives here so the two scripts cannot drift: a bucket
# bootstrap creates under one name and deploy points at under another fails
# only at runtime, as an upload that 403s.

set -euo pipefail

GCP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$GCP_DIR/../.." && pwd)"

# One config file per environment: GCP_CONFIG=infra/gcp/staging.env deploy.sh
# all. Separate PROJECTS per environment, not name prefixes in one project, so
# staging can never read production's secrets or database.
GCP_CONFIG="${GCP_CONFIG:-$GCP_DIR/config.env}"
if [ ! -f "$GCP_CONFIG" ]; then
  echo "missing $GCP_CONFIG — copy infra/gcp/config.env.example and fill it in" >&2
  exit 1
fi
# shellcheck source=/dev/null
source "$GCP_CONFIG"
: "${PROJECT_ID:?set PROJECT_ID in config.env}"
: "${REGION:?set REGION in config.env}"
: "${WEB_ORIGINS:?set WEB_ORIGINS in config.env}"

# Every gcloud call targets this project without touching the user's global
# `gcloud config`, which may point somewhere else entirely.
export CLOUDSDK_CORE_PROJECT="$PROJECT_ID"

AR_REPO="stylist"
REGISTRY="$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO"

SQL_INSTANCE="stylist-pg"
SQL_CONN="$PROJECT_ID:$REGION:$SQL_INSTANCE"

# Two instances, not db0/db1 on one: maxmemory-policy is per INSTANCE, so a
# single instance cannot be noeviction for the queue and LRU for the cache.
REDIS_QUEUE="stylist-queue"
REDIS_CACHE="stylist-cache"

UPLOAD_BUCKET="$PROJECT_ID-stylist-uploads"
MODELS_BUCKET="$PROJECT_ID-stylist-models"

RUNTIME_SA="stylist-runtime@$PROJECT_ID.iam.gserviceaccount.com"
DEPLOYER_SA="stylist-deployer@$PROJECT_ID.iam.gserviceaccount.com"
STORAGE_SA="stylist-storage@$PROJECT_ID.iam.gserviceaccount.com"

SVC_API="stylist-api"
SVC_ML="stylist-ml"
SVC_LITELLM="stylist-litellm"
POOL_WORKER="stylist-worker"
JOB_MIGRATE="stylist-migrate"

# Direct VPC egress, all traffic: Memorystore is only reachable inside the VPC,
# and ml/litellm are ingress=internal, which only accepts calls arriving from
# it. Outbound internet (Gemini, VTON, Google APIs) leaves through Cloud NAT.
VPC_FLAGS=(--network=default --subnet=default --vpc-egress=all-traffic)

secret_exists() { gcloud secrets describe "$1" >/dev/null 2>&1; }

put_secret() { # name value — creates the secret, or adds a new version
  if secret_exists "$1"; then
    printf %s "$2" | gcloud secrets versions add "$1" --data-file=- >/dev/null
  else
    printf %s "$2" | gcloud secrets create "$1" --replication-policy=automatic --data-file=- >/dev/null
  fi
}

read_secret() { gcloud secrets versions access latest --secret="$1"; }

# Hex only: these end up inside URLs and alembic's configparser, where `%`,
# `@` and `/` each break something different.
random_hex() { openssl rand -hex "${1:-32}"; }

project_number() { gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)'; }

# Cloud Run's deterministic URL, known before the service is first deployed —
# which is what lets the api be configured with ml's URL in a single pass.
run_url() { echo "https://$1-$(project_number).$REGION.run.app"; }

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
