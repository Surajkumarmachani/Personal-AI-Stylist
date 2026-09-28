#!/usr/bin/env bash
# Build and roll out the backend to Cloud Run. Assumes bootstrap.sh has run.
#
# Usage:
#   infra/gcp/deploy.sh all        build, migrate, then deploy every service
#   infra/gcp/deploy.sh build      build and push the three images only
#   infra/gcp/deploy.sh migrate    run migrations against the current TAG
#   infra/gcp/deploy.sh services   deploy ml, litellm, api, worker at TAG
#   infra/gcp/deploy.sh models     verify local weights and sync them to GCS
#   infra/gcp/deploy.sh admin --email you@x.com [--revoke]   grant /ops access
#
# GCP_CONFIG selects the environment (default infra/gcp/config.env):
#   GCP_CONFIG=infra/gcp/staging.env infra/gcp/deploy.sh all
#
# TAG defaults to the current git SHA; set TAG=... to redeploy an older build
# (which is the whole rollback procedure: `TAG=<sha> deploy.sh services`).
#
# ORDER MATTERS in `all`: migrations run BEFORE the new code starts, so the
# old revision must tolerate the new schema for the minutes in between. That
# is the same expand-then-contract rule the compose stack already follows.

source "$(dirname "$0")/lib.sh"
cd "$REPO_ROOT"

if [ -z "${TAG:-}" ]; then
  TAG="$(git rev-parse --short HEAD)"
  if [ -n "$(git status --porcelain)" ]; then
    # Cloud Build uploads the working tree, so a dirty build contains code no
    # commit has. Tag it so nobody mistakes it for $TAG later.
    TAG="$TAG-dirty-$(date +%Y%m%d%H%M%S)"
    echo "warning: uncommitted changes — building as $TAG" >&2
  fi
fi

API_URL="${API_PUBLIC_URL:-$(run_url "$SVC_API")}"
ML_URL="$(run_url "$SVC_ML")"
LITELLM_URL="$(run_url "$SVC_LITELLM")"

# ---- env and secret wiring ---------------------------------------------------

# YAML, not --set-env-vars: CORS_ALLOW_ORIGINS is comma-separated, and commas
# are gcloud's own delimiter there.
yaml_kv() { printf "%s: '%s'\n" "$1" "${2//\'/\'\'}"; }

redis_host() { gcloud redis instances describe "$1" --region="$REGION" --format='value(host)'; }

# Shared by api, worker and the migrate job: they read one Settings class.
write_app_env() {
  local qhost chost
  qhost="$(redis_host "$REDIS_QUEUE")"
  chost="$(redis_host "$REDIS_CACHE")"
  {
    yaml_kv ENVIRONMENT production
    yaml_kv LOG_LEVEL INFO
    # One JSON object per line, so Cloud Logging sees severity (stylist_obs/logs.py).
    yaml_kv LOG_FORMAT json
    yaml_kv REDIS_QUEUE_URL "redis://$qhost:6379/0"
    yaml_kv REDIS_CACHE_URL "redis://$chost:6379/0"
    yaml_kv ML_BASE_URL "$ML_URL"
    yaml_kv LITELLM_BASE_URL "$LITELLM_URL"
    yaml_kv VLM_MODEL "${VLM_MODEL:-vlm-tagger-mock}"
    # Cloud Storage through its S3-compatible XML API, signed with the
    # stylist-storage HMAC key. Same endpoint inside and out: there is no
    # internal hostname to leak into a presigned URL, unlike MinIO.
    yaml_kv S3_ENDPOINT_URL https://storage.googleapis.com
    yaml_kv S3_PUBLIC_ENDPOINT_URL https://storage.googleapis.com
    yaml_kv S3_BUCKET "$UPLOAD_BUCKET"
    yaml_kv S3_REGION auto
    yaml_kv CORS_ALLOW_ORIGINS "$WEB_ORIGINS"
    yaml_kv WEB_BASE_URL "${WEB_BASE_URL:-${WEB_ORIGINS%%,*}}"
    yaml_kv GOOGLE_CLIENT_ID "${GOOGLE_CLIENT_ID:-}"
    yaml_kv GOOGLE_REDIRECT_URI "$API_URL/calendar/callback"
    yaml_kv GOOGLE_HOLIDAY_CALENDAR_ID "${GOOGLE_HOLIDAY_CALENDAR_ID:-en.indian#holiday@group.v.calendar.google.com}"
    yaml_kv SHOP_SUBID_PARAM "${SHOP_SUBID_PARAM:-subid}"
    yaml_kv VTON_PROVIDER "${VTON_PROVIDER:-}"
    yaml_kv VTON_BASE_URL "${VTON_BASE_URL:-}"
    yaml_kv VTON_TIMEOUT_S 900
    yaml_kv TRYON_DAILY_QUOTA "${TRYON_DAILY_QUOTA:-10}"
    yaml_kv TRYON_MAX_PASSES "${TRYON_MAX_PASSES:-2}"
    # Matched to ml's ML_MAX_CONCURRENCY, as in compose (§C2).
    yaml_kv WORKER_MAX_JOBS 2
    # Cloud SQL's default max_connections on db-custom-1-3840 is 100. The
    # local default of 20 per process x up to 8 api instances would exhaust
    # it before the worker or a migration could connect.
    yaml_kv DB_POOL_SIZE 5
  } > "$1"
}

