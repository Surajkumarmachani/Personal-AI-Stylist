#!/usr/bin/env bash
# One-time Google Cloud setup: everything deploy.sh assumes already exists.
#
# Safe to re-run. Each step checks for its resource first and skips it if
# present, so a run that died halfway (a quota error on Cloud SQL, say) is
# resumed by running it again rather than by cleaning up by hand.
#
# Usage: infra/gcp/bootstrap.sh
#
# Takes ~15 minutes on a fresh project; Cloud SQL and Memorystore account for
# nearly all of it.

source "$(dirname "$0")/lib.sh"

log "Enabling APIs"
gcloud services enable \
  run.googleapis.com sqladmin.googleapis.com redis.googleapis.com \
  artifactregistry.googleapis.com cloudbuild.googleapis.com \
  secretmanager.googleapis.com compute.googleapis.com \
  storage.googleapis.com iam.googleapis.com \
  cloudresourcemanager.googleapis.com iamcredentials.googleapis.com \
  sts.googleapis.com logging.googleapis.com

log "Artifact Registry"
gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" >/dev/null 2>&1 ||
  gcloud artifacts repositories create "$AR_REPO" --repository-format=docker --location="$REGION"

# Newer projects run Cloud Build as the Compute default service account, which
# is not always granted push rights on a repository created after the fact.
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$REGION" \
  --member="serviceAccount:$(project_number)-compute@developer.gserviceaccount.com" \
  --role=roles/artifactregistry.writer >/dev/null

log "Service accounts"
# stylist-runtime: what every Cloud Run workload runs as.
# stylist-storage: owns ONLY the uploads bucket, and exists to hold the HMAC
#   key the S3 client signs with. Kept separate so a leaked HMAC key reaches
#   user uploads and nothing else — not secrets, not the database.
for sa in stylist-runtime stylist-storage; do
  gcloud iam service-accounts describe "$sa@$PROJECT_ID.iam.gserviceaccount.com" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "$sa"
done
for role in roles/cloudsql.client roles/secretmanager.secretAccessor \
            roles/logging.logWriter roles/monitoring.metricWriter; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$RUNTIME_SA" --role="$role" --condition=None >/dev/null
done

log "Network: Private Google Access + Cloud NAT"
# all-traffic VPC egress sends EVERYTHING into the VPC, so without NAT the
# api could reach Redis but not Gemini, Google OAuth, or the VTON provider.
gcloud compute networks subnets update default --region="$REGION" --enable-private-ip-google-access
gcloud compute routers describe stylist-router --region="$REGION" >/dev/null 2>&1 ||
  gcloud compute routers create stylist-router --network=default --region="$REGION"
gcloud compute routers nats describe stylist-nat --router=stylist-router --region="$REGION" >/dev/null 2>&1 ||
  gcloud compute routers nats create stylist-nat --router=stylist-router --region="$REGION" \
    --auto-allocate-nat-external-ips --nat-all-subnet-ip-ranges

log "Generated secrets"
for s in jwt-secret db-owner-password db-app-password; do
  secret_exists "$s" || put_secret "$s" "$(random_hex)"
done
secret_exists litellm-master-key || put_secret litellm-master-key "sk-$(random_hex)"

log "Cloud SQL (Postgres 16) — the slow step"
if ! gcloud sql instances describe "$SQL_INSTANCE" >/dev/null 2>&1; then
  gcloud sql instances create "$SQL_INSTANCE" \
    --database-version=POSTGRES_16 --edition=ENTERPRISE --tier="${DB_TIER:-db-custom-1-3840}" \
    --region="$REGION" --availability-type=ZONAL \
    --storage-auto-increase --backup-start-time=20:00 --enable-point-in-time-recovery
fi
for db in stylist litellm; do
  gcloud sql databases describe "$db" --instance="$SQL_INSTANCE" >/dev/null 2>&1 ||
    gcloud sql databases create "$db" --instance="$SQL_INSTANCE"
