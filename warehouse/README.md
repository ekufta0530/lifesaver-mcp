# warehouse — retention KPI pipeline (Phase 3)

Accumulates the Work Order List history into a local SQLite warehouse and computes
the five retention KPIs. Design and rationale: [`../dashboard/DESIGN.md`](../dashboard/DESIGN.md).

## Layers

```
raw files (warehouse_raw/*.csv.gz)   every pull, verbatim, immutable — the backup
  -> line_items                      deduped current truth, upserted on a stable key
       + line_item_history           append-only record of every changed field
  -> customers / customer_aliases    resolved identities (exact + normalized match)
  -> visits / customer_lifecycle     "a purchase == a customer on a date"
  -> kpi_monthly                     one row per (metric, month, calc_version)
```

Everything in `identity.py`, `visits.py`, `kpis.py` is a pure function. `store.py`
is the only module that knows it is SQLite.

## CLI

```
python -m warehouse.job status                 # counts + coverage + checkpoint
python -m warehouse.job backfill                # full history, month by month, resumable
python -m warehouse.job backfill --from 2023-01 --to 2023-06
python -m warehouse.job backfill --restart      # ignore the resume checkpoint
python -m warehouse.job sync                    # routine trailing-window pull + refresh
python -m warehouse.job resolve                 # re-run identity resolution only
python -m warehouse.job rebuild                 # rebuild visits + lifecycle only
python -m warehouse.job snapshot --all          # (re)compute every KPI month
python -m warehouse.job snapshot --month 2026-08
python -m warehouse.job kpis --metric first_to_second_rate
python -m warehouse.job review                  # aliases flagged for a human (none yet)
```

`backfill` and `sync` hit lsscloud.com and hold the single shared LifeSaver
session — do not run them while the MCP server might also be pulling.

Config is env vars (see `../.env.example`), all optional.

## First run

```
export LIFESAVER_USERNAME=... LIFESAVER_PASSWORD=...
python -m warehouse.job backfill        # takes a while: ~36 pulls, 3s apart
python -m warehouse.job kpis            # eyeball against the baselines
sqlite3 warehouse.db 'SELECT DISTINCT current_status FROM line_items'   # sanity-check the status filter
```

Then back up: copy `warehouse.db` and sync `warehouse_raw/` to a versioned GCS
bucket.

## Metrics (`kpis.py`)

| metric | meaning |
|---|---|
| `first_to_second_rate` | of the first-visit cohort that matured 12 months before the report month, the share who came back within 365 days |
| `repeat_revenue_share` | revenue from non-first visits ÷ total revenue, trailing 12 months |
| `median_days_to_second` | median gap first→second visit, for that cohort's returners |
| `active_customers_ttm` | distinct customers with a visit in the trailing 12 months |
| `reactivation_rate` | of customers 12+ months lapsed at the month's start, the share who bought that month |

Bump `CALC_VERSION` in `models.py` when a definition changes — old and new series
then coexist in `kpi_monthly` instead of overwriting.

## Tests

`../tests/test_warehouse_*.py` — offline, no network, no GCP. Run with the rest:
`.venv/bin/python -m pytest -q`.
