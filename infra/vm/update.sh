#!/usr/bin/env bash
# Runs ON the VM, as root, on every deploy (infra/gcp/deploy.sh vm):
#   1. fetch the bundle (compose file, Caddyfile, app.env, scripts) from GCS
#   2. render /opt/stylist/.env = app.env + secrets from Secret Manager
#   3. pull images at $TAG, migrate, then start everything
#
# Usage: update.sh gs://<deploy-bucket>/bundle
set -euo pipefail
BUNDLE="${1:?usage: update.sh gs://bucket/bundle}"
cd /opt/stylist

echo "==> waiting for Docker (first boot installs it)"
for _ in $(seq 1 60); do
  [ -f /opt/stylist/.vm-ready ] && docker compose version >/dev/null 2>&1 && break
  sleep 5
done
docker compose version >/dev/null

echo "==> fetching bundle"
gcloud storage cp "$BUNDLE/*" /opt/stylist/ --quiet
# shellcheck source=/dev/null
source /opt/stylist/app.env

echo "==> rendering .env from Secret Manager"
secret() { gcloud secrets versions access latest --secret="$1" 2>/dev/null || true; }
umask 077
owner_pw="$(secret db-owner-password)"
app_pw="$(secret db-app-password)"
[ -n "$owner_pw" ] && [ -n "$app_pw" ] || { echo "db passwords missing in Secret Manager" >&2; exit 1; }
{
  cat /opt/stylist/app.env
  echo "DB_OWNER_PASSWORD=$owner_pw"
  echo "APP_DB_PASSWORD=$app_pw"
  echo "DATABASE_URL=postgresql+asyncpg://stylist_app:$app_pw@postgres:5432/stylist"
  echo "MIGRATION_DATABASE_URL=postgresql://stylist_owner:$owner_pw@postgres:5432/stylist"
  echo "LITELLM_DATABASE_URL=postgresql://stylist_owner:$owner_pw@postgres:5432/litellm"
  # name-in-app=secret-name. Optional ones are simply absent when unset.
  for pair in JWT_SECRET=jwt-secret LITELLM_MASTER_KEY=litellm-master-key \
              S3_ACCESS_KEY=s3-access-key S3_SECRET_KEY=s3-secret-key \
              OPENROUTER_API_KEY=openrouter-api-key GEMINI_API_KEY=gemini-api-key \
              ANTHROPIC_API_KEY=anthropic-api-key GROQ_API_KEY=groq-api-key \
              OPENAI_API_KEY=openai-api-key VTON_API_TOKEN=vton-api-token \
              GOOGLE_CLIENT_SECRET=google-client-secret \
              GOOGLE_CALENDAR_API_KEY=google-calendar-api-key \
              SHOP_POSTBACK_SECRET=shop-postback-secret \
              INBOUND_EMAIL_SECRET=inbound-email-secret \
              LANGFUSE_PUBLIC_KEY=langfuse-public-key LANGFUSE_SECRET_KEY=langfuse-secret-key; do
    v="$(secret "${pair#*=}")"
    [ -n "$v" ] && echo "${pair%%=*}=$v"
  done
} > /opt/stylist/.env.new
mv /opt/stylist/.env.new /opt/stylist/.env

fb="$(secret firebase-credentials)"
if [ -n "$fb" ]; then
  printf '%s' "$fb" > /opt/stylist/secrets/firebase.json
  echo "FIREBASE_CREDENTIALS_FILE=/app/secrets/firebase.json" >> /opt/stylist/.env
fi
# The worker runs as uid 10001 (Dockerfile) and must be able to read it.
chown -R 10001 /opt/stylist/secrets
umask 022

echo "==> pulling images at $TAG"
gcloud auth print-access-token |
  docker login -u oauth2accesstoken --password-stdin "https://${REGISTRY%%/*}" >/dev/null
docker compose pull --quiet

echo "==> data services"
docker compose up -d --wait postgres redis-queue redis-cache

echo "==> migrations"
docker compose run --rm litellm-db-init
docker compose run --rm migrate

echo "==> app"
docker compose up -d --remove-orphans
docker image prune -af --filter "until=168h" >/dev/null || true

# Nightly Postgres dump to the backups bucket, 21:30 UTC = 03:00 IST.
chmod +x /opt/stylist/backup.sh
echo "30 21 * * * root /opt/stylist/backup.sh >> /var/log/stylist-backup.log 2>&1" \
  > /etc/cron.d/stylist-backup

echo "==> waiting for the api"
for _ in $(seq 1 30); do
  if docker compose exec -T api python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/healthz')" 2>/dev/null; then
    docker compose ps --format 'table {{.Service}}\t{{.Status}}'
    echo "==> deployed $TAG"
    exit 0
  fi
  sleep 4
done
docker compose ps
docker compose logs --tail=40 api
echo "api did not become healthy" >&2
exit 1