done
# stylist_owner runs migrations and nothing else. gcloud-created users are
# members of cloudsqlsuperuser, which is what lets 0002 CREATE EXTENSION vector.
# The app role, stylist_app, is created by migration 0001 and gets its real
# password from scripts/set_app_role_password.py on every migrate run.
OWNER_PW="$(read_secret db-owner-password)"
APP_PW="$(read_secret db-app-password)"
if gcloud sql users list --instance="$SQL_INSTANCE" --format='value(name)' | grep -qx stylist_owner; then
  gcloud sql users set-password stylist_owner --instance="$SQL_INSTANCE" --password="$OWNER_PW"
else
  gcloud sql users create stylist_owner --instance="$SQL_INSTANCE" --password="$OWNER_PW"
fi
# Unix socket via the Cloud SQL connector (--add-cloudsql-instances), so the
# database needs no private IP and no VPC peering.
SOCK="/cloudsql/$SQL_CONN"
put_secret database-url           "postgresql+asyncpg://stylist_app:$APP_PW@/stylist?host=$SOCK"
put_secret migration-database-url "postgresql://stylist_owner:$OWNER_PW@/stylist?host=$SOCK"
# Prisma (LiteLLM) wants a placeholder host alongside the socket path.
put_secret litellm-database-url   "postgresql://stylist_owner:$OWNER_PW@localhost/litellm?host=$SOCK"

log "Memorystore for Redis"
if ! gcloud redis instances describe "$REDIS_QUEUE" --region="$REGION" >/dev/null 2>&1; then
  # The queue: noeviction and RDB snapshots. An evicted or lost job is work
  # that silently never happens.
  gcloud redis instances create "$REDIS_QUEUE" --region="$REGION" --tier=basic \
    --size="${REDIS_SIZE_GB:-1}" --redis-version=redis_7_2 --network=default \
    --redis-config=maxmemory-policy=noeviction \
    --persistence-mode=rdb --rdb-snapshot-period=1h
fi
if ! gcloud redis instances describe "$REDIS_CACHE" --region="$REGION" >/dev/null 2>&1; then
  gcloud redis instances create "$REDIS_CACHE" --region="$REGION" --tier=basic \
    --size="${REDIS_SIZE_GB:-1}" --redis-version=redis_7_2 --network=default \
    --redis-config=maxmemory-policy=allkeys-lru
fi

log "Cloud Storage buckets"
for b in "$UPLOAD_BUCKET" "$MODELS_BUCKET"; do
  gcloud storage buckets describe "gs://$b" >/dev/null 2>&1 ||
    gcloud storage buckets create "gs://$b" --location="$REGION" \
      --uniform-bucket-level-access --public-access-prevention
done
# The browser uploads straight to the bucket (presigned POST) and loads images
# from it (presigned GET), so the bucket — not just the API — must allow the
# Vercel origin. Missing this presents as "Failed to fetch" on upload.
cors_file="$(mktemp)"
origins_json="$(printf '%s' "$WEB_ORIGINS" | tr ',' '\n' | sed 's/^ *//;s/ *$//;/^$/d;s/.*/"&"/' | paste -sd, -)"
cat > "$cors_file" <<EOF
[{"origin": [$origins_json],
  "method": ["GET", "HEAD", "POST", "PUT"],
  "responseHeader": ["Content-Type", "ETag"],
  "maxAgeSeconds": 3600}]
EOF
gcloud storage buckets update "gs://$UPLOAD_BUCKET" --cors-file="$cors_file"
rm -f "$cors_file"

gcloud storage buckets add-iam-policy-binding "gs://$UPLOAD_BUCKET" \
  --member="serviceAccount:$STORAGE_SA" --role=roles/storage.objectAdmin >/dev/null
# ml reads weights through a read-only FUSE mount; viewer is all it needs.
gcloud storage buckets add-iam-policy-binding "gs://$MODELS_BUCKET" \
  --member="serviceAccount:$RUNTIME_SA" --role=roles/storage.objectViewer >/dev/null

log "HMAC key for the S3-compatible client"
if ! secret_exists s3-access-key; then
  read -r access_id hmac_secret < <(gcloud storage hmac create "$STORAGE_SA" \
    --format='value(metadata.accessId,secret)')
  put_secret s3-access-key "$access_id"
  put_secret s3-secret-key "$hmac_secret"
