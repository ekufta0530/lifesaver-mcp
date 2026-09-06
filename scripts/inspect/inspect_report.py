"""
Lifesaver Software (lsscloud.com) report parameter inspector.

Purpose: `lifesaver_report_pull.py` only knows the parameter field IDs for
ONE report (WorkOrderList: ctl08$ctl03$txtValue / ctl08$ctl05$txtValue for
start/end date). There are 52 reports total (see REPORT_CATALOG below,
scraped live from https://lsscloud.com/Reporting's dropdown menus) and
there's no reason to assume they all take the same two-date-field shape --
some probably take a single date, some a dropdown (employee/salesperson/
vendor), some no parameters at all.

This script visits a report page (authenticated, reusing the login() from
lifesaver_report_pull.py) and, instead of assuming field IDs, discovers them:

  1. Finds the ReportViewer's parameter/toolbar container. Confirmed live on
     WorkOrderList: the "View Report" submit button is always named
     ctl00$ContentPlaceHolder1$reportViewer$ctl08$ctl00, and ctl08 is the ID
     of the parameter-area container that holds the button AND every
     parameter's input control. So: find the button, walk up its parents
     until we hit the element whose id ends in "_ctl08" -- that's the
     container to search for this report's actual parameters.
  2. Walks that container's contents in document order, tracking the most
     recently seen non-empty text. Whenever it hits a form field (input/
     select/textarea) other than the button itself, it records that
     most-recent text as the field's "label_guess". This is the same thing
     a human does reading the rendered page top-to-bottom -- it doesn't
     depend on guessing exact ctl-number semantics, which we don't fully
     know beyond ctl08$ctl00/ctl03/ctl05 on this one report.
  3. Also records the raw type (text input / select+options / checkbox /
     radio / hidden), current/default value, and full field name+id.
  4. Falls back gracefully: if no ctl08$ctl00 button is found at all (a
     report might not be a ReportViewer page the same way -- notably the
     two /Reporting/ URLs in the catalog, which live under a different
     controller than every other /Reports/ URL), it says so explicitly
     rather than guessing, and dumps whatever ctl00$...$reportViewer$...
     fields exist anywhere on the page as a fallback for manual review.

Output: a JSON manifest (default report_manifest.json), one entry per
report path, written incrementally (after each report) so a crash or
Ctrl-C partway through doesn't lose earlier results and a re-run can pick
up where it left off with --skip-existing.

This is a read-only reconnaissance tool -- it never submits a date range or
exports anything, just GETs each report page once to see its parameter
panel as initially rendered.

Usage:
    export LIFESAVER_USERNAME=...
    export LIFESAVER_PASSWORD=...

    # inspect everything in the catalog:
    python inspect_report.py

    # inspect just a couple, by path or by name (case-insensitive substring):
    python inspect_report.py --only WorkOrderList,PaymentSummary
    python inspect_report.py --only "employee sales"

    # re-run without re-hitting reports already in the manifest:
    python inspect_report.py --skip-existing

    # just print the catalog (no requests made):
    python inspect_report.py --list
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup, NavigableString

# Reuses the already-confirmed login flow (plain POST, UserName/Password
# fields, session-cookie auth) instead of duplicating it here. Run this
# script from the same directory as lifesaver_report_pull.py.
import lifesaver_report_pull as lsp

BASE = lsp.BASE

# ---------------------------------------------------------------------------
# Full report catalog, scraped live from the dropdown menus at
# https://lsscloud.com/Reporting on 2026-09-06. Category/name are for
# human readability in the manifest; `path` is what actually gets fetched.
# The two "Reporting" (not "Reports") paths are flagged -- they may not be
# ReportViewer pages built the same way as the rest.
# ---------------------------------------------------------------------------
REPORT_CATALOG = [
    {"category": "Payments", "name": "Payment Summary", "path": "/Reports/PaymentSummary"},
    {"category": "Payments", "name": "Payment Summary Monthly", "path": "/Reports/PaymentSummaryMonthly"},
    {"category": "Payments", "name": "LifeSaver Payments Payouts", "path": "/Reporting/LifeSaverPaymentsPayoutReport"},
    {"category": "Payments", "name": "Write-off Summary", "path": "/Reports/WriteOffSummary"},
    {"category": "Orders", "name": "Order Summary", "path": "/Reports/FramingOrderSummary"},
    {"category": "Orders", "name": "Order Summary Monthly", "path": "/Reports/OrderSummaryMonthly"},
    {"category": "Orders", "name": "Delivered Order Summary", "path": "/Reports/DeliveredOrderSummary"},
    {"category": "Orders", "name": "Undelivered Order Summary", "path": "/Reports/UndeliveredOrderSummary"},
    {"category": "Orders", "name": "Voided Order Summary", "path": "/Reports/VoidedOrderSummary"},
    {"category": "Orders", "name": "Quote Summary", "path": "/Reports/QuoteSummary"},
    {"category": "Orders", "name": "Work Order Summary", "path": "/Reports/WorkOrderSummary"},
    {"category": "Work Orders", "name": "Work Order List", "path": "/Reports/WorkOrderList"},
    {"category": "Work Orders", "name": "Art Copies", "path": "/Reports/ArtCopies"},
    {"category": "Work Orders", "name": "PickList", "path": "/Reports/PickList"},
    {"category": "Work Orders", "name": "Cut List", "path": "/Reports/CutList"},
    {"category": "Paid In Full", "name": "Paid In Full", "path": "/Reports/PaidInFull"},
    {"category": "Paid In Full", "name": "Paid In Full Monthly", "path": "/Reports/PaidInFullMonthly"},
    {"category": "Ticket Sales", "name": "Employee Sales", "path": "/Reports/EmployeeSales"},
    {"category": "Ticket Sales", "name": "Salesperson", "path": "/Reports/Salesperson"},
    {"category": "Ticket Sales", "name": "Inventory Sales", "path": "/Reports/ArtSales"},
    {"category": "Ticket Sales", "name": "Promotions", "path": "/Reports/Promotions"},
    {"category": "Ticket Sales", "name": "Promotions Summary", "path": "/Reports/PromotionsSummary"},
    {"category": "Ticket Sales", "name": "Manually Entered Items", "path": "/Reports/ManuallyEnteredItems"},
    {"category": "Ticket Sales", "name": "Department Sales", "path": "/Reports/DepartmentSales"},
    {"category": "Customer", "name": "Customer Revenue", "path": "/Reports/ConsumerRevenueSummary"},
    {"category": "Customer", "name": "Customer Export", "path": "/Reports/ConsumerInfoReport"},
    {"category": "Customer", "name": "Constant Contact Export", "path": "/Reports/CustomerInfoExport_ConstantContact"},
    {"category": "Customer", "name": "Mailchimp Contact Export", "path": "/Reports/CustomerInfoExport_Mailchimp"},
    {"category": "Production", "name": "Details", "path": "/Reports/Production"},
    {"category": "Production", "name": "Past Due", "path": "/Reports/PastDue"},
    {"category": "Production", "name": "Orders by Weekday", "path": "/Reports/OrdersByWeekday"},
    {"category": "Production", "name": "Orders by Hour", "path": "/Reports/OrdersByHour"},
    {"category": "Production", "name": "Assembly Times", "path": "/Reports/AssemblyTimes"},
    {"category": "Production", "name": "Delivery Times", "path": "/Reports/DeliveryTimes"},
    {"category": "Production", "name": "Production Log", "path": "/Reports/ProductionLog"},
    {"category": "Production", "name": "Call List", "path": "/Reports/CallList"},
    {"category": "Material Usage", "name": "Mats", "path": "/Reports/MatUsage"},
    {"category": "Material Usage", "name": "Mouldings", "path": "/Reports/MouldingUsage"},
    {"category": "Material Usage", "name": "Glazing", "path": "/Reports/GlazingUsage"},
    {"category": "Material Usage", "name": "Material Detail", "path": "/Reports/MaterialDetail"},
    {"category": "Material Usage", "name": "Moulding Bin", "path": "/Reports/MouldingBin"},
    {"category": "Invoices", "name": "Find Invoice", "path": "/Reporting/ReprintInvoice"},
    {"category": "Closing", "name": "Closing Summary", "path": "/Reports/ClosingSummary"},
    {"category": "Closing", "name": "PickList", "path": "/Reports/PickList"},
    {"category": "Closing", "name": "Receivables", "path": "/Reports/Receivables"},
    {"category": "Inventory", "name": "Vendor Updates", "path": "/Reports/VendorUpdate"},
    {"category": "Inventory", "name": "Inventory Detail", "path": "/Reports/ArtInventory"},
    {"category": "Tax Exempt", "name": "Tax Exempt Orders", "path": "/Reports/TaxExemptOrderSummary"},
    {"category": "Tax Exempt", "name": "Tax Exempt Delivered Orders", "path": "/Reports/TaxExemptDeliveredOrderSummary"},
    {"category": "Tax Exempt", "name": "Tax Exempt Payments", "path": "/Reports/TaxExemptPaymentSummary"},
    {"category": "Tax Exempt", "name": "Tax Exempt Paid In Full", "path": "/Reports/TaxExemptPaidInFull"},
    {"category": "Store Admin", "name": "User Permissions", "path": "/Reports/UserPermissions"},
]

REPORTVIEWER_PREFIX = "ctl00$ContentPlaceHolder1$reportViewer$"
VIEW_REPORT_SUFFIX_RE = re.compile(r"reportViewer\$ctl08\$ctl00$")


def deduped_catalog() -> list[dict]:
    """Collapse duplicate paths (e.g. PickList is listed under both Work
    Orders and Closing) into one entry, keeping every category/name it's
    listed under."""
    by_path: dict[str, dict] = {}
    for entry in REPORT_CATALOG:
        existing = by_path.get(entry["path"])
        if existing:
            existing["also_listed_under"].append(f'{entry["category"]} > {entry["name"]}')
        else:
            by_path[entry["path"]] = {
                "category": entry["category"],
                "name": entry["name"],
                "path": entry["path"],
                "also_listed_under": [],
            }
    return list(by_path.values())


@dataclass
class ParamField:
    field_name: str
    field_id: str | None
    tag: str
    input_type: str | None
    label_guess: str | None
    current_value: str | None
    options: list[dict] = field(default_factory=list)


def find_parameter_container(soup: BeautifulSoup):
    """Locate the ReportViewer's parameter-area container. Confirmed live:
    the View Report button is named .../ctl08/ctl00 and ctl08's element id
    ends in "_ctl08" -- climb the button's ancestors until we find it."""
    button = soup.find("input", attrs={"name": VIEW_REPORT_SUFFIX_RE})
    if button is None:
        return None, None
    node = button
    while node is not None and not (node.get("id", "") if hasattr(node, "get") else "").endswith("_ctl08"):
        node = node.parent
    return node, button.get("name")


