# Lifesaver Software Report Puller — Spec

## Goal

Three-phase build:

1. **Phase 1 — API service.** A small service that programmatically authenticates
   to Lifesaver Software's `lsscloud.com` (a Microsoft ReportViewer / SSRS-backed
   ASP.NET Web Forms app), pulls the `WorkOrderList` report for an arbitrary date
   range, and returns it as clean structured data (CSV parsed into JSON, or raw
   CSV) via a simple internal HTTP API.
2. **Phase 2 — MCP server.** Wrap Phase 1's client as an MCP server so the report
   can be queried directly from Claude (e.g. "pull last month's work order list
   from Lifesaver"). Deployed to Cloud Run — see `DEPLOY.md`.
3. **Phase 3 — retention KPI warehouse + dashboard.** Accumulate work-order
   history into a local warehouse (solving both the ~36-month source-retention
   limit and the fact that the KPIs need full per-customer history) and serve
   five retention KPIs plus a business-revenue view. Backend built in
   `warehouse/` (SQLite; see `warehouse/README.md`). Design, KPI definitions, and
   open questions: `dashboard/DESIGN.md`.

Phase 1 must work standalone and be testable on its own before Phase 2 wraps it.
Approval to programmatically access this report has already been obtained from
Lifesaver Software.

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

### Open questions — all resolved during implementation

The working, deployed flow is `lifesaver/client.py` (`lifesaver_report_pull.py`
is the original single-file version). What the investigation settled:

- **Login** is a plain form POST (`UserName` / `Password`), no JS token gate — no
  headless browser needed anywhere.
- **`ReportSession`**, **`ControlID`**, and **`RSProxy`** are pulled out of the
  step-2 postback response by regex (they appear in the embedded viewer image
  URL) — read fresh on every pull, never hardcoded.
- The step-2 postback echoes back the hidden Web Forms state fields
  (`__VIEWSTATE`, `__VIEWSTATEGENERATOR`, `__EVENTVALIDATION`, …) **and every
  `ctl00$...` input on the page**, unchanged, exactly as the browser would —
  then overrides the two date fields and fires the "View Report" button by its
  own `name=value`.
- Session expiry is handled transparently — the client re-logs-in and retries
  once on an auth failure, and clears its own stale session on
  `UserAlreadyLoggedIn`.
- Export `Format=CSV` for `WorkOrderList` is clean (UTF-8 BOM only). Other
  reports' CSV is unusable — see "Report coverage" below.

## Phase 2 — MCP server

### Responsibilities

- Wrap Phase 1 as an MCP server exposing at least one tool, e.g.
  `get_work_order_list_report(start_date, end_date)`, returning the parsed
  report data to Claude.
- Tool description/schema should make clear what date range format is
  expected and what the report contains, so Claude can call it correctly
  without guessing.
- Reuse Phase 1's `LifesaverClient` + parser rather than reimplementing the
  scraping logic — this keeps the MCP layer thin and lets Phase 1 be
  tested/used independently.

**As built:** the server calls `LifesaverClient` **in-process** (not Phase 1's
HTTP API — that app is local-dev only). It holds one login for the life of the
process and serialises pulls behind a lock, because LifeSaver allows one session
per user; the deployed Cloud Run service is pinned to a single instance for the
same reason (see `DEPLOY.md`). Transports: stdio locally, streamable-http with
bearer-token auth when deployed.

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

## Invoices — a separate, simpler flow (with one non-obvious step)

`/Reporting/ReprintInvoice` (the "Find Invoice" catalog entry, one of the two
non-ReportViewer pages above) is a plain search box: entering an invoice number
loads the real report in an iframe pointing at:

```
https://lsscloud.com/Reports/Invoice/?StoreId=6006&InvoiceId=<internal id>
```

Confirmed live 2026-09-17 (invoice #584): **no postback is needed here**, unlike
WorkOrderList. A plain GET on that URL renders the invoice and already embeds a
fresh `ReportSession`/`ControlID` in the response — same regex shape as the
WorkOrderList postback response — so `Format=PDF` export works off that single
GET.

**Gotcha, also confirmed live 2026-09-17:** the `InvoiceId` that URL needs is
**not** the human-readable "Invoice #" (`WorkOrderList`'s `invoiceNumber`
column) — it's a separate internal id. Using the human number directly doesn't
fail loudly: `/Reports/Invoice/` still returns 200 with a valid-looking
`ReportSession`/`ControlID` and a well-formed PDF, but every field is unbound —
`Order Date`/`Last Revised` show `1/1/0001` (.NET `DateTime.MinValue`),
"Invoice #" itself shows `#Error`, every amount is `$0.00`. It's the report's
empty-state template, silently exported as a structurally valid PDF. The fix,
confirmed via live network capture: POST the human Invoice # as JSON to

```
POST https://lsscloud.com/Invoice/GetStoreInvoiceId/
Content-Type: application/json
{"invoiceId": "584"}
```

which resolves to the internal id (`584` → `10044999`, confirmed pairing) —
*that* value is what goes in `InvoiceId` on `/Reports/Invoice/`. The exact
response JSON key wasn't captured before the source browser session expired;
`resolve_invoice_id()` tries a list of likely candidates and raises with the
raw response body if none match, so it self-diagnoses rather than silently
misparsing.

Implementation, `lifesaver_report_pull.py`:
- `resolve_invoice_id()` — the human-# → internal-id POST above.
- `get_invoice_report_session()` — GET `/Reports/Invoice/` with the *resolved*
  id, returns `ReportSession`/`ControlID`/`RSProxy`.
- `pull_invoice()` — wires both together, then exports.
- `export_csv()` grew an `export_format` parameter (`"CSV"` default, `"PDF"`
  for invoices) instead of hardcoding CSV.

Verified end-to-end (resolve → render → export) with
`scripts/inspect/verify_invoice_endpoint.py`, which prints the raw resolve
response and warns that a well-formed PDF alone doesn't prove correctness —
open the file and check for real data, not just `%PDF-` magic bytes (that's
exactly how the empty-invoice bug above first slipped past testing).

Bulk-pulling a batch of invoices as PDFs is `pull_invoices.py` (invoice
numbers from a `WorkOrderList` CSV export, or a plain `--ids` list; one login,
one session, delay between requests, one failure doesn't kill the batch, each
PDF written to disk as it's pulled).

This is not the same effort as the reverted multi-report generalization above —
those reports failed because their *CSV* export serializes visual-layout
internals (textbox/chart/gauge names) instead of data. Invoices are pulled as
`Format=PDF`, which renders the actual visual layout — exactly what's wanted
for an invoice document, so that failure mode doesn't apply here.

## Customer list — a report built for exactly this, once found

For "build a customer list," the invoice PDFs are the wrong source — a real
sample (`pull_invoice`, invoice #584) has the customer's name and cell phone
only, no email or address, and pulling one is one HTTP round-trip per invoice.

The catalog has three reports built for this instead: **Customer Export**
(`/Reports/ConsumerInfoReport`), **Constant Contact Export**
(`/Reports/CustomerInfoExport_ConstantContact`), **Mailchimp Contact Export**
(`/Reports/CustomerInfoExport_Mailchimp`). These were excluded from the
original report-coverage sweep because their parameter panel isn't the plain
two-date-textbox shape that sweep covered — a "Customer Groups" text filter
plus a "Filter:" dropdown (no filter/show all, by order date range, by order
$, by order count, top N) — not because they were tried and found to export
visual-layout garbage like everything else in that sweep.

Confirmed live 2026-09-17: `ConsumerInfoReport`, with `Filter:` set to
**"No filter, show all customers."**, exports real per-customer columns —
`firstName, lastName, companyName, address1, address2, city, state,
postalCode, homePhone, workPhone, faxPhone, cellPhone, firstPurchase,
lastPurchase, lastPurchaseAmt, totalPurchase, emailAddress, category, ...` —
as CSV. That specific filter option ignores the report's date-range
parameters entirely, so the export is **every customer in the database**,
not scoped to any date window or invoice — confirmed as 672 rows spanning
years, no truncation.

The `Filter:` dropdown's field name isn't a stable ctl-number — it shifts
slightly between `ConsumerInfoReport` and its ConstantContact/Mailchimp
siblings (they lack `ConsumerInfoReport`'s two extra fields: a "Show these
fields" customization textbox and an email-only radio toggle, which shifts
every later field's ctl-number). `find_customer_export_filter_field()` in
`lifesaver_report_pull.py` finds it by matching the "No filter, show all
customers." option text instead of a hardcoded field name, so it works
against any of the three variants unchanged.

Implementation: `lifesaver_report_pull.py` —
`find_customer_export_filter_field()` + `pull_customer_export()`. CLI:
`pull_customers.py`. Recon record: `scripts/inspect/verify_customer_export.py`
(used to confirm this before promoting it into real code).

## Environment / running notes

- Target IDE: VS Code, building with Claude Code.
- No browser automation dependency expected (see Background) — keep the
  implementation to a standard HTTP client + parser stack unless the login
  investigation proves that wrong.
