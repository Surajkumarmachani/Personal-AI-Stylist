#!/usr/bin/env bash
# One-time Google Cloud setup for the budget plan (~$35/month): everything
# deploy.sh assumes already exists.
#
# Safe to re-run. Each step checks for its resource first and skips it if
# present, so a run that died halfway is resumed by running it again.
#
# Usage: infra/gcp/bootstrap.sh          (~5 minutes)

source "$(dirname "$0")/lib.sh"

log "Enabling APIs"
gcloud services enable \
  compute.googleapis.com run.googleapis.com artifactregistry.googleapis.com \
  cloudbuild.googleapis.com secretmanager.googleapis.com storage.googleapis.com \
  iam.googleapis.com cloudresourcemanager.googleapis.com \
  iamcredentials.googleapis.com sts.googleapis.com logging.googleapis.com

log "Artifact Registry"
gcloud artifacts repositories describe "$AR_REPO" --location="$REGION" >/dev/null 2>&1 ||
  gcloud artifacts repositories create "$AR_REPO" --repository-format=docker --location="$REGION"
# Keep the registry from growing forever (each deploy adds ~2 GB of images):
# delete untagged images and keep the 5 newest versions of each.
cat > /tmp/stylist-ar-cleanup.json <<'JSON'
[{"name": "keep-recent", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 5}},
 {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"olderThan": "1d"}}]
JSON
gcloud artifacts repositories set-cleanup-policies "$AR_REPO" --location="$REGION" \
  --policy=/tmp/stylist-ar-cleanup.json --no-dry-run >/dev/null
rm -f /tmp/stylist-ar-cleanup.json
# Newer projects run Cloud Build as the Compute default service account.
gcloud artifacts repositories add-iam-policy-binding "$AR_REPO" --location="$REGION" \
  --member="serviceAccount:$(project_number)-compute@developer.gserviceaccount.com" \
  --role=roles/artifactregistry.writer >/dev/null

log "Service accounts"
# stylist-runtime: the VM and the ml service run as this.
# stylist-storage: owns ONLY the uploads bucket, and exists to hold the HMAC
#   key the S3 client signs with, so a leaked HMAC key reaches user uploads
#   and nothing else.
for sa in stylist-runtime stylist-storage; do
  gcloud iam service-accounts describe "$sa@$PROJECT_ID.iam.gserviceaccount.com" >/dev/null 2>&1 ||
    gcloud iam service-accounts create "$sa"
done
# run.invoker: the VM calls the PRIVATE ml service with this identity's token.
for role in roles/secretmanager.secretAccessor roles/logging.logWriter \
            roles/monitoring.metricWriter roles/artifactregistry.reader roles/run.invoker; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$RUNTIME_SA" --role="$role" --condition=None >/dev/null
done

log "Generated secrets"
for s in jwt-secret db-owner-password db-app-password; do
  secret_exists "$s" || put_secret "$s" "$(random_hex)"
done
secret_exists litellm-master-key || put_secret litellm-master-key "sk-$(random_hex)"

log "Cloud Storage buckets"
for b in "$UPLOAD_BUCKET" "$MODELS_BUCKET" "$BACKUP_BUCKET" "$DEPLOY_BUCKET"; do
  gcloud storage buckets describe "gs://$b" >/dev/null 2>&1 ||
    gcloud storage buckets create "gs://$b" --location="$REGION" \
      --uniform-bucket-level-access --public-access-prevention
done
# The browser uploads straight to the bucket (presigned POST) and loads images
# from it (presigned GET), so the bucket itself must allow the Vercel origin.
cors_file="$(mktemp)"
origins_json="$(printf '%s' "$WEB_ORIGINS" | tr ',' '\n' | sed 's/^ *//;s/ *$//;/^$/d;s/.*/"&"/' | paste -sd, -)"
cat > "$cors_file" <<JSON
[{"origin": [$origins_json],
  "method": ["GET", "HEAD", "POST", "PUT"],
  "responseHeader": ["Content-Type", "ETag"],
  "maxAgeSeconds": 3600}]
JSON
gcloud storage buckets update "gs://$UPLOAD_BUCKET" --cors-file="$cors_file"
rm -f "$cors_file"
# Backups older than 30 days are deleted automatically.
lc_file="$(mktemp)"
echo '{"rule": [{"action": {"type": "Delete"}, "condition": {"age": 30}}]}' > "$lc_file"
gcloud storage buckets update "gs://$BACKUP_BUCKET" --lifecycle-file="$lc_file"
rm -f "$lc_file"

