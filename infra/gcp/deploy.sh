#!/usr/bin/env bash
# Build and roll out the budget plan. Assumes bootstrap.sh has run.
#
# Usage:
#   infra/gcp/deploy.sh all        build, then deploy ml and the VM
#   infra/gcp/deploy.sh build      build and push the three images only
#   infra/gcp/deploy.sh ml         deploy the ml service (Cloud Run) at TAG
#   infra/gcp/deploy.sh vm         deploy everything else (the VM) at TAG
#   infra/gcp/deploy.sh models     verify local weights and sync them to GCS
#   infra/gcp/deploy.sh admin --email you@x.com [--revoke] | --list
#   infra/gcp/deploy.sh status     container status on the VM
#   infra/gcp/deploy.sh logs [service]   recent logs (api, worker, litellm…)
#
# TAG defaults to the current git SHA; `TAG=<sha> deploy.sh vm` redeploys an
# older build, which is the whole rollback procedure. Migrations run inside
# `vm` before the new containers start, so the old version must tolerate the
# new schema for the moment in between (expand, then contract).
#
# GCP_CONFIG selects the environment (default infra/gcp/config.env).

source "$(dirname "$0")/lib.sh"
cd "$REPO_ROOT"

TAG_GIVEN="${TAG:-}"
if [ -z "${TAG:-}" ]; then
  TAG="$(git rev-parse --short HEAD)"
  if [ -n "$(git status --porcelain)" ]; then
    # Cloud Build uploads the working tree, so a dirty build contains code no
    # commit has. Tag it so nobody mistakes it for $TAG later.
    TAG="$TAG-dirty-$(date +%Y%m%d%H%M%S)"
    echo "warning: uncommitted changes — building as $TAG" >&2
  fi
fi

ML_URL="$(run_url "$SVC_ML")"

cmd_build() {
  log "Building images at $TAG (Cloud Build)"
  gcloud builds submit --config=infra/gcp/cloudbuild.yaml \
    --region="$REGION" --substitutions="_REPO=$REGISTRY,_TAG=$TAG" .
}

cmd_models() {
  log "Verifying local model weights"
  local py="python3"
  [ -x .venv/bin/python ] && py=".venv/bin/python"
  # Refuse to publish weights whose checksums do not match the registry pins.
  "$py" scripts/download_models.py --verify
  log "Syncing models/ to gs://$MODELS_BUCKET"
  gcloud storage rsync --recursive --delete-unmatched-destination-objects \
    --exclude='\.gitkeep$' models "gs://$MODELS_BUCKET"
}

cmd_ml() {
  log "ml (Cloud Run, scale-to-zero, private) at $TAG"
  # min-instances 0: billed only while it works. The price is a cold start
  # (~30-60 s) on the first photo after a quiet spell; ingest is a background
  # job, so the user sees "processing" rather than an error.
  # --no-allow-unauthenticated: only identities with run.invoker (the VM's
  # service account) get in. An open URL would be 4 vCPUs anyone could spend.
  # --max-instances 2 caps the worst-case bill.
  gcloud run deploy "$SVC_ML" --region="$REGION" \
    --image="$REGISTRY/ml:$TAG" --service-account="$RUNTIME_SA" \
    --ingress=all --no-allow-unauthenticated --port=8000 \
    --execution-environment=gen2 --cpu=4 --memory=8Gi --cpu-boost \
    --concurrency=4 --min-instances=0 --max-instances=2 --timeout=300 \
    --clear-volumes --clear-volume-mounts \
    --add-volume=name=models,type=cloud-storage,bucket="$MODELS_BUCKET",readonly=true \
    --add-volume-mount=volume=models,mount-path=/models \
    --set-env-vars=ENVIRONMENT=production,LOG_FORMAT=json,U2NET_HOME=/models/u2net,MODELS_ROOT=/models,ORT_INTRA_OP_THREADS=2,ORT_ENABLE_CPU_ARENA=false,ML_MAX_CONCURRENCY=2 \
    --startup-probe=httpGet.path=/health/ready,httpGet.port=8000,periodSeconds=10,timeoutSeconds=5,failureThreshold=30 \
    --command=uvicorn --args=stylist_ml.main:app,--host,0.0.0.0,--port,8000,--no-access-log
}

