#!/usr/bin/env bash
# Nightly logical dump of the stylist database to the backups bucket (cron,
# installed by update.sh). Dumped as the OWNER: stylist_app is NOBYPASSRLS, so
# a dump taken as the app role would contain no rows at all (scripts/backup.py
# explains). The bucket's lifecycle rule deletes dumps after 30 days.
set -euo pipefail
cd /opt/stylist
# shellcheck source=/dev/null
source /opt/stylist/app.env
stamp="$(date -u +%Y-%m-%dT%H%M%SZ)"
docker compose exec -T postgres pg_dump -Fc -U stylist_owner stylist |
  gcloud storage cp - "gs://$BACKUP_BUCKET/postgres/stylist-$stamp.dump" --quiet
echo "$(date -u) backup ok: stylist-$stamp.dump"
