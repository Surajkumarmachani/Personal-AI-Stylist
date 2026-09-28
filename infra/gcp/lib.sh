# Shared names and helpers for bootstrap.sh and deploy.sh. Sourced, not run.
#
# THE BUDGET PLAN (~$35/month): one e2-medium VM runs Postgres, both Redis,
# LiteLLM, api, worker and Caddy (infra/vm/); ml runs on Cloud Run, scaled to
# zero and private. Every resource name lives here so the two scripts cannot
# drift apart.

set -euo pipefail

GCP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$GCP_DIR/../.." && pwd)"

# One config file per environment: GCP_CONFIG=infra/gcp/staging.env deploy.sh
# all. Separate PROJECTS per environment, so staging can never read
# production's secrets or data.
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
ZONE="${ZONE:-$REGION-a}"

# Every gcloud call targets this project without touching the user's global
# `gcloud config`, which may point somewhere else entirely.
export CLOUDSDK_CORE_PROJECT="$PROJECT_ID"

AR_REPO="stylist"
REGISTRY="$REGION-docker.pkg.dev/$PROJECT_ID/$AR_REPO"

VM_NAME="stylist-vm"
VM_IP_NAME="stylist-ip"
VM_MACHINE="${VM_MACHINE:-e2-medium}"

UPLOAD_BUCKET="$PROJECT_ID-stylist-uploads"
MODELS_BUCKET="$PROJECT_ID-stylist-models"
BACKUP_BUCKET="$PROJECT_ID-stylist-backups"
DEPLOY_BUCKET="$PROJECT_ID-stylist-deploy"

RUNTIME_SA="stylist-runtime@$PROJECT_ID.iam.gserviceaccount.com"
STORAGE_SA="stylist-storage@$PROJECT_ID.iam.gserviceaccount.com"
DEPLOYER_SA="stylist-deployer@$PROJECT_ID.iam.gserviceaccount.com"

SVC_ML="stylist-ml"

secret_exists() { gcloud secrets describe "$1" >/dev/null 2>&1; }

put_secret() { # name value — creates the secret, or adds a new version
  if secret_exists "$1"; then
    printf %s "$2" | gcloud secrets versions add "$1" --data-file=- >/dev/null
  else
    printf %s "$2" | gcloud secrets create "$1" --replication-policy=automatic --data-file=- >/dev/null
  fi
}

read_secret() { gcloud secrets versions access latest --secret="$1"; }

# Hex only: these end up inside URLs, where `%`, `@` and `/` each break
# something different.
random_hex() { openssl rand -hex "${1:-32}"; }

project_number() { gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)'; }

# Cloud Run's deterministic URL, known before the service is first deployed.
run_url() { echo "https://$1-$(project_number).$REGION.run.app"; }

vm_ip() { gcloud compute addresses describe "$VM_IP_NAME" --region="$REGION" --format='value(address)'; }

# The api's public hostname. API_DOMAIN if you have one (point an A record at
# the VM's IP first); otherwise a free sslip.io name that resolves to the IP,
# which is all Let's Encrypt needs to issue a real certificate.
api_host() {
  if [ -n "${API_DOMAIN:-}" ]; then echo "$API_DOMAIN"; else echo "api.$(vm_ip | tr . -).sslip.io"; fi
}

vm_ssh() { # command — run as root on the VM
  gcloud compute ssh "$VM_NAME" --zone="$ZONE" --quiet --command="sudo bash -c $(printf %q "$1")"
}

log() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
