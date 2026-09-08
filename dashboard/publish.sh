#!/usr/bin/env bash
# Rebuild the dashboard from the warehouse.db in $PWD and publish it to the
# static-site bucket. This is a *code* refresh -- it does NOT log into LifeSaver
# and does NOT recompute KPIs; it just re-renders index.html from whatever data
# is already in warehouse.db. Both dashboard/refresh.sh and CI call it.
#
#   SITE_BUCKET=lifesaver-kpi-dashboard-303f74 dashboard/publish.sh
#
# Needs: $SITE_BUCKET set, warehouse.db already present in $PWD, gcloud auth.

set -euo pipefail

: "${SITE_BUCKET:?set SITE_BUCKET}"

echo "==> rebuild dashboard"
python -m dashboard.build --json dashboard/data.json

echo "==> publish site to gs://$SITE_BUCKET"
gcloud storage cp dashboard/index.html "gs://$SITE_BUCKET/index.html" \
  --content-type="text/html; charset=utf-8" --cache-control="public, max-age=300"
gcloud storage cp dashboard/data.json "gs://$SITE_BUCKET/data.json" \
  --content-type="application/json; charset=utf-8" --cache-control="public, max-age=300"

echo "==> done: https://storage.googleapis.com/$SITE_BUCKET/index.html"