# Secrets that must exist (bootstrap creates them) plus the optional
# third-party ones, included only when present. Cloud Run refuses to deploy a
# revision that references a secret that does not exist.
app_secrets() {
  local s="DATABASE_URL=database-url:latest,JWT_SECRET=jwt-secret:latest"
  s+=",LITELLM_MASTER_KEY=litellm-master-key:latest"
  s+=",S3_ACCESS_KEY=s3-access-key:latest,S3_SECRET_KEY=s3-secret-key:latest"
  local pair
  for pair in VTON_API_TOKEN=vton-api-token GOOGLE_CLIENT_SECRET=google-client-secret \
              GOOGLE_CALENDAR_API_KEY=google-calendar-api-key \
              SHOP_POSTBACK_SECRET=shop-postback-secret; do
    secret_exists "${pair#*=}" && s+=",$pair:latest"
  done
  echo "$s"
}

litellm_secrets() {
  local s="LITELLM_MASTER_KEY=litellm-master-key:latest"
  s+=",LITELLM_DATABASE_URL=litellm-database-url:latest"
  local pair
  for pair in GEMINI_API_KEY=gemini-api-key ANTHROPIC_API_KEY=anthropic-api-key \
              OPENROUTER_API_KEY=openrouter-api-key GROQ_API_KEY=groq-api-key \
              OPENAI_API_KEY=openai-api-key LANGFUSE_PUBLIC_KEY=langfuse-public-key \
              LANGFUSE_SECRET_KEY=langfuse-secret-key; do
    secret_exists "${pair#*=}" && s+=",$pair:latest"
  done
  echo "$s"
}

# ---- steps -------------------------------------------------------------------

cmd_build() {
  log "Building images at $TAG (Cloud Build)"
  gcloud builds submit --config=infra/gcp/cloudbuild.yaml \
    --region="$REGION" --substitutions="_REPO=$REGISTRY,_TAG=$TAG" .
}

cmd_models() {
  log "Verifying local model weights"
  local py="python3"
  [ -x .venv/bin/python ] && py=".venv/bin/python"
  # Refuse to publish weights whose checksums do not match the registry pins:
  # a corrupted file here would serve wrong predictions to every user.
  "$py" scripts/download_models.py --verify
  log "Syncing models/ to gs://$MODELS_BUCKET"
  gcloud storage rsync --recursive --delete-unmatched-destination-objects \
    --exclude='\.gitkeep$' models "gs://$MODELS_BUCKET"
}

