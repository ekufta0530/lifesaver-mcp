# sqlite_extract — Mason store's KPI ingest

The Mason store's history (June 2015 on) arrives as a full LifeSaver SQLite
extract in `gs://mcps-507817-lifesaver-data/lifesaver.sqlite`, not via the
cloud report. `python -m sqlite_extract.importer lifesaver.sqlite mason.db`
rebuilds a `core.store.Warehouse` with the same `visits` / `customer_lifecycle` /
`kpi_monthly` tables from it, using the same `build_visits` + `compute_kpis`.
Differences: customers are LifeSaver customer numbers (no name matching),
revenue is the ticket subtotal (pre-tax, net of discount), and "today" is the
extract's latest order date. [`dashboard/publish.sh`](../dashboard/publish.sh) runs it on every publish and
renders `mason.html`, the dashboard's second tab.

No lsscloud.com login and no raw/line-item layers: the extract *is* the raw
layer, and the destination is rebuilt from scratch on every run.

Tests: [`../tests/sqlite_extract/`](../tests/sqlite_extract/).