fi

if [ -n "${GITHUB_REPO:-}" ]; then
  log "GitHub Actions deploy identity (Workload Identity Federation)"
  # GitHub's OIDC token is exchanged for short-lived credentials on
  # stylist-deployer. There is no JSON key to leak, and the attribute
  # condition means only $GITHUB_REPO — not any repo on GitHub — can use it.
  gcloud iam service-accounts describe "$DEPLOYER_SA" >/dev/null 2>&1 ||
    gcloud iam service-accounts create stylist-deployer
  # viewer: the describes deploy.sh runs (redis host, secret existence,
  # project number). The rest are exactly the writes it makes.
  for role in roles/viewer roles/run.admin roles/cloudbuild.builds.editor \
              roles/artifactregistry.writer roles/storage.objectAdmin \
              roles/serviceusage.serviceUsageConsumer; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:$DEPLOYER_SA" --role="$role" --condition=None >/dev/null
  done
  # actAs: deploy revisions that run as stylist-runtime, and builds that run
  # as the Compute default account.
  for sa in "$RUNTIME_SA" "$(project_number)-compute@developer.gserviceaccount.com"; do
    gcloud iam service-accounts add-iam-policy-binding "$sa" \
      --member="serviceAccount:$DEPLOYER_SA" --role=roles/iam.serviceAccountUser >/dev/null
  done
  gcloud iam workload-identity-pools describe github --location=global >/dev/null 2>&1 ||
    gcloud iam workload-identity-pools create github --location=global --display-name="GitHub Actions"
  gcloud iam workload-identity-pools providers describe github-oidc \
    --workload-identity-pool=github --location=global >/dev/null 2>&1 ||
    gcloud iam workload-identity-pools providers create-oidc github-oidc \
      --workload-identity-pool=github --location=global \
      --issuer-uri=https://token.actions.githubusercontent.com \
      --attribute-mapping=google.subject=assertion.sub,attribute.repository=assertion.repository \
      --attribute-condition="assertion.repository == '$GITHUB_REPO'"
  pool_id="projects/$(project_number)/locations/global/workloadIdentityPools/github"
  gcloud iam service-accounts add-iam-policy-binding "$DEPLOYER_SA" \
    --role=roles/iam.workloadIdentityUser \
    --member="principalSet://iam.googleapis.com/$pool_id/attribute.repository/$GITHUB_REPO" >/dev/null
  cat <<EOF

GitHub → Settings → Environments → create one per deploy target
(e.g. "production") with these VARIABLES (not secrets — none are sensitive):

  GCP_WIF_PROVIDER = $pool_id/providers/github-oidc
  GCP_DEPLOYER_SA  = $DEPLOYER_SA
  GCP_CONFIG_ENV   = <the full contents of $(basename "$GCP_CONFIG")>
EOF
fi

cat <<EOF

Bootstrap complete.

Next, add the third-party secrets you use (each is optional; deploy.sh wires
up only the ones that exist). Paste the value, then press Ctrl-D:

  gcloud secrets create gemini-api-key          --project=$PROJECT_ID --data-file=-
  gcloud secrets create anthropic-api-key       --project=$PROJECT_ID --data-file=-
  gcloud secrets create openrouter-api-key      --project=$PROJECT_ID --data-file=-
  gcloud secrets create groq-api-key            --project=$PROJECT_ID --data-file=-
  gcloud secrets create vton-api-token          --project=$PROJECT_ID --data-file=-
  gcloud secrets create google-client-secret    --project=$PROJECT_ID --data-file=-
  gcloud secrets create google-calendar-api-key --project=$PROJECT_ID --data-file=-
  gcloud secrets create shop-postback-secret    --project=$PROJECT_ID --data-file=-

Firebase push (a file, not a pasted value):

  gcloud secrets create firebase-credentials --project=$PROJECT_ID \\
    --data-file=secrets/<your-firebase-adminsdk>.json

Then: infra/gcp/deploy.sh models && infra/gcp/deploy.sh all
EOF
