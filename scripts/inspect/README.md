# Report-page recon — `inspect_report.py`

**Status: parked.** This was a one-off reconnaissance pass over the other report
pages in `lsscloud.com`, done while deciding whether the puller could be
generalized beyond `WorkOrderList`. The answer was no — every other report
exports SSRS visual-layout internals (textbox names, chart series, gauge needle
positions), not data. See **[spec.md](../../spec.md) → "Report coverage"** for
the findings and, if the other reports' data is ever actually needed, the ATOM
data-feed renderer as the path forward (it does not use this script).

The script and its output ([`report_manifest.json`](../../report_manifest.json)
at the repo root) are kept only as reference for the per-report parameter-field
layouts. Nothing in the codebase imports either.

## What it does

For a report page, `inspect_report.py` works out — without submitting anything or
exporting — :
- whether it's a standard ReportViewer parameter page (looks for the
  `ctl08$ctl00` "View Report" button, constant across the app),
- what parameter fields it has, in order, with field name/id/type,
- a best-guess label per field (nearest preceding visible text),
- whether each field matches the confirmed `WorkOrderList` shape (plain text
  `txtValue`) or is something else (dropdown, checkbox, …) needing a human.

One GET per report, just the initially-rendered parameter panel.

## Running it

It imports `lifesaver_report_pull` for the login flow, so run it **from the repo
root**, not this folder:

```bash
export LIFESAVER_USERNAME=... LIFESAVER_PASSWORD=...

python scripts/inspect/inspect_report.py --list                  # catalog only, no network
python scripts/inspect/inspect_report.py --only WorkOrderList     # check against the known report
python scripts/inspect/inspect_report.py --only "employee sales,PaymentSummary"
python scripts/inspect/inspect_report.py                          # full sweep
python scripts/inspect/inspect_report.py --skip-existing          # resume from the manifest
python scripts/inspect/inspect_report.py --delay 3                # seconds between requests (default 1.5)
```

## Output: `report_manifest.json`

One entry per report path, written after each report so a crash never loses
progress. Each entry:

```json
{
  "/Reports/WorkOrderList": {
    "path": "/Reports/WorkOrderList",
    "category": "Work Orders",
    "name": "Work Order List",
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
      }
    ],
    "all_params_look_like_confirmed_date_pattern": true,
    "needs_manual_review": false,
    "review_reason": null
  }
}
```

`needs_manual_review: true` is the field to filter on. It gets set when: no
`ctl08$ctl00` button was found (e.g. the two `/Reporting/`-prefixed URLs), a
button was found with zero parameter fields under it, or at least one parameter
isn't a plain-text `txtValue` field (`review_reason` says which).

## Caveats

- Reads only the *initial* rendered state — a panel that changes shape after an
  interaction wouldn't be fully captured.
- `label_guess` is a heuristic (nearest preceding text). It matched the known
  `WorkOrderList` labels in testing; spot-check per report.
