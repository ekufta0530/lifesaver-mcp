# lifesaver-mcp

Pull SSRS "Work Order List" reports from Lifesaver Software (`lsscloud.com`) as
structured data, and track customer-retention KPIs on top of the accumulated
history. Three phases (see [spec.md](spec.md) and [dashboard/DESIGN.md](dashboard/DESIGN.md)):

1. **Phase 1 — client + API** (`lifesaver/`): authenticates, runs the 3-step
   ReportViewer scrape, parses the CSV. Usable as a library (`LifesaverClient`)
   or a local FastAPI service.
2. **Phase 2 — MCP server** (`mcp_server/`): exposes the report as an MCP tool,
   over **stdio** locally or **streamable-http** when deployed (e.g. a claude.ai
   custom connector). Calls Phase 1's client in-process. Deployed to Cloud Run —
   see [DEPLOY.md](DEPLOY.md).
3. **Phase 3 — retention warehouse + dashboard** (`warehouse/`, `dashboard/`):
   accumulates every pull into a local SQLite warehouse (the source only keeps a
   rolling ~36 months, and the KPIs need full per-customer history), computes
   five retention KPIs, and renders a self-contained HTML dashboard published to
   a GCS static site. See [warehouse/README.md](warehouse/README.md).

## Layout

| Path | What |
|---|---|
| `lifesaver/client.py` | the 3-step scrape as a class; one session, re-login + retry on expiry |
| `lifesaver/parser.py` | CSV bytes → typed rows (BOM, `$` currency, `M/d/yyyy` dates) |
| `lifesaver/reports.py` | per-report config (page path + date-field control IDs); WorkOrderList only |
| `lifesaver/models.py` | pydantic row/response schemas |
| `lifesaver/api.py` | FastAPI app (local dev; not deployed) |
| `mcp_server/server.py` | MCP server: one tool, `get_work_order_list_report(start_date, end_date)`; stdio + streamable-http transports, bearer-token auth |
| `warehouse/` | Phase 3 backend: raw landing → `line_items` → `visits` → `kpi_monthly`. Pure-function analytics + one SQLite module. CLI: `python -m warehouse.job` |
| `dashboard/build.py` | reads `warehouse.db` → self-contained `index.html` (+ `data.json`) |
| `dashboard/publish.sh` / `refresh.sh` | publish the rendered site to GCS / full daily cycle (pull → sync → publish) |
| `lifesaver_report_pull.py` | original standalone reference script (still runnable); also has `pull_invoice()` (single invoice as PDF) and `pull_customer_export()` (full customer list as CSV) |
| `scripts/pull_invoices.py` | bulk-pull invoices as PDFs (`invoice_<id>.pdf`), from a WorkOrderList CSV or a plain list of invoice numbers |
| `scripts/pull_customers.py` | pull the full customer list as CSV (every customer, not scoped to any date range) |
| `scripts/inspect/` | one-off recon of the other SSRS report pages (parked — see its README); also has `verify_invoice_endpoint.py` and `verify_customer_export.py`, which confirmed those two flows live before they were promoted into `lifesaver_report_pull.py` |
| `Dockerfile`, `DEPLOY.md` | one-image build + Cloud Run deploy for the remote (claude.ai) MCP setup |
| `.github/workflows/publish.yml` | on push to `main`: test → build+push image to GHCR → deploy MCP to Cloud Run → rebuild+publish the dashboard |
| `tests/` | offline suite: fake session + saved HTML/CSV fixtures, plus the warehouse pipeline tests |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements-dev.txt   # runtime + pytest
cp .env.example .env   # fill in LIFESAVER_USERNAME / LIFESAVER_PASSWORD
```

(`requirements.txt` alone is the container runtime set.)

## Run the API

```bash
.venv/bin/uvicorn lifesaver.api:app --port 8000
```

- `GET /reports/work-order-list?start=2025-08-01&end=2025-08-31` → JSON
- add `&format=csv` for the raw export
- interactive docs at `http://localhost:8000/docs`

## Run the MCP server (Phase 2)

Does **not** need the Phase 1 API — it runs the scrape in-process. It just needs
`LIFESAVER_USERNAME` / `LIFESAVER_PASSWORD` (from `.env` or the environment).

