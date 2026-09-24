"""
Live recon: does any of the 3 customer-export report pages actually export
real per-customer data (name, email, phone, address, ...) as CSV, or -- like
every other report except WorkOrderList (see spec.md "Report coverage") --
does it export SSRS visual-layout internals (Textbox1, Textbox2, ...)
instead?

Candidates, all under Store Reporting -> Customer:
  /Reports/ConsumerInfoReport                  ("Customer Export")
  /Reports/CustomerInfoExport_ConstantContact  ("Constant Contact Export")
  /Reports/CustomerInfoExport_Mailchimp        ("Mailchimp Contact Export")

These were never tested in the original report-coverage sweep because their
parameter panel isn't the plain two-date-textbox shape that sweep covered --
they also have a "Customer Groups" text filter and a "Filter:" dropdown
(no filter/show all, by order date range, by order $, by order count, top N).
Being literally built to feed email-marketing tools, they're the more likely
place to find real name/email/phone/address columns than scraping invoice
PDFs (which, per a real sample pulled via pull_invoice(), only carry the
customer's name and cell -- no email, no address).

This script:
  1. GETs the report page.
  2. Finds the "Filter:" <select> by its option text (not a hardcoded
     ctl-number -- the field numbering differs slightly between the 3
     variants) and sets it to "No filter, show all customers."
  3. Leaves every other parameter at its default/blank value.
  4. POSTs back (reusing lifesaver_report_pull's postback machinery),
     scrapes ReportSession/ControlID, exports Format=CSV.
  5. Prints the CSV header + first few rows so it's obvious at a glance
     whether this is real customer data or Textbox-garbage.

Usage:
    export LIFESAVER_USERNAME=...
    export LIFESAVER_PASSWORD=...
    PYTHONPATH=. python scripts/inspect/verify_customer_export.py
    PYTHONPATH=. python scripts/inspect/verify_customer_export.py --report /Reports/CustomerInfoExport_Mailchimp
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys

from bs4 import BeautifulSoup

import lifesaver_report_pull as lsp

REPORT_CHOICES = [
    "/Reports/ConsumerInfoReport",
    "/Reports/CustomerInfoExport_Mailchimp",
    "/Reports/CustomerInfoExport_ConstantContact",
]


def find_filter_select_and_button(soup: BeautifulSoup):
    """Find the 'Filter:' <select> (by option text) and the View Report
    button's field name, without assuming a fixed ctl-number -- the field
    numbering shifts slightly between the 3 report variants."""
    button = soup.find("input", attrs={"name": lambda n: n and n.endswith("ctl08$ctl00")})
    filter_select = None
    for sel in soup.find_all("select"):
        texts = {o.get_text(strip=True).replace("\xa0", " ") for o in sel.find_all("option")}
        if any("No filter" in t and "show all customers" in t for t in texts):
            filter_select = sel
            break
    return filter_select, (button.get("name") if button else None)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", default=REPORT_CHOICES[0], choices=REPORT_CHOICES)
    parser.add_argument("--out", default="customer_export_sample.csv")
    args = parser.parse_args(argv)

    username = os.environ.get("LIFESAVER_USERNAME")
    password = os.environ.get("LIFESAVER_PASSWORD")
    if not username or not password:
        parser.error("set LIFESAVER_USERNAME and LIFESAVER_PASSWORD environment variables")

    report_url = f"{lsp.BASE}{args.report}"
    session = lsp.requests.Session()
    lsp.login(session, username=username, password=password)
    try:
        print(f"[1] GET {report_url}")
        soup = lsp.get_report_page(session, report_url)

        filter_select, button_name = find_filter_select_and_button(soup)
        if filter_select is None or button_name is None:
            print("    -> could not find the 'Filter:' dropdown and/or View Report "
                  "button on this page -- dumping raw HTML to debug_customer_export.html")
            with open("debug_customer_export.html", "w") as f:
                f.write(str(soup))
            return 1

        no_filter_value = None
        for opt in filter_select.find_all("option"):
            text = opt.get_text(strip=True).replace("\xa0", " ")
            if "No filter" in text and "show all customers" in text:
                no_filter_value = opt.get("value")
                break
        print(f"    -> found Filter select {filter_select.get('name')!r}, "
              f"'no filter' option value = {no_filter_value!r}")

        payload = lsp.extract_hidden_fields(soup)
        payload.update(lsp.extract_other_ctl_fields(soup))
        payload[filter_select.get("name")] = no_filter_value
        payload[button_name] = lsp.VIEW_REPORT_BUTTON_VALUE

        print("[2] POST back with Filter = 'No filter, show all customers.'")
        resp = session.post(report_url, data=payload)
        resp.raise_for_status()

        try:
            report_session, control_id, rsproxy = lsp.extract_report_session(resp.text)
        except ValueError as exc:
            print(f"    -> {exc}")
            with open("debug_customer_export_postback.html", "w") as f:
                f.write(resp.text)
            return 1
        print(f"    -> ReportSession={report_session}  ControlID={control_id}")

        print("[3] Exporting Format=CSV")
        csv_bytes = lsp.export_csv(
            session, report_session, control_id, rsproxy,
            file_name="CustomerExport", export_format="CSV",
        )
        with open(args.out, "wb") as f:
            f.write(csv_bytes)
        print(f"    -> wrote {len(csv_bytes)} bytes to {args.out}")

        text = csv_bytes.decode("utf-8-sig", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
        print(f"\n[4] Header row: {rows[0] if rows else '(empty)'}")
        for row in rows[1:4]:
            print(f"    row: {row}")

        header = rows[0] if rows else []
        looks_like_garbage = (
            not header
            or all(col.strip().lower().startswith("textbox") or not col.strip() for col in header)
        )

        if looks_like_garbage:
            print("\nRESULT: looks like SSRS visual-layout garbage (Textbox* columns / "
                  f"empty), same failure mode as the other non-WorkOrderList reports. "
                  f"See {args.out}.")
            return 1
        print(f"\nRESULT: looks like REAL column names -- check {args.out} by hand to "
              "confirm (name/email/phone/address present, real values, row count "
              "roughly matches your actual customer count).")
        return 0
    finally:
        lsp.logout(session)


if __name__ == "__main__":
    sys.exit(main())