def extract_parameters(container, button_name: str) -> list[ParamField]:
    """Walk the container in document order, remembering the last non-empty
    text seen, and attach it as the label guess for the next form field."""
    results: list[ParamField] = []
    last_text: str | None = None
    for el in container.descendants:
        if isinstance(el, NavigableString):
            text = str(el).strip()
            if text:
                last_text = text
            continue
        name = getattr(el, "name", None)
        if name not in ("input", "select", "textarea"):
            continue
        field_name = el.get("name", "")
        if not field_name or field_name == button_name:
            continue
        options = []
        if name == "select":
            options = [
                {"value": o.get("value"), "text": o.get_text(strip=True)}
                for o in el.find_all("option")
            ]
        results.append(
            ParamField(
                field_name=field_name,
                field_id=el.get("id"),
                tag=name,
                input_type=el.get("type") if name == "input" else None,
                label_guess=last_text,
                current_value=el.get("value"),
                options=options,
            )
        )
    return results


def all_reportviewer_fields(soup: BeautifulSoup) -> list[dict]:
    """Fallback/cross-check: every ctl00$...$reportViewer$... form field
    anywhere on the page, regardless of container detection. Useful when
    find_parameter_container() can't find a ctl08 button at all."""
    out = []
    for el in soup.find_all(["input", "select", "textarea"]):
        name = el.get("name", "")
        if name.startswith(REPORTVIEWER_PREFIX):
            out.append({"field_name": name, "field_id": el.get("id"), "tag": el.name})
    return out