cmd_migrate() {
  log "Migrating (job $JOB_MIGRATE at $TAG)"
  gcloud run jobs deploy "$JOB_MIGRATE" --region="$REGION" \
    --image="$REGISTRY/app:$TAG" --service-account="$RUNTIME_SA" \
    --set-cloudsql-instances="$SQL_CONN" \
    --set-secrets="MIGRATION_DATABASE_URL=migration-database-url:latest,APP_DB_PASSWORD=db-app-password:latest" \
    --command=sh \
    --args=-c,"alembic -c packages/stylist_db/alembic.ini upgrade head && python scripts/set_app_role_password.py" \
    --task-timeout=15m --max-retries=0 \
    --execute-now --wait
}

# scripts/grant_admin.py as a one-off job. It needs database access by design
# (see its docstring), and in production the database is reachable only from
# inside Cloud Run — so the script goes to the database, not the other way.
cmd_admin() {
  [ $# -gt 0 ] || { echo "usage: deploy.sh admin --email you@example.com [--revoke] | --list" >&2; exit 2; }
  local args="scripts/grant_admin.py"
  local a
  for a in "$@"; do args+=",$a"; done
  log "grant_admin.py $*"
  gcloud run jobs deploy stylist-admin --region="$REGION" \
    --image="$REGISTRY/app:$TAG" --service-account="$RUNTIME_SA" \
    --set-cloudsql-instances="$SQL_CONN" \
    --set-secrets="DATABASE_URL=database-url:latest" \
    --command=python --args="$args" \
    --task-timeout=5m --max-retries=0 --execute-now --wait
  echo "Output: gcloud logging read 'resource.labels.job_name=stylist-admin' --limit=20 --format='value(textPayload)'"
}

cmd_services() {
  local env_file
  env_file="$(mktemp)"
  write_app_env "$env_file"

  log "ml"
  # Weights come from a read-only GCS FUSE mount, the Cloud Run equivalent of
  # compose's `../../models:/models:ro` — never baked into the image (View 2).
  # 4 vCPU = the 2 threads x 2 inferences compose measured; 8Gi covers the
  # 3.9GiB measured peak plus FUSE's cache. The startup probe waits on
  # /readyz so no request lands during the ~20s model load.
  gcloud run deploy "$SVC_ML" --region="$REGION" \
    --image="$REGISTRY/ml:$TAG" --service-account="$RUNTIME_SA" \
    --ingress=internal --allow-unauthenticated --port=8000 \
    --execution-environment=gen2 --cpu=4 --memory=8Gi \
    --concurrency=4 --min-instances=1 --max-instances=4 \
    --clear-volumes --clear-volume-mounts \
    --add-volume=name=models,type=cloud-storage,bucket="$MODELS_BUCKET",readonly=true \
    --add-volume-mount=volume=models,mount-path=/models \
    --set-env-vars=ENVIRONMENT=production,LOG_FORMAT=json,U2NET_HOME=/models/u2net,MODELS_ROOT=/models,ORT_INTRA_OP_THREADS=2,ORT_ENABLE_CPU_ARENA=false,ML_MAX_CONCURRENCY=2 \
    --startup-probe=httpGet.path=/health/ready,httpGet.port=8000,periodSeconds=10,timeoutSeconds=5,failureThreshold=30 \
    --command=uvicorn --args=stylist_ml.main:app,--host,0.0.0.0,--port,8000,--no-access-log

  log "litellm"
  # No VPC flags: it needs Cloud SQL (socket) and the public internet, and
  # nothing inside the VPC. Default egress reaches the model providers.
  gcloud run deploy "$SVC_LITELLM" --region="$REGION" \
    --image="$REGISTRY/litellm:$TAG" --service-account="$RUNTIME_SA" \
    --ingress=internal --allow-unauthenticated --port=4000 \
    --cpu=1 --memory=2Gi --min-instances=1 --max-instances=4 \
    --add-cloudsql-instances="$SQL_CONN" \
    --set-env-vars=STORE_MODEL_IN_DB=True \
    --set-secrets="$(litellm_secrets)" \
    --startup-probe=httpGet.path=/health/liveliness,httpGet.port=4000,periodSeconds=10,timeoutSeconds=5,failureThreshold=12

  log "api"
  # --no-access-log: Cloud Run writes a structured request log for every call
  # already; uvicorn's copy would double the log volume and bill.
  # --timeout 600: SSE streams are capped at 300s in settings, and Cloud Run's
  # 300s default would cut them at exactly the wrong moment.
  gcloud run deploy "$SVC_API" --region="$REGION" \
    --image="$REGISTRY/app:$TAG" --service-account="$RUNTIME_SA" \
    --ingress=all --allow-unauthenticated --port=8000 \
    --cpu=1 --memory=1Gi --concurrency=80 --min-instances=1 --max-instances=8 \
    --timeout=600 \
    --add-cloudsql-instances="$SQL_CONN" "${VPC_FLAGS[@]}" \
    --env-vars-file="$env_file" --set-secrets="$(app_secrets)" \
    --command=uvicorn \
    --args=stylist_api.main:app,--host,0.0.0.0,--port,8000,--proxy-headers,--forwarded-allow-ips=*,--no-access-log

  log "worker"
  # A worker POOL, not a service: arq pulls from Redis and listens on no port,
  # and a Cloud Run service that never binds one fails its startup probe.
  local worker_secrets
  worker_secrets="$(app_secrets)"
  local worker_env="$env_file.worker"
  cp "$env_file" "$worker_env"
  if secret_exists firebase-credentials; then
    # Mounted as a file, read-only — the same shape as compose's secrets/ mount.
    worker_secrets+=",/app/secrets/firebase.json=firebase-credentials:latest"
    yaml_kv FIREBASE_CREDENTIALS_FILE /app/secrets/firebase.json >> "$worker_env"
  fi
  if ! gcloud run worker-pools deploy --help >/dev/null 2>&1; then
    cat >&2 <<'EOF'
`gcloud run worker-pools` failed to load. On Homebrew installs this is usually
the missing `grpc` module; give gcloud a Python that has it:

  python3 -m venv ~/.gcloud-py && ~/.gcloud-py/bin/pip install grpcio
  export CLOUDSDK_PYTHON=~/.gcloud-py/bin/python CLOUDSDK_PYTHON_SITEPACKAGES=1

then re-run: infra/gcp/deploy.sh services
EOF
    rm -f "$worker_env" "$env_file"
    return 1
  fi
  gcloud run worker-pools deploy "$POOL_WORKER" --region="$REGION" \
    --image="$REGISTRY/app:$TAG" --service-account="$RUNTIME_SA" \
    --instances=1 --cpu=1 --memory=2Gi \
    --add-cloudsql-instances="$SQL_CONN" "${VPC_FLAGS[@]}" \
    --env-vars-file="$worker_env" --set-secrets="$worker_secrets" \
    --command=arq --args=stylist_worker.main.WorkerSettings
  rm -f "$worker_env" "$env_file"

  cat <<EOF

Deployed $TAG.

  API:  $API_URL
  Check: curl -s $API_URL/health/ready
  (NOT /readyz: Cloud Run reserves some paths ending in z on public URLs.)

Vercel → Settings → Environment Variables:
  NEXT_PUBLIC_API_BASE=$API_URL
Google OAuth client → Authorized redirect URIs:
  $API_URL/calendar/callback
EOF
}

case "${1:-}" in
  all)      cmd_build; cmd_migrate; cmd_services ;;
  build)    cmd_build ;;
  migrate)  cmd_migrate ;;
  services) cmd_services ;;
  models)   cmd_models ;;
  admin)    shift; cmd_admin "$@" ;;
  *) sed -n '2,21p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
