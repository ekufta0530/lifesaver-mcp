#!/usr/bin/env bash
# Daily refresh: pull the warehouse from GCS, sync new data, rebuild the
# dashboard, push everything back. This is exactly what the scheduled Cloud Run
# Job runs; it also works locally (needs gcloud auth + LIFESAVER_USERNAME /
# LIFESAVER_PASSWORD in the environment).
#
#   WAREHOUSE_BUCKET=lifesaver-kpi-warehouse \
#   SITE_BUCKET=lifesaver-kpi-dashboard-303f74 \
#   dashboard/refresh.sh
#
# Closed monthly/quarterly reports never change (the warehouse freezes each KPI
# row once its month ends); only the live current month + quarter move.

set -euo pipefail

: "${WAREHOUSE_BUCKET:?set WAREHOUSE_BUCKET}"
: "${SITE_BUCKET:?set SITE_BUCKET}"
WORKDIR="${WORKDIR:-$(pwd)}"
cd "$WORKDIR"

echo "==> pull warehouse from gs://$WAREHOUSE_BUCKET"
gcloud storage cp "gs://$WAREHOUSE_BUCKET/warehouse.db" warehouse.db
mkdir -p warehouse_raw
gcloud storage rsync -r "gs://$WAREHOUSE_BUCKET/warehouse_raw" warehouse_raw

echo "==> sync new work-order data + recompute KPIs"
python -m warehouse.job sync
python -m warehouse.job status

echo "==> rebuild + publish dashboard"
dashboard/publish.sh

echo "==> push warehouse back to gs://$WAREHOUSE_BUCKET"
gcloud storage cp warehouse.db "gs://$WAREHOUSE_BUCKET/warehouse.db"
gcloud storage rsync -r warehouse_raw "gs://$WAREHOUSE_BUCKET/warehouse_raw"

echo "==> done: https://storage.googleapis.com/$SITE_BUCKET/index.html"