**Locally (stdio).** `.mcp.json` is already wired for Claude Code — approve the
`lifesaver` server. Or by hand:

```bash
.venv/bin/python -m mcp_server.server
```

**Remote (streamable-http)** — for a claude.ai custom connector:

```bash
MCP_TRANSPORT=streamable-http MCP_AUTH_TOKEN=$(openssl rand -hex 32) \
  .venv/bin/python -m mcp_server.server
# MCP at http://localhost:8080/mcp  (needs  Authorization: Bearer <token>)
# health at http://localhost:8080/health
```

Deploying this to Cloud Run + GHCR: **[DEPLOY.md](DEPLOY.md)**.

One tool: `get_work_order_list_report(start_date, end_date)` (ISO dates) → parsed
work-order rows.

## Retention warehouse + dashboard (Phase 3)

```bash
export LIFESAVER_USERNAME=... LIFESAVER_PASSWORD=...
.venv/bin/python -m warehouse.job backfill   # full history, month by month, resumable
.venv/bin/python -m warehouse.job kpis       # eyeball against the baselines
.venv/bin/python -m dashboard.build          # -> dashboard/index.html
```

Full CLI and layer model: [warehouse/README.md](warehouse/README.md). Design,
KPI definitions, and open questions: [dashboard/DESIGN.md](dashboard/DESIGN.md).
`warehouse.db`, `warehouse_raw/`, and the rendered `dashboard/index.html` are all
gitignored — the system of record is a GCS bucket (see DESIGN.md §15).

## Standalone script (no server)

```bash
export LIFESAVER_USERNAME=... LIFESAVER_PASSWORD=...
.venv/bin/python lifesaver_report_pull.py --start 8/1/2025 --end 8/31/2025 --out aug.csv
```

## Pulling invoices as PDFs

A single invoice (`lifesaver_report_pull.pull_invoice(session, invoice_number)`)
hits a different endpoint than WorkOrderList — `/Reports/Invoice/` needs no
postback, but the human-readable "Invoice #" must first be resolved to an
internal id via a separate POST (`resolve_invoice_id()`) before it works;
using the human number directly renders a silently-empty PDF (`$0.00`
everywhere, `#Error` in place of the invoice number). See
[spec.md](spec.md) → "Invoices — a separate, simpler flow" for how both of
these were confirmed.

Bulk-pulling, from a WorkOrderList CSV export's `invoiceNumber` column or a
plain list:

```bash
export LIFESAVER_USERNAME=... LIFESAVER_PASSWORD=...
.venv/bin/python scripts/pull_invoices.py --csv aug.csv --out-dir invoices
.venv/bin/python scripts/pull_invoices.py --ids 584,591,602 --out-dir invoices
```

Writes `invoices/invoice_<id>.pdf` per invoice, one login/session for the
whole batch, a delay between requests (`--delay`, default 1.5s), and one
invoice failing doesn't stop the rest.

## Pulling the customer list

Don't scrape it from the invoice PDFs (name + cell phone only, no
email/address, one request per invoice) — the report catalog has a report
built for exactly this. `Filter: "No filter, show all customers."` returns
every customer in the database with real columns (name, email, phone,
address, first/last purchase, total spend, ...), not scoped to any date
range. See [spec.md](spec.md) → "Customer list — a report built for exactly
this, once found" for how that was confirmed.

```bash
export LIFESAVER_USERNAME=... LIFESAVER_PASSWORD=...
.venv/bin/python scripts/pull_customers.py --out customers.csv
```

## One session per user

LifeSaver only allows one active session per user (license-limited). If a run
doesn't log out, the next login is blocked with `UserAlreadyLoggedIn`. The client
handles this automatically: it terminates **its own** stale session (never another
user's) and retries. To clear one manually:

```bash
LIFESAVER_USERNAME=... LIFESAVER_PASSWORD=... .venv/bin/python scripts/terminate_my_sessions.py
```

Set `LIFESAVER_TERMINATE_OWN_SESSION=false` to disable the auto-clear.

## Tests

```bash
.venv/bin/python -m pytest -q
```

Offline only — no network, no credentials, no GCP. The fixtures in
`tests/fixtures/` approximate the live responses; `export_sample.csv` is a real
capture. After any live run, refresh the HTML fixtures from real responses to
make the suite true regression coverage.