def inspect_report(session: requests.Session, path: str) -> dict:
    url = f"{BASE}{path}"
    resp = session.get(url)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    result: dict = {
        "path": path,
        "final_url": resp.url,
        "http_status": resp.status_code,
    }

    if "/Reports/" not in path:
        result["note"] = (
            "This path is not under /Reports/ like the rest of the catalog -- "
            "confirm by hand whether it's a ReportViewer page at all before "
            "trusting the parsed fields below."
        )

    container, button_name = find_parameter_container(soup)
    if container is None:
        result["reportviewer_button_found"] = False
        result["parameters"] = []
        result["all_reportviewer_fields_on_page"] = all_reportviewer_fields(soup)
        result["needs_manual_review"] = True
        result["review_reason"] = (
            "No ctl08$ctl00 'View Report' button found -- this page may not "
            "be a standard ReportViewer parameter page (no parameters at "
            "all, a completely different form, or a login/redirect landed "
            "here instead). See all_reportviewer_fields_on_page for any "
            "stray reportViewer fields, and inspect final_url/http_status."
        )
        return result

    fields_found = extract_parameters(container, button_name)
    result["reportviewer_button_found"] = True
    result["submit_button_field"] = button_name
    result["parameters"] = [
        {
            "field_name": f.field_name,
            "field_id": f.field_id,
            "tag": f.tag,
            "input_type": f.input_type,
            "label_guess": f.label_guess,
            "current_value": f.current_value,
            **({"options": f.options} if f.options else {}),
        }
        for f in fields_found
    ]
    # Textboxes ending in txtValue are the pattern confirmed for
    # WorkOrderList's date fields -- flag any parameter that DOESN'T match
    # that shape so it's obvious at a glance which reports need extra
    # attention (dropdowns, checkboxes, non-date text fields, etc).
    result["all_params_look_like_confirmed_date_pattern"] = all(
        f.tag == "input" and f.input_type == "text" and f.field_name.endswith("txtValue")
        for f in fields_found
    ) if fields_found else None
    result["needs_manual_review"] = not fields_found or not result["all_params_look_like_confirmed_date_pattern"]
    if not fields_found:
        result["review_reason"] = "View Report button found but no parameter fields under it -- this report may take zero parameters."
    elif not result["all_params_look_like_confirmed_date_pattern"]:
        result["review_reason"] = "At least one parameter isn't a plain text 'txtValue' field like the confirmed WorkOrderList date fields -- check label_guess/options by hand."
    else:
        result["review_reason"] = None
    return result


