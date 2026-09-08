# Frame Shop Performance Dashboard — Backend Design

Status: **built** (backend + dashboard, 2026-09-07). Phase 3 of the project;
Phases 1–2 (report puller + MCP server) are in `spec.md` and deployed. The
pipeline and dashboard run locally and the dashboard auto-publishes on push to
`main`; the remaining open items are the scheduled data pull and production
persistence (§12 #6, §15). Per-section status is inline below ("Built:",
strikethroughs) and summarised in §13–14.

> Renamed 2026-09-08 from "Retention KPI Dashboard" — the page now leads with
> business performance (revenue, a monthly year-over-year table, average ticket)
> and keeps customer retention as one section below. Page title: **Frame Shop
> Performance**.

## 1. Purpose

Track five customer-retention KPIs for the framing store, monthly, with a
quarterly trend review:

| # | KPI | Definition (precise form in §7) | Baseline |
|---|---|---|---|
| 1 | First-to-second-purchase rate (12-mo window) | of a first-purchase cohort, % who came back within 365 days | 19.2% |
| 2 | Repeat share of revenue | repeat-visit revenue ÷ total revenue, trailing 12 mo | 27.3% |
| 3 | Median days to second purchase | median gap, first → second visit, for those who returned | 97 days |
| 4 | Active customer base (TTM) | distinct customers with a visit in the last 12 months | ~365 |
| 5 | Reactivation rate | % of 12-mo-lapsed customers who bought in the period | not tracked yet |

## 2. Why this needs a warehouse (not just caching)

Every KPI is computed **per customer across their whole purchase history** — first
and second visit dates, "last visit ≥ 12 months ago", distinct customers over a
trailing year. The only data source (`get_work_order_list_report(start, end)`)
returns a **date-range slice of line items**. You cannot compute any of these five
numbers from slices without first assembling the full history yourself.

So a local store is required to compute the KPIs at all — and that same store is
also the answer to the 36-month source-retention problem. One store solves both.

## 3. Source system — recap and constraints

- **One tool, one shape:** work-order line items whose `orderDate` is in
  `[start, end]`. Columns: `invoiceNumber, workOrderNumber, customer,
  lineItemNumber, description, currentStatus, retail, discount, orderDate,
  dateDue`. (See `lifesaver/parser.py`, `tests/fixtures/export_sample.csv`.)
- **One LifeSaver session per user**, license-enforced. The MCP server serialises
  pulls behind a lock and must run as exactly one instance. Any second component
  that logs in fights for the same session.
- **~36-month rolling retention** at the source. Anything not captured within a
  rolling 36-month window is gone permanently. → the backfill is urgent.
- **Orders mutate after creation.** `currentStatus` moves (`OnOrder` → `Completed`
  → picked up; also `Void`), discounts get edited, line items get added. A pull of
  "August" run in September will not equal the same pull run in November. → sync
  must **re-pull a trailing window and upsert**, not append-only.
- **Cold start + login** on every MCP call today (~3–10 s). The warehouse removes
  this from the read path.

## 4. Architecture

```
                 ┌─────────────────────────────┐
  lsscloud.com ──│  sync worker (Cloud Run Job) │──┐   owns the ONE LifeSaver session
   (SSRS)        │  Cloud Scheduler: daily +    │  │
                 │  monthly                     │  │
                 └─────────────────────────────┘  │
                                                  ▼
   ┌───────────────┐   ┌────────────────┐   ┌──────────────────┐
   │ Layer 1: RAW  │──▶│ Layer 2:       │──▶│ Layer 3: DERIVED │
   │ GCS, JSONL,   │   │ WAREHOUSE      │   │ visits,          │
   │ immutable,    │   │ line_items     │   │ customer_lifecycle,│
   │ versioned     │   │ (+ history)    │   │ kpi_monthly      │
   └───────────────┘   │ customers,     │   └──────────────────┘
                       │ customer_aliases│           │
                       └────────────────┘            ▼
                                          ┌──────────────────────┐
                              MCP server ─│ read-only: KPI series,│
                              / dashboard │ customer drilldown    │
                                          └──────────────────────┘
```

- **Layer 1 — raw landing.** Every pull's exported rows written to GCS as
  newline-delimited JSON, one object per CSV row plus a `pull_id`. Never mutated.
  Bucket has object versioning + a retention policy. Everything downstream is
  rebuildable from this; this is the artifact we protect hardest.