gcloud storage buckets add-iam-policy-binding "gs://$UPLOAD_BUCKET" \
  --member="serviceAccount:$STORAGE_SA" --role=roles/storage.objectAdmin >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$MODELS_BUCKET" \
  --member="serviceAccount:$RUNTIME_SA" --role=roles/storage.objectViewer >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$BACKUP_BUCKET" \
  --member="serviceAccount:$RUNTIME_SA" --role=roles/storage.objectAdmin >/dev/null
gcloud storage buckets add-iam-policy-binding "gs://$DEPLOY_BUCKET" \
  --member="serviceAccount:$RUNTIME_SA" --role=roles/storage.objectViewer >/dev/null

log "HMAC key for the S3-compatible client"
if ! secret_exists s3-access-key; then
  read -r access_id hmac_secret < <(gcloud storage hmac create "$STORAGE_SA" \
    --format='value(metadata.accessId,secret)')
  put_secret s3-access-key "$access_id"
  put_secret s3-secret-key "$hmac_secret"
fi

log "Network: static IP + HTTPS firewall rule"
gcloud compute addresses describe "$VM_IP_NAME" --region="$REGION" >/dev/null 2>&1 ||
  gcloud compute addresses create "$VM_IP_NAME" --region="$REGION"
gcloud compute firewall-rules describe stylist-web >/dev/null 2>&1 ||
  gcloud compute firewall-rules create stylist-web --network=default \
    --allow=tcp:80,tcp:443,udp:443 --target-tags=stylist-web --source-ranges=0.0.0.0/0

log "Daily disk snapshots (kept 7 days)"
gcloud compute resource-policies describe stylist-daily --region="$REGION" >/dev/null 2>&1 ||
  gcloud compute resource-policies create snapshot-schedule stylist-daily --region="$REGION" \
    --daily-schedule --start-time=21:00 --max-retention-days=7 \
    --on-source-disk-delete=keep-auto-snapshots

log "VM ($VM_MACHINE in $ZONE)"
if ! gcloud compute instances describe "$VM_NAME" --zone="$ZONE" >/dev/null 2>&1; then
  gcloud compute instances create "$VM_NAME" --zone="$ZONE" \
    --machine-type="$VM_MACHINE" \
    --image-family=debian-12 --image-project=debian-cloud \
    --boot-disk-size=30GB --boot-disk-type=pd-balanced \
    --service-account="$RUNTIME_SA" --scopes=cloud-platform \
    --address="$(vm_ip)" --tags=stylist-web \
    --shielded-secure-boot --shielded-vtpm --shielded-integrity-monitoring \
    --metadata=enable-oslogin=TRUE \
    --metadata-from-file=startup-script="$REPO_ROOT/infra/vm/vm-setup.sh"
  gcloud compute disks add-resource-policies "$VM_NAME" --zone="$ZONE" \
    --resource-policies=stylist-daily
fi

if [ -n "${GITHUB_REPO:-}" ]; then
  log "GitHub Actions deploy identity (Workload Identity Federation)"
  # GitHub's OIDC token is exchanged for short-lived credentials on
  # stylist-deployer: no JSON key exists, and only $GITHUB_REPO can use it.
  gcloud iam service-accounts describe "$DEPLOYER_SA" >/dev/null 2>&1 ||
    gcloud iam service-accounts create stylist-deployer
  # osAdminLogin + instanceAdmin: `gcloud compute ssh` to run update.sh.
  for role in roles/viewer roles/run.admin roles/cloudbuild.builds.editor \
              roles/artifactregistry.writer roles/storage.objectAdmin \
              roles/serviceusage.serviceUsageConsumer roles/compute.osAdminLogin \
              roles/compute.instanceAdmin.v1; do
    gcloud projects add-iam-policy-binding "$PROJECT_ID" \
      --member="serviceAccount:$DEPLOYER_SA" --role="$role" --condition=None >/dev/null
  done
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
(e.g. "production") with these VARIABLES:

  GCP_WIF_PROVIDER = $pool_id/providers/github-oidc
  GCP_DEPLOYER_SA  = $DEPLOYER_SA
  GCP_CONFIG_ENV   = <the full contents of $(basename "$GCP_CONFIG")>
EOF
fi

cat <<EOF

Bootstrap complete. The VM installs Docker on its first boot (~2 minutes).

API address: https://$(api_host)
  → put this in Vercel as NEXT_PUBLIC_API_BASE

Third-party keys go in Secret Manager (paste the value, then Ctrl-D), e.g.:
  gcloud secrets create openrouter-api-key --project=$PROJECT_ID --data-file=-

Then: infra/gcp/deploy.sh models && infra/gcp/deploy.sh all
EOF