def load_existing(output_path: str) -> dict:
    if os.path.exists(output_path):
        with open(output_path, "r") as f:
            return json.load(f)
    return {}


def save(output_path: str, manifest: dict) -> None:
    with open(output_path, "w") as f:
        json.dump(manifest, f, indent=2)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Discover parameter fields for every Lifesaver report page.")
    parser.add_argument("--output", default="report_manifest.json", help="output JSON manifest path")
    parser.add_argument("--only", help="comma-separated list of paths or name substrings to limit to")
    parser.add_argument("--delay", type=float, default=1.5, help="seconds to sleep between requests (default 1.5)")
    parser.add_argument("--skip-existing", action="store_true", help="don't re-fetch paths already present in --output")
    parser.add_argument("--list", action="store_true", help="print the deduped catalog and exit (no requests)")
    args = parser.parse_args(argv)

    catalog = deduped_catalog()

    if args.list:
        for entry in catalog:
            extra = f"  (also: {', '.join(entry['also_listed_under'])})" if entry["also_listed_under"] else ""
            print(f"{entry['category']:<16} {entry['name']:<32} {entry['path']}{extra}")
        print(f"\n{len(catalog)} unique report paths.")
        return 0

    targets = catalog
    if args.only:
        needles = [n.strip().lower() for n in args.only.split(",")]
        targets = [
            e for e in catalog
            if any(n in e["path"].lower() or n in e["name"].lower() for n in needles)
        ]
        if not targets:
            parser.error(f"--only matched nothing in the catalog: {args.only!r}")

    username = os.environ.get("LIFESAVER_USERNAME")
    password = os.environ.get("LIFESAVER_PASSWORD")
    if not username or not password:
        parser.error("set LIFESAVER_USERNAME and LIFESAVER_PASSWORD environment variables")

    manifest = load_existing(args.output)

    if args.skip_existing:
        targets = [e for e in targets if e["path"] not in manifest]

    if not targets:
        print("Nothing to do (everything already in the manifest -- drop --skip-existing to re-fetch).")
        return 0

    session = requests.Session()
    lsp.login(session, username=username, password=password)

    review_count = 0
    for i, entry in enumerate(targets, 1):
        path = entry["path"]
        print(f"[{i}/{len(targets)}] {entry['category']} > {entry['name']}  ({path})", end=" ... ", flush=True)
        try:
            result = inspect_report(session, path)
            result["category"] = entry["category"]
            result["name"] = entry["name"]
            manifest[path] = result
            if result.get("needs_manual_review"):
                review_count += 1
                print(f"NEEDS REVIEW: {result.get('review_reason')}")
            else:
                param_names = [p["field_name"].rsplit("$", 1)[-1] for p in result["parameters"]]
                print(f"ok, params: {param_names}")
        except Exception as exc:  # noqa: BLE001 -- keep the batch going on any single failure
            manifest[path] = {
                "path": path,
                "category": entry["category"],
                "name": entry["name"],
                "error": str(exc),
                "needs_manual_review": True,
                "review_reason": f"Request/parse failed: {exc}",
            }
            review_count += 1
            print(f"ERROR: {exc}")

        save(args.output, manifest)  # write after every report so partial progress is never lost
        if i < len(targets):
            time.sleep(args.delay)

    print(f"\nDone. {len(targets)} reports inspected this run, {review_count} flagged for manual review.")
    print(f"Manifest written to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