- **Layer 2 — warehouse.** Deduped current truth. `line_items` upserted on a
  natural key (§6, **open question**), plus an append-only `line_item_history`
  capturing every observed change (status transitions, price edits).
- **Layer 3 — derived.** `visits` (the "purchase" grain, §6), `customer_lifecycle`
  (one row per customer), and `kpi_monthly` — one row per (metric, month,
  calc_version), **frozen once the month/cohort can no longer change**. The
  dashboard reads only Layer 3.

## 5. Storage choice

**Built (v1): SQLite** for Layers 2–3, **gzipped CSV files** for Layer 1
(`warehouse_raw_dir`, sync that folder to a versioned GCS bucket).

Rationale for going SQLite-first rather than straight to BigQuery:

- The whole dataset is tens of thousands of rows, ever, with exactly one writer
  (the sync job).
- **The KPI math runs in Python, not warehouse SQL** (`warehouse/kpis.py`), so the
  engine only has to store rows and hand them back — SQLite does that with zero
  cost, zero ops, and a single file that is trivial to back up and to develop
  and test against locally.
- Get the pipeline correct and validate the KPIs against the published baselines
  locally *before* taking on any cloud infrastructure.

BigQuery stays the migration target if a BI tool (Looker Studio) or data volume
calls for it. The `Warehouse` class in `warehouse/store.py` is the *only* thing
that would change — every transform above it is pure and storage-agnostic.

| Option | When it'd win | Cost floor |
|---|---|---|
| **SQLite (built)** | tiny data, single writer, Python-side analytics, local-first | $0 |
| BigQuery + GCS | a live BI tool on the warehouse; 10×+ the data | ~$0 at this volume |
| Cloud SQL Postgres | a custom dashboard app wanting a conventional DB + PITR | ~$10–25/mo |

### Production persistence — still open (§12 #6)

Cloud Run has ephemeral disk. Before the routine sync and the MCP read tools can
run in the cloud, the SQLite file needs a durable home: a GCS-backed volume mount
on the sync job + a copy-out after each run, or ship a read replica in the MCP
image, or move to Cloud SQL. Decide after the local backfill validates the numbers.

## 6. Grain definitions

### "A purchase" = a visit

**A visit is `(customer_id, order_date)`.** If a customer drops off three frames
in one trip and the store writes three work orders (three invoices) that day, that
is **one** purchase. Confirmed with the user. (`warehouse/visits.py`.)