# Non-secret settings for the VM. Secrets are added ON the VM by update.sh,
# straight from Secret Manager, so they never pass through this machine.
write_app_env() {
  local host="$1"
  kv() { printf "%s='%s'\n" "$1" "${2//\'/}"; }
  {
    kv TAG "$TAG"
    kv REGISTRY "$REGISTRY"
    kv API_HOST "$host"
    kv BACKUP_BUCKET "$BACKUP_BUCKET"
    kv ENVIRONMENT production
    kv LOG_LEVEL INFO
    kv LOG_FORMAT json
    kv REDIS_QUEUE_URL redis://redis-queue:6379/0
    kv REDIS_CACHE_URL redis://redis-cache:6379/0
    kv ML_BASE_URL "$ML_URL"
    kv ML_AUTH_AUDIENCE "$ML_URL"
    kv LITELLM_BASE_URL http://litellm:4000
    kv VLM_MODEL "${VLM_MODEL:-vlm-tagger-mock}"
    # Cloud Storage through its S3-compatible XML API, signed with the
    # stylist-storage HMAC key.
    kv S3_ENDPOINT_URL https://storage.googleapis.com
    kv S3_PUBLIC_ENDPOINT_URL https://storage.googleapis.com
    kv S3_BUCKET "$UPLOAD_BUCKET"
    kv S3_REGION auto
    kv CORS_ALLOW_ORIGINS "$WEB_ORIGINS"
    kv WEB_BASE_URL "${WEB_BASE_URL:-${WEB_ORIGINS%%,*}}"
    kv GOOGLE_CLIENT_ID "${GOOGLE_CLIENT_ID:-}"
    kv GOOGLE_REDIRECT_URI "https://$host/calendar/callback"
    kv GOOGLE_HOLIDAY_CALENDAR_ID "${GOOGLE_HOLIDAY_CALENDAR_ID:-en.indian#holiday@group.v.calendar.google.com}"
    kv SHOP_SUBID_PARAM "${SHOP_SUBID_PARAM:-subid}"
    # Forwarded order emails (Phase 15). Empty keeps the feature off.
    kv INBOUND_EMAIL_DOMAIN "${INBOUND_EMAIL_DOMAIN:-}"
    kv ORDER_EMAIL_MODEL "${ORDER_EMAIL_MODEL:-vlm-tagger}"
    kv VTON_PROVIDER "${VTON_PROVIDER:-}"
    kv VTON_BASE_URL "${VTON_BASE_URL:-}"
    kv VTON_TIMEOUT_S 900
    kv TRYON_DAILY_QUOTA "${TRYON_DAILY_QUOTA:-10}"
    kv TRYON_MAX_PASSES "${TRYON_MAX_PASSES:-2}"
    kv WORKER_MAX_JOBS 2
    kv DB_POOL_SIZE 5
  } > "$2"
}

image_exists() {
  gcloud artifacts docker images describe "$REGISTRY/app:$1" >/dev/null 2>&1
}

cmd_vm() {
  local host bundle tmp
  host="$(api_host)"
  bundle="gs://$DEPLOY_BUCKET/bundle"
  # `deploy.sh vm` on its own (a config change, say) should not need a
  # rebuild. When no image exists for the computed TAG, and none was asked
  # for explicitly, reuse the version the VM is running now.
  if ! image_exists "$TAG"; then
    local current
    current="$(gcloud storage cat "$bundle/app.env" 2>/dev/null | sed -n "s/^TAG='\(.*\)'$/\1/p")"
    if [ -z "$TAG_GIVEN" ] && [ -n "$current" ] && image_exists "$current"; then
      echo "no image for $TAG; redeploying the current version $current" >&2
      TAG="$current"
    else
      echo "no image $REGISTRY/app:$TAG — run 'deploy.sh build' first, or set TAG" >&2
      exit 1
    fi
  fi
  tmp="$(mktemp -d)"
  write_app_env "$host" "$tmp/app.env"
  cp infra/vm/docker-compose.yml infra/vm/Caddyfile infra/vm/update.sh infra/vm/backup.sh "$tmp/"

  log "Uploading the VM bundle ($TAG)"
  gcloud storage cp "$tmp"/* "$bundle/" --quiet
  rm -rf "$tmp"

  log "Updating the VM (pull, migrate, restart)"
  vm_ssh "mkdir -p /opt/stylist && gcloud storage cp $bundle/update.sh /opt/stylist/update.sh --quiet && bash /opt/stylist/update.sh $bundle"

  cat <<EOF

Deployed $TAG.

  API:   https://$host
  Check: curl -s https://$host/health/ready
         (the first call can take ~60 s: it wakes the ml service)

Vercel → Settings → Environment Variables (then redeploy the frontend):
  NEXT_PUBLIC_API_BASE=https://$host
Google OAuth client → Authorized redirect URIs:
  https://$host/calendar/callback
EOF
  if [ -n "${INBOUND_EMAIL_DOMAIN:-}" ]; then
    cat <<EOF
Order emails → the Cloudflare worker's INBOUND_URL secret:
  https://$host/inbound/email?key=<the value of the inbound-email-secret secret>
  (print it with: gcloud secrets versions access latest --secret=inbound-email-secret --project=$PROJECT_ID)
EOF
  fi
}

cmd_admin() {
  [ $# -gt 0 ] || { echo "usage: deploy.sh admin --email you@example.com [--revoke] | --list" >&2; exit 2; }
  local args=""
  local a
  for a in "$@"; do args+=" $(printf %q "$a")"; done
  log "grant_admin.py$args"
  vm_ssh "cd /opt/stylist && docker compose exec -T api python scripts/grant_admin.py$args"
}

cmd_status() { vm_ssh "cd /opt/stylist && docker compose ps --format 'table {{.Service}}\t{{.Status}}'"; }

cmd_logs() {
  local svc=""
  [ -n "${1:-}" ] && svc=" $(printf %q "$1")"
  vm_ssh "cd /opt/stylist && docker compose logs --tail=100$svc"
}

case "${1:-}" in
  all)    cmd_build; cmd_ml; cmd_vm ;;
  build)  cmd_build ;;
  ml)     cmd_ml ;;
  vm)     cmd_vm ;;
  models) cmd_models ;;
  admin)  shift; cmd_admin "$@" ;;
  status) cmd_status ;;
  logs)   shift; cmd_logs "${1:-}" ;;
  *) sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'; exit 2 ;;
esac
