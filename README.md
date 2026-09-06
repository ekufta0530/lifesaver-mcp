# lifesaver-mcp

Pull SSRS "Work Order List" reports from Lifesaver Software (`lsscloud.com`) as
structured data. Two phases (see [spec.md](spec.md)):

1. **Phase 1 — client + API** (`lifesaver/`): authenticates, runs the 3-step
   ReportViewer scrape, parses the CSV. Usable as a library (`LifesaverClient`)
   or a local FastAPI service.
2. **Phase 2 — MCP server** (`mcp_server/`): exposes the report as an MCP tool,
   over **stdio** locally or **streamable-http** when deployed (e.g. a claude.ai
   custom connector). Calls Phase 1's client in-process. See [DEPLOY.md](DEPLOY.md).

## Layout

| Path | What |
|---|---|
| `lifesaver/client.py` | the 3-step scrape as a class; one session, re-login + retry on expiry |
| `lifesaver/parser.py` | CSV bytes → typed rows (BOM, `$` currency, `M/d/yyyy` dates) |
| `lifesaver/reports.py` | per-report config (page path + date-field control IDs) |
| `lifesaver/models.py` | pydantic row/response schemas |
| `lifesaver/api.py` | FastAPI app (local dev; not deployed) |
| `mcp_server/server.py` | MCP server: one tool, `get_work_order_list_report(start_date, end_date)`; stdio + streamable-http transports, bearer-token auth |
| `lifesaver_report_pull.py` | original standalone reference script (still runnable) |
| `Dockerfile`, `DEPLOY.md` | one-image build + Cloud Run deploy for the remote (claude.ai) setup |
| `.github/workflows/publish.yml` | test → build → push image to `ghcr.io/<repo>` |
| `tests/` | offline suite: fake session + saved HTML/CSV fixtures |

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
