#!/usr/bin/env bash
# Rebuild the dashboard from the warehouse.db in $PWD and publish it to the
# static-site bucket. This is a *code* refresh -- it does NOT log into LifeSaver
# and does NOT recompute KPIs; it just re-renders index.html from whatever data
# is already in warehouse.db. Both dashboard/refresh.sh and CI call it.
#
# It also rebuilds the Mason tab (mason.html) from that store's LifeSaver
# SQLite extract in GCS ($MASON_SOURCE). If the extract can't be read, the main
# store still publishes and the Mason page already in the bucket is left as is.
#
#   SITE_BUCKET=lifesaver-kpi-dashboard-303f74 dashboard/publish.sh
#
# Needs: $SITE_BUCKET set, warehouse.db already present in $PWD, gcloud auth.

set -euo pipefail

: "${SITE_BUCKET:?set SITE_BUCKET}"
MASON_SOURCE="${MASON_SOURCE:-gs://mcps-507817-lifesaver-data/lifesaver.sqlite}"

upload_html() {
  gcloud storage cp "$1" "gs://$SITE_BUCKET/$2" \
    --content-type="text/html; charset=utf-8" --cache-control="public, max-age=300"
}

echo "==> rebuild dashboard"
python -m dashboard.build --json dashboard/data.json

echo "==> publish site to gs://$SITE_BUCKET"
upload_html dashboard/index.html index.html
gcloud storage cp dashboard/data.json "gs://$SITE_BUCKET/data.json" \
  --content-type="application/json; charset=utf-8" --cache-control="public, max-age=300"

echo "==> rebuild Mason tab from $MASON_SOURCE"
if gcloud storage cp "$MASON_SOURCE" mason.sqlite; then
  python -m warehouse.sqlite_import mason.sqlite mason.db
  python -m dashboard.build --store mason --db mason.db --out dashboard/mason.html
  upload_html dashboard/mason.html mason.html
else
  echo "::warning::could not read $MASON_SOURCE -- Mason tab not refreshed"
fi

echo "==> done: https://storage.googleapis.com/$SITE_BUCKET/index.html"