- `visits.revenue = Σ(retail − discount)` over that customer's line items on that
  date, **excluding non-sale statuses**. The exclusion set
  (`DEFAULT_NON_SALE_STATUSES`) is `void, cancelled, canceled, quote, estimate` —
  conservative: drop only what is definitely not a sale, keep everything else.
  Override with `WAREHOUSE_NON_SALE_STATUSES`. Full enum still open (§12 #2).
- Consecutive-day drop-offs for one project count as two visits. Accepted
  simplification.
- Commercial customers (`"Nestle Purina Petcare - Deion Taylor"`): default is
  **not** to split — the whole normalized string is the identity, so two contacts
  at one company are two customers (the conservative, no-over-merge choice). Flip
  with `WAREHOUSE_SPLIT_COMMERCIAL_CONTACT=true`. Needs user confirmation (§12 #4).

### Revenue

`revenue = retail − discount`, always. Confirmed with the user.

### Natural key for `line_items` — `(work_order_id, line_item_number)`

**Confirmed against the full backfill (1086 rows).** The three fields:

- **`work_order_number`** — integer part is a **globally unique, per-line-item**
  sequence (`work_order_id`; 1086 rows → 1086 distinct). Each framed piece gets its
  own number. The `.N` suffix is a per-order revision counter — stored as an
  attribute, never in the key.
- **`invoice_number`** — the **order** (one customer transaction); repeats across
  the line items of a multi-line order. Present on every row, from `1` up.
- **`line_item_number`** — position within the order (usually `1`; a handful of
  rows carry `2`).

`work_order_id` alone is unique, so `(work_order_id, line_item_number)` is
bulletproof. `invoice_number` is kept as the order-grouping attribute.
(`warehouse/models.py::natural_key`.)

## 7. KPI definitions (precise)

Let a *cohort month* `C` = the calendar month of a customer's **first-ever visit**.
Let `R` = the reporting month.

1. **First-to-second-purchase rate.** For the cohort `C = R − 12 months` (the
   cohort whose 12-month window just closed):
   `value = count(customers in C with a 2nd visit within 365 days of visit 1)
            ÷ count(customers in C)`.
   One clean number per reporting month; never revised once emitted. A blended
   variant (cohorts `R−24 … R−12`) gives a larger sample for the quarterly review.

2. **Repeat share of revenue.** Over the trailing 12 months ending at `R`:
   `value = Σ revenue of visits where visit_rank ≥ 2  ÷  Σ revenue of all visits`.

3. **Median days to second purchase.** For the same matured cohort as KPI 1,
   restricted to customers who *did* return within 365 days:
   `value = median(second_visit_date − first_visit_date)`.
   Tying it to a fixed cohort avoids the survivorship drift you'd get from "all
   returners so far".

4. **Active customer base (TTM).** `value = count(distinct customer_id with ≥ 1
   visit in the 12 months ending at R)`.

5. **Reactivation rate.** For reporting month `R`:
   - *lapsed pool at R start* = customers with ≥ 1 lifetime visit and **no** visit
     in the 365 days before `R` starts.
   - *reactivated in R* = of that pool, those with a visit during `R`.
   - `value = reactivated ÷ lapsed pool`.

All five are stored in `kpi_monthly` with `numerator`, `denominator`,
`cohort_month` (where relevant), `calc_version`, and `is_final`.

**Horizon caveat — resolved.** The backfill shows `invoice_number` starting at `1`
(a `"test1"` order dated 2024-05-07): **the store began on LifeSaver in May 2024,
so this warehouse holds the complete customer history.** No earlier data exists to
be missing, so the cohort KPIs are unbiased. (First ~3 months, May–Jul 2024, are
2 orders/month of setup/testing; real volume starts Aug 2024.)

## 8. Customer identity resolution

No customer ID exists in the source. Approach (agreed direction with user):

- **Always keep `customer_raw`** — the exact source string, on every line item,
  never mutated.
- **`normalized_name`** — lowercased, whitespace collapsed, trailing punctuation
  stripped, commercial contact suffix (`" - <contact>"`) split off.
- **Synthetic `customer_id`** — stable, assigned via a resolution pipeline whose
  every decision is recorded in `customer_aliases` (so it is deterministic and
  correctable, and re-runs never lose a manual fix):

  1. **exact** — `customer_raw` already in `customer_aliases` → reuse its id.
  2. **normalized** — `normalized_name` matches an existing customer → link,
     `match_method='normalized'`.
  3. **fuzzy (assisted, not automatic merge)** — token-sort / Jaro-Winkler against
     existing canonical names:
     - score ≥ 0.92 → auto-link, `match_method='fuzzy'`
     - 0.85 ≤ score < 0.92 → **new id but `needs_review=true`** (queued for a human)
     - score < 0.85 → new id, `match_method='new'`
  4. **manual** overrides (`match_method='manual'`) are sticky — never
     re-evaluated.

- Conservative thresholds on purpose: over-merging (`Dave Smith` ↔ `Dan Smith`)
  corrupts every KPI silently, under-merging just costs a review click. Typo cases
  we've already seen: `"Miike Woodland"`.
- A small review UI (or even a reviewed CSV) drains the `needs_review` queue.
- Periodic audit: list `customer_id`s whose members' names disagree beyond a
  threshold.

**Built:** steps 1–2 only (exact + normalized), in `warehouse/identity.py`. The
`customer_id` is a deterministic hash of the match key, so re-running never churns
ids. `customer_aliases.needs_review` / `match_score` columns exist and
`job.py review` reads them, but nothing sets `needs_review` yet — that is the
fuzzy layer (step 12). A `manual` alias is never re-evaluated.

## 9. Sync jobs

All run from `warehouse/job.py` (later: a Cloud Run Job on Cloud Scheduler). The
scrape steps hold the single shared LifeSaver session — never run them while the
MCP server might also be pulling (model A below).

- **`job.py backfill`** — one-time, resumable. Iterates month-by-month, oldest
  first, from the 36-month floor to now: pull → raw file → upsert `line_items` →
  checkpoint. `WAREHOUSE_BACKFILL_DELAY_SECONDS` (default 3) between months. A
  crash/interrupt resumes at the checkpoint; `--restart` ignores it. Ends by
  rebuilding every derived layer and the full KPI history. **Run this early —
  history is rolling off the source.**
- **`job.py sync`** — the routine incremental. Re-pulls a trailing
  `WAREHOUSE_SYNC_WINDOW_DAYS` window (default 120), upserts, re-resolves
  identities, rebuilds visits, and recomputes the last 6 report months of
  snapshots (frozen `is_final` rows are left alone). 120 days is a guess at "how
  long an order stays mutable" — tune once real churn is visible.
- Snapshots freeze automatically: `is_final` is set once `today` is past the end
  of the report month, and the store refuses to overwrite a frozen row.

### Session / concurrency model

The one-session constraint forces a single scrape owner. Options:

- **A (recommended):** sync worker is the only thing that logs in. MCP server
  becomes pure warehouse reads. Freshness = "as of last sync (< 24 h)", which is
  fine for monthly KPIs. Remove the live-scrape tool, or keep it only in local
  stdio mode.
- **B:** keep the MCP live tool, and have it and the worker share one lock /
  instance. More moving parts, no real benefit here.

Go with A.

## 10. Backup strategy

- **Raw layer is the thing that must never be lost.** Layers 2–3 rebuild from it
  completely (re-run `sync_range`'s upsert over every raw file, then resolve /
  rebuild / snapshot). Every pull is `warehouse_raw/<start>_<end>.<pull_id>.csv.gz`
  plus a `raw_pulls` row (sha256, counts).
- **Off-box copy:** sync `warehouse_raw/` to a **versioned GCS bucket** with a
  retention policy / bucket lock, so a bug or bad deploy cannot erase it.
- **The SQLite file:** copy `warehouse.db` to GCS after each sync run (it is a few
  MB). Cheap point-in-time history; not the source of truth (raw is).
- **One-time full-history export:** if LifeSaver support can produce a CSV dump
  older than the API's 36 months, drop it in `warehouse_raw/` and upsert it — the
  ideal seed for the cohort KPIs (§12 #3). Ask now.

## 11. Cost

Local-first v1: **$0.** When it moves to the cloud:

- GCS (raw files + db copies): pennies at this volume.
- Cloud Scheduler: free tier.
- Cloud Run Job: scales to zero between runs.
- Cloud SQL, only if chosen for production persistence, is the one option with a
  real monthly floor (~$10–25).

## 12. Open questions

1. ~~Natural key~~ — **closed.** `work_order_id` is globally unique across all
   1086 backfilled rows (§6).

2. ~~`currentStatus` values~~ — **closed.** 28 months has only `OnOrder` (1078),
   `Void` (7), `InProgress` (1); no fulfilment lifecycle — an order stays
   `OnOrder`. Confirmed with the user: `OnOrder` == a completed, delivered sale.
   All 7 `Void`s traced: 4 are same-day/near-day **re-dos** whose corrected
   replacement is already in the data as `OnOrder` (keeping the void would
   *double-count*); 3 are genuine non-sales (a cancelled piece, a lost order, one
   too recent to tell). Dropping `Void` is correct and complete; the rest of
   `WAREHOUSE_NON_SALE_STATUSES` is harmless future-proofing.

3. ~~History beyond the retention window~~ — **closed.** The store started on
   LifeSaver in May 2024; there is no earlier history (§7).

4. **Commercial customers.** `"Lafayette College - Laura McKee"`,
   `"Nestle Purina Petcare - Deion Taylor"` — treat as the company (one customer)
   or per contact? Default today is per full-string (no split). Low volume so far.

5. **Fuzzy-match thresholds** — the backfill produced **zero** normalized-merge
   collisions across 628 customers, i.e. every repeat customer's name was entered
   byte-identically each time, or the differences are real. Before building fuzzy
   matching, check how many near-duplicate names actually exist — it may not be
   worth it.

6. **Dashboard front end** — Looker Studio-style BI vs. a small custom web app vs.
   an MCP-driven view. Decide next.

7. ~~Baseline provenance~~ — **closed.** The baselines came from a first-pass
   analysis earlier the same day (2026-09-07); the warehouse figures supersede
   them (they factor in data-quality handling the first pass missed — `Void`
   re-dos, the visit grain, identity normalisation). Use the warehouse numbers.

## 13. Repo layout — as built

The design doc lives in `dashboard/`; the backend is its own package, sibling to
`lifesaver/` and `mcp_server/`. It's mostly ETL, not UI.

```
lifesaver/          unchanged — the lsscloud.com scraper client/parser
warehouse/          Phase 3 backend
  config.py         env-var settings (db path, raw dir, status filter, windows)
  models.py         dataclasses + natural_key() + content_hash() + CALC_VERSION
  months.py         month arithmetic (no python-dateutil)
  identity.py       pure: customer_raw -> customer_id (exact + normalized)
  visits.py         pure: line_items -> visits + customer_lifecycle
  kpis.py           pure: visits -> the 5 KpiSnapshots for a report month
  store.py          SQLite: the ONLY storage-aware module (raw / upsert / query)
  pipeline.py       orchestration: sync_range, resolve, rebuild, snapshot, backfill
  job.py            CLI: status | backfill | sync | resolve | rebuild | snapshot | kpis | review
dashboard/
  DESIGN.md         this doc
  build.py          reads the warehouse -> self-contained index.html (+ data.json)
  publish.sh        build + upload index.html/data.json to the GCS site bucket
  refresh.sh        full daily cycle: pull db from GCS -> job sync -> publish -> push back
  index.html        generated dashboard (gitignored)
tests/              test_warehouse_*.py  (48 tests)
```

Dependency direction: `warehouse/` imports `LifesaverClient` + `parse_work_order_csv`
from `lifesaver/`; `dashboard/build.py` imports `warehouse.models.CALC_VERSION`
and otherwise reads `warehouse.db` directly. One way, no cycles.

All analytics (`identity`, `visits`, `kpis`) are pure functions over dataclasses —
no I/O, no SQL — so they are fully unit-tested and the storage engine can change
without touching them.

## 14. Build order

1. ~~Storage + Layer 2 schema~~ — **done** (`store.py`, SQLite; §5).
2. ~~Sync: `LifesaverClient` → raw file → upsert `line_items`~~ — **done**
   (`pipeline.sync_range`; natural key per §6; change-tracking into
   `line_item_history`).
3. ~~Backfill (resumable, month-by-month)~~ — **done** (`pipeline.backfill`,
   `job.py backfill`). **Not yet run against live data.**
4. ~~Identity resolution: exact + normalized~~ — **done** (`identity.py`).
5. ~~Layer 3: `visits`, `customer_lifecycle`~~ — **done** (`visits.py`).
6. ~~`kpi_monthly` for all five metrics + historical backfill~~ — **done**
   (`kpis.py`, `pipeline.snapshot_history`, `job.py snapshot --all`).
7. ~~Run the backfill locally; validate KPIs against the baselines~~ — **done
   2026-09-07.** 37 monthly pulls, 1086 line items, 628 customers, 853 visits,
   0 unresolved, 0 row loss. `first_to_second_rate` = 19.1 % (baseline 19.2 %).
   Other baselines match the warehouse's late-2025 snapshots and have since
   drifted up (§12 #7). No horizon bias — store began May 2024 (§7).
8. Decide production persistence (§5 sub-section / §12 #6).
9. Daily + monthly Cloud Scheduler triggers on a Cloud Run Job.
10. MCP read tools: `get_retention_kpis(...)`, `get_customer_history(...)`; retire
    the live-scrape path (model A, §9).
11. ~~Dashboard front end~~ — **v2 done 2026-09-07.** `dashboard/build.py` reads
    the warehouse and renders a self-contained `dashboard/index.html`: the full
    KPI history is embedded and the page renders client-side. Opens on the
    **current month** (the live view a daily rebuild keeps moving, marked with a
    LIVE badge); a period dropdown reaches the current quarter (also live) and
    every closed 2026 monthly + quarterly report (frozen — `is_final` guarantees
    those rows never change). `ⓘ` tooltips on every metric. `?p=<period>` deep
    links. `--json` also dumps `data.json`. Published as a private Artifact.
    **v3 (2026-09-07):** a "The business" section leads the page — headline
    revenue tiles (period revenue with MoM + YoY, trailing-12-months vs prior
    year, orders + average ticket, revenue-to-date) and a monthly-revenue bar
    chart with a 3-month average line. All period-aware; the live month shows
    month-to-date with an on-pace projection and a same-days-last-year YoY. Built
    from the `visits` table (same revenue basis as the KPIs), so no schema
    change.
    **v4 (2026-09-08):** renamed to **Frame Shop Performance**. Added a
    **monthly performance vs. last year** table near the top: one row per month
    (`Month | prior-yr revenue | this-yr revenue | Δ% | prior-yr orders |
    this-yr orders | Δ% | avg ticket this yr + Δ%`), a `YTD` / `Trailing 12 mo`
    total row, a **Calendar year / Trailing 12 months** toggle, and an
    **Exclude the Polaris Mission** checkbox. The current month's row is
    month-to-date vs the same run of days a year earlier (reuses the existing
    `partial_ly_*` fields). The table renders fully client-side; `build.load()`
    ships `business.outlier` (per-month rev/order contribution of the two
    `OUTLIER_VISIT_IDS`) plus `partial_ly_{rev,v}_ex`, so the toggle needs no
    rebuild. The Polaris Mission = customer `c781adfa3958` ("Casey Phillips" /
    "The Polaris Mission"), two Oct–Nov 2024 commissions for a SpaceX program,
    ~$39.5k net — it only moves comparisons whose prior-year month is Oct or
    Nov 2024 (the trailing-12 view); toggle scope is the business section only,
    retention KPIs untouched.
12. **Daily auto-refresh** — see §15. Needs step 8 (persistent warehouse) first.
    *Partly done (2026-09-07):* dashboard **code** deploys are automated — a push
    to `main` re-renders `index.html` from the GCS warehouse and re-uploads it
    (`.github/workflows/publish.yml` → `deploy-dashboard` → `dashboard/publish.sh`).
    The scheduled **data** pull is still open.
13. Fuzzy identity — only if §12 #5 finds enough near-duplicate names to matter.

## 15. Daily refresh

Goal: every day, no human action — pull fresh data, recompute, rebuild, deploy.
The **live** views (current month, current quarter) move; every closed period
stays frozen (the warehouse's `is_final` flag guarantees it).

### Done (2026-09-07)

- **GCS buckets** in project `mcps-507817`, region `us-central1`:
  - `gs://lifesaver-kpi-warehouse` — private (`publicAccessPrevention` on),
    object versioning on. Holds `warehouse.db` + `warehouse_raw/`. This is now
    the system of record; the local copy is a working copy.
  - `gs://lifesaver-kpi-dashboard-303f74` — static site
    (`mainPageSuffix: index.html`). `index.html` + `data.json` uploaded.
- **`dashboard/publish.sh`** — build + upload only: `dashboard.build --json` →
  upload `index.html` + `data.json` to the site bucket. No LifeSaver login, no
  KPI recompute; renders from whatever is in the local `warehouse.db`. Called by
  both `refresh.sh` and the `deploy-dashboard` CI job.
- **`dashboard/refresh.sh`** — the full cycle: pull db from GCS → `warehouse.job
  sync` → `dashboard/publish.sh` → push db + raw back. Runs locally (gcloud auth
  + LIFESAVER creds) and is what the scheduled job will run.

### Still to do

1. **Make the site readable.** The site bucket is not public yet — pending a
   decision on exposure (the dashboard carries customer counts and lifetime
   revenue). Either `allUsers:objectViewer` on the bucket (anyone with the URL),
   or put it behind auth (IAP + load balancer, or Firebase with sign-in).
2. **Schedule the data pull.** Dashboard *code* deploys are now automated on
   push to `main` (`.github/workflows/publish.yml` → `deploy-dashboard`), but
   the daily LifeSaver pull / KPI recompute is not. Options: a Cloud Run Job
   running `refresh.sh` on a daily Cloud Scheduler trigger
   (`cloudscheduler.googleapis.com` not yet enabled; needs an image with the
   warehouse + dashboard code + gcloud + LIFESAVER creds from Secret Manager and
   a service account with objectAdmin on both buckets), or a scheduled GitHub
   Actions workflow with the LIFESAVER creds as repo secrets.
3. **LifeSaver single-session** — the job and the deployed MCP server both log
   in. Low collision risk for now (MCP is min-instances=0, on-demand; the job
   runs at a fixed early hour). Clean fix is model A (§9).

Optional later: switch `index.html` to `fetch('data.json')` so a refresh only
re-uploads the small JSON. `build.py --json` already emits it.
