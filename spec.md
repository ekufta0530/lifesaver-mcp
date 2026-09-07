# Lifesaver Software Report Puller — Spec

## Goal

Two-phase build:

1. **Phase 1 — API service.** A small service that programmatically authenticates
   to Lifesaver Software's `lsscloud.com` (a Microsoft ReportViewer / SSRS-backed
   ASP.NET Web Forms app), pulls the `WorkOrderList` report for an arbitrary date
   range, and returns it as clean structured data (CSV parsed into JSON, or raw
   CSV) via a simple internal HTTP API.
2. **Phase 2 — MCP server.** Wrap Phase 1's API as an MCP server so the report can
   be queried directly from Claude (e.g. "pull last month's work order list from
   Lifesaver").

Phase 1 must work standalone and be testable on its own before Phase 2 wraps it.
Approval to programmatically access this report has already been obtained from
Lifesaver Software.

**Phase 3 — retention KPI dashboard.** Accumulate work-order history into a local
warehouse (solving both the 36-month source-retention limit and the fact that the
KPIs need full per-customer history) and serve five retention KPIs. Backend built
in `warehouse/` (SQLite; see `warehouse/README.md`). Design and open questions:
`dashboard/DESIGN.md`.

## Background / how the target system works

Lifesaver's report page is **not** a REST API — it's a classic ASP.NET Web Forms
page (`Microsoft.Reporting.WebForms.ReportViewer` control) that proxies to a
backend SQL Server Reporting Services (SSRS) instance. Confirmed via DevTools:

- Report page: `https://lsscloud.com/Reports/WorkOrderList`
- Underlying SSRS server (seen via `RSProxy` param):
  `http://lifesaver-sql1.corp.lifesaversoft.com/reportserver`
- Export handler: `https://lsscloud.com/Reserved.ReportViewerWebControl.axd`
- Server stack: IIS 10 / ASP.NET 4.0.30319 (Web Forms, not MVC/API)

This means data retrieval is a **3-step stateful HTTP flow**, not a single
request:

1. **GET the report page** (authenticated) → scrape hidden Web Forms state
   fields out of the HTML: `__VIEWSTATE`, `__VIEWSTATEGENERATOR`,
   `__EVENTVALIDATION`, plus the other `ctl00$...` form fields present on the
   page (echo these back unchanged).
2. **POST back to that same URL** (`/Reports/WorkOrderList`) with the scraped
   hidden fields plus the date-range parameters, to set the report's parameters
   and generate a fresh `ReportSession` + `ControlID` for that date range:
   - `ctl00$ContentPlaceHolder1$reportViewer$ctl08$ctl03$txtValue` = start date
   - `ctl00$ContentPlaceHolder1$reportViewer$ctl08$ctl05$txtValue` = end date
   - Date format: `M/d/yyyy`, **no zero-padding** (e.g. `7/1/2025`, not
     `07/01/2025`)
   - The response HTML/inline JS contains the new `ReportSession` for this
     parameterized run — needs to be scraped out (exact location TBD, likely
     inline `<script>`).
3. **GET the export endpoint** with the fresh `ReportSession` + `ControlID`,
   `Format=CSV`:
   ```
   GET https://lsscloud.com/Reserved.ReportViewerWebControl.axd
     ?ReportSession=<from step 2>
     &Culture=1033&CultureOverrides=True
     &UICulture=1033&UICultureOverrides=True
     &ReportStack=1
     &ControlID=<confirm: stable or regenerated each session?>
     &RSProxy=http%3a%2f%2flifesaver-sql1.corp.lifesaversoft.com%2freportserver
     &OpType=Export
     &FileName=LifeSaver+Reports
     &ContentDisposition=OnlyHtmlInline
     &Format=CSV
   ```
   Response body is the CSV.

Login itself (username/password) is expected to be a plain form POST — needs to
be captured from DevTools and confirmed as not JS-token-gated before assuming
`requests`/`httpx` alone can do it (see Open Questions).

No headless browser should be required for any of this — it's a pure
HTTP-client + cookie-jar problem end to end, *if* login is genuinely a plain
form POST as expected. Fallback plan if login turns out to be JS-heavy: use a
one-time Playwright login step purely to mint the auth cookie, then hand that
cookie to the plain HTTP client for steps 1–3.

## Phase 1 — API service

### Responsibilities

- Authenticate to lsscloud.com and maintain/refresh a session cookie.
- Execute the 3-step flow above for a given `(start_date, end_date)`.
- Parse the returned CSV into structured rows.
- Expose this via a small local HTTP API, e.g.:
  - `GET /reports/work-order-list?start=2025-07-01&end=2026-09-01`
  - Returns JSON (parsed rows) by default; optionally raw CSV via
    `?format=csv`.
- Handle session expiry / re-login transparently (retry once on auth failure).
- Config via environment variables, not hardcoded: `LIFESAVER_USERNAME`,
  `LIFESAVER_PASSWORD`, base URL, etc. Never commit credentials.

### Non-goals for Phase 1

- No caching/scheduling layer yet (can be added later if needed).
- No support for reports other than `WorkOrderList` yet — but structure the
  code so adding another report (different date-field control IDs, different
  export path) is a config change, not a rewrite.
- No UI.

### Suggested stack

- Python, `httpx` or `requests` for the HTTP client, `BeautifulSoup` for
  scraping the Web Forms hidden fields, a lightweight web framework (FastAPI
  recommended — gives you the HTTP API and OpenAPI schema for free, which also
  makes Phase 2's MCP wrapping easier) to expose the endpoint.
- A starter script already exists for the 3-step flow
  (`lifesaver_report_pull.py`, shared earlier in this conversation) — treat it
  as the skeleton for the core client logic, not the finished service.

### Open questions to resolve during implementation

- [ ] What does the login POST actually look like? (endpoint, field names, any
      CSRF/anti-forgery token, whether it's its own Web Forms postback)
- [ ] Where exactly does `ReportSession` appear in the step-2 response body?
      (confirm format so the scraping regex/parser is reliable)
- [ ] Is `ControlID` stable across sessions/logins, or regenerated each time?
      If regenerated, where does it first appear (probably also needs to be
      scraped from the initial page load, not hardcoded).
- [ ] Does the step-2 postback actually require *every* `ctl00$...` field from
      the original page echoed back, or can extraneous ones be dropped?
- [ ] Session/cookie lifetime — how long before a re-login is needed?
- [x] Confirm export `Format=CSV` output is clean/parseable as-is, or if it
      needs cleanup. **Answered:** WorkOrderList's CSV is clean (UTF-8 BOM only).
      Other reports' CSV is unusable — see "Report coverage" below.

## Phase 2 — MCP server

### Responsibilities

- Wrap Phase 1's API as an MCP server exposing at least one tool, e.g.
  `get_work_order_list_report(start_date, end_date)`, returning the parsed
  report data to Claude.
- Tool description/schema should make clear what date range format is
  expected and what the report contains, so Claude can call it correctly
  without guessing.
- Should call Phase 1's API over HTTP (keep the two phases decoupled) rather
  than reimplementing the scraping logic — this keeps the MCP layer thin and
  lets Phase 1 be tested/used independently.

### Non-goals for Phase 2 (initially)

- Multi-report support beyond what Phase 1 exposes.
- Write operations — this is read-only reporting data.

## Report coverage — why this stays WorkOrderList-only

`WorkOrderList` is the **only** report in `lsscloud.com` that produces usable
tabular data. This was established, not assumed:

1. `scripts/inspect/inspect_report.py` was run against the live site and captured
   the parameter panel of all 51 report pages → `report_manifest.json`.
2. A generalized build was wired for the 27 reports whose only parameters are a
   start + end date (same shape as WorkOrderList) and each was pulled live.
3. **Every one except WorkOrderList exported as SSRS visual-layout internals, not
   data.** The CSV renderer serializes whatever the report *draws*:

   | Export columns you get back | What the report actually is | Examples |
   |---|---|---|
   | `Textbox1`, `Textbox2`, `Textbox189`, … | a summary layout whose RDL never set `DataElementName` on its cells, so SSRS falls back to the textbox control names | Payment Summary, Order/Work-Order/Tax-Exempt summaries, Promotions, Department Sales, Production Details |
   | `…_Chart1_CategoryGroup_label`, `…_Chart1_CategoryGroup_Value_Y` | a **chart** — you get the plotted series points | Employee Sales, Orders by Weekday, Orders by Hour, Assembly Times, Delivery Times, Glazing Usage |
   | `RadialGauge1_RadialScale1_MinimumValue`, `RadialGauge1_RadialPointer1_GaugeInputValue`, … | a **gauge** — you get the needle position | Customer Revenue, Mat Usage, Moulding Usage |

   `WorkOrderList` is the exception only because its RDL was authored with real
   CSV column names (`invoiceNumber`, `workOrderNumber`, `customer`, …) over a
   single flat table.

That generalized build was reverted. `report_manifest.json` is kept in the repo
as reference.

### If the other reports' data is ever needed

Do **not** try to parse their CSV. SSRS has an **ATOM data-feed renderer**
(`Format=ATOM` in place of `Format=CSV` on the `Reserved.ReportViewerWebControl.axd`
export call) that ignores the visual layout and returns the underlying *dataset*
rows as Atom XML — one feed per data region in the report. That is the path to
structured data from the dashboard-style reports. It is a separate effort: it
needs XML parsing and handling of multiple feeds per report, and each report's
datasets still have to be understood individually.

### Also unmapped regardless of export format

Reports whose parameter panel is more than two date textboxes were never wired at
all (dropdowns, radio toggles, month/year pickers, multi-value text filters). The
two `/Reporting/`-prefixed pages (`LifeSaverPaymentsPayoutReport`, `ReprintInvoice`)
are not ReportViewer pages. `PastDue` renders with no parameter panel. Field
layouts for all of these are in `report_manifest.json`.

## Environment / running notes

- Target IDE: VS Code, building with Claude Code.
- No browser automation dependency expected (see Background) — keep the
  implementation to a standard HTTP client + parser stack unless the login
  investigation proves that wrong.
