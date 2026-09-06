# lifesaver-mcp

Pull SSRS "Work Order List" reports from Lifesaver Software (`lsscloud.com`) as
structured data. Two phases (see [spec.md](spec.md)):

1. **Phase 1 — API service** (`lifesaver/`): authenticates, runs the 3-step
   ReportViewer scrape, parses the CSV, serves it over HTTP.
2. **Phase 2 — MCP server** (`mcp_server/`): thin adapter, forwards MCP tool
   calls to Phase 1 over HTTP.

## Layout

| Path | What |
|---|---|
| `lifesaver/client.py` | the 3-step scrape as a class; one session, re-login + retry on expiry |
| `lifesaver/parser.py` | CSV bytes → typed rows (BOM, `$` currency, `M/d/yyyy` dates) |
| `lifesaver/reports.py` | per-report config (page path + date-field control IDs) |
| `lifesaver/models.py` | pydantic response schemas (→ OpenAPI → Phase 2 tool schema) |
| `lifesaver/api.py` | FastAPI app |
| `mcp_server/server.py` | MCP server: one tool, `get_work_order_list_report(start_date, end_date)` |
| `lifesaver_report_pull.py` | original standalone reference script (still runnable) |
| `tests/` | offline suite: fake session + saved HTML/CSV fixtures |

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # fill in LIFESAVER_USERNAME / LIFESAVER_PASSWORD
```

## Run the API

```bash
.venv/bin/uvicorn lifesaver.api:app --port 8000
```

- `GET /reports/work-order-list?start=2025-08-01&end=2025-08-31` → JSON
- add `&format=csv` for the raw export
- interactive docs at `http://localhost:8000/docs`

## Run the MCP server (Phase 2)

Needs the Phase 1 API running (above). Then, for Claude Code, `.mcp.json` is
already wired — just approve the `lifesaver` server. Or run it by hand:

```bash
LIFESAVER_API_URL=http://localhost:8000 .venv/bin/python -m mcp_server.server
```

Exposes one tool: `get_work_order_list_report(start_date, end_date)` with ISO
dates. It calls Phase 1 over HTTP and returns the parsed rows — no scraping
logic of its own.

## Standalone script (no server)

```bash
export LIFESAVER_USERNAME=... LIFESAVER_PASSWORD=...
.venv/bin/python lifesaver_report_pull.py --start 8/1/2025 --end 8/31/2025 --out aug.csv
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

Offline only — no network, no credentials. The fixtures in `tests/fixtures/`
approximate the live responses; `export_sample.csv` is a real capture. After any
live run, refresh the HTML fixtures from real responses to make the suite true
regression coverage.
