# Lifesaver Software (lsscloud.com) report tooling

Two scripts, meant to be run from the same folder:

| Script | Purpose |
|---|---|
| `lifesaver_report_pull.py` | Pulls one report (currently `WorkOrderList`) to CSV for a given date range. **Unchanged in this pass.** |
| `inspect_report.py` | Discovers what parameter fields a report page actually has, so the rest of the 51-report catalog can be added without guessing field names one at a time. |

Both talk to `https://lsscloud.com`, a classic ASP.NET Web Forms app with a Microsoft SSRS ReportViewer control embedded per report page, wrapped in a thin MVC login shell. See `spec.md` for the full background on how that flow works (login → get page → postback → scrape ReportSession/ControlID → export).

## Setup

```
pip install requests beautifulsoup4
export LIFESAVER_USERNAME=...
export LIFESAVER_PASSWORD=...
```

Both scripts read credentials from those two environment variables — never pass them on the command line or commit them anywhere.

## `lifesaver_report_pull.py`

Pulls the Work Order List report to a CSV file.

```
python lifesaver_report_pull.py --start 7/1/2025 --end 9/1/2026 --out workorderlist.csv
```

- `--start` / `--end`: dates in `M/d/yyyy`, no zero-padding (e.g. `7/1/2025`, not `07/01/2025`) — this is the exact format the live date-picker textbox uses.
- `--out`: output CSV path (default `workorderlist.csv`).
- `--report-url`: override the report page URL if you want to point `pull_report()` at a different `/Reports/<X>` page — works today only if that report happens to share WorkOrderList's exact field layout (two plain date textboxes at `ctl08$ctl03`/`ctl08$ctl05`). Most reports in the catalog probably don't — that's what `inspect_report.py` is for.

## `inspect_report.py`

Read-only reconnaissance tool. For a given report page, it works out:
- whether it's a standard ReportViewer parameter page at all (looks for the `ctl08$ctl00` "View Report" button, confirmed constant across the app),
- what parameter fields it has, in what order, with what field name/id/type,
- a best-guess label for each field (by reading the visible text immediately before it in the page, same as a person would),
- whether that field matches the *confirmed* WorkOrderList shape (plain text `txtValue`) or looks different (dropdown, checkbox, something else) and so needs a human to look at it.

It never submits a date range or exports anything — one GET per report, just to see the parameter panel as initially rendered.

### Commands

```
# sanity-check the catalog with no network requests
python inspect_report.py --list

# confirm the tool against the one report we already know, before trusting it on the rest
python inspect_report.py --only WorkOrderList

# try a handful (comma-separated path or name substrings, case-insensitive)
python inspect_report.py --only "employee sales,PaymentSummary,Salesperson"

# the full 51-report sweep
python inspect_report.py

# resume a previous run without re-fetching what's already in the manifest
python inspect_report.py --skip-existing

# be gentler/rougher on the server (default 1.5s between requests)
python inspect_report.py --delay 3
```

### Output: `report_manifest.json`

One entry per report path, written after every single report (not just at the end), so a crash or Ctrl-C never loses progress and `--skip-existing` can pick up where you left off. Each entry looks like:

```json
{
  "/Reports/WorkOrderList": {
    "path": "/Reports/WorkOrderList",
    "category": "Work Orders",
    "name": "Work Order List",
    "final_url": "https://lsscloud.com/Reports/WorkOrderList",
    "http_status": 200,
    "reportviewer_button_found": true,
    "submit_button_field": "ctl00$ContentPlaceHolder1$reportViewer$ctl08$ctl00",
    "parameters": [
      {
        "field_name": "ctl00$ContentPlaceHolder1$reportViewer$ctl08$ctl03$txtValue",
        "field_id": "ctl00_ContentPlaceHolder1_reportViewer_ctl08_ctl03_txtValue",
        "tag": "input",
        "input_type": "text",
        "label_guess": "Start Date",
        "current_value": "9/1/2025"
      },
      { "...End Date field..." : "..." }
    ],
    "all_params_look_like_confirmed_date_pattern": true,
    "needs_manual_review": false,
    "review_reason": null
  }
}
```

**`needs_manual_review: true`** is the field to filter on when deciding what to look at by hand. It gets set when:
- no `ctl08$ctl00` button was found at all (page may not be a ReportViewer page the same way — expected for the two `/Reporting/`-prefixed URLs in the catalog, `LifeSaverPaymentsPayoutReport` and `ReprintInvoice`),
- a button was found but it has zero parameter fields underneath it (report may take no parameters), or
- at least one parameter isn't a plain-text `txtValue` field like the confirmed date fields (a dropdown, checkbox, or anything else) — `review_reason` says which.

When `reportviewer_button_found` is `false`, check `all_reportviewer_fields_on_page` in that entry — it's every `ctl00$...$reportViewer$...` field found anywhere on the page as a raw fallback, in case the container-detection heuristic (climbing from the button to its `ctl08` parent) missed something rather than the page genuinely lacking a ReportViewer control.

### What this doesn't do (yet)

- Doesn't fill in a date range or hit the export endpoint — it only reads the page's *initial* rendered state, so a report whose parameter panel changes shape after an interaction (rare, but possible) wouldn't be fully captured.
- `label_guess` is exactly that — a heuristic based on nearest preceding visible text. It matched the known WorkOrderList labels correctly in testing, but should be spot-checked per report, especially any flagged `needs_manual_review`.
- Doesn't yet turn `report_manifest.json` into the config `lifesaver_report_pull.py` would consume to generalize `pull_report()` across all 51 reports — that's the next step once the manifest is reviewed.
