"""
Live verification for the invoice resolve -> render -> export chain.

Follow-up to the original /Reports/Invoice/ check (confirmed a plain GET
renders the report, no postback needed -- see spec.md). That first pass
missed a step: hitting /Reports/Invoice/ with the *human* Invoice # (the
"Invoice #" WorkOrderList already returns) renders successfully -- 200,
valid-looking ReportSession/ControlID, well-formed PDF -- but every field is
unbound: Order Date/Last Revised show 1/1/0001 (.NET DateTime.MinValue),
"Invoice #" itself shows #Error, every amount is $0.00. That's the report's
empty-state template, not a real invoice.

Root cause, confirmed via live network capture: the InvoiceId that
/Reports/Invoice/ actually needs is a separate internal id, resolved by
POSTing the human Invoice # as JSON to /Invoice/GetStoreInvoiceId/ first
(e.g. human #584 -> internal 10044999). lifesaver_report_pull.py now has
resolve_invoice_id() for this, with a best-guess list of response JSON keys
to check (the exact key wasn't captured live before the source session
expired) -- this script prints the raw response so that list can be
confirmed/corrected if it doesn't match.

Usage (run from the repo root -- PYTHONPATH=. is needed so the script can
import lifesaver_report_pull, since Python puts the script's own directory,
not the cwd, on sys.path by default):
    export LIFESAVER_USERNAME=...
    export LIFESAVER_PASSWORD=...
    PYTHONPATH=. python scripts/inspect/verify_invoice_endpoint.py --invoice 584
"""

from __future__ import annotations

import argparse
import os
import sys

import lifesaver_report_pull as lsp


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--invoice", required=True, help="a known-good human Invoice # to test against")
    parser.add_argument("--store-id", default=lsp.DEFAULT_STORE_ID)
    parser.add_argument("--out", default="verify_invoice.pdf")
    args = parser.parse_args(argv)

    username = os.environ.get("LIFESAVER_USERNAME")
    password = os.environ.get("LIFESAVER_PASSWORD")
    if not username or not password:
        parser.error("set LIFESAVER_USERNAME and LIFESAVER_PASSWORD environment variables")

    session = lsp.requests.Session()
    lsp.login(session, username=username, password=password)
    try:
        print(f"[1] Resolving human Invoice #{args.invoice} via {lsp.RESOLVE_INVOICE_ID_URL}")
        resp = session.post(lsp.RESOLVE_INVOICE_ID_URL, json={"invoiceId": str(args.invoice)})
        resp.raise_for_status()
        raw = resp.json()
        print(f"    -> raw response: {raw!r}")

        try:
            resolved_id = lsp.resolve_invoice_id(session, args.invoice)
        except ValueError as exc:
            print(f"\nRESULT: FAIL -- {exc}")
            print("Fix: add the real key (shown in the raw response above) to the "
                  "key tuple in resolve_invoice_id(), then re-run this script.")
            return 1
        print(f"    -> resolve_invoice_id() parsed: {resolved_id}")

        print(f"[2] GET {lsp.INVOICE_REPORT_URL}?StoreId={args.store_id}&InvoiceId={resolved_id}")
        report_session, control_id, rsproxy = lsp.get_invoice_report_session(
            session, resolved_id, args.store_id
        )
        print(f"    -> ReportSession={report_session}  ControlID={control_id}")

        print("[3] Exporting Format=PDF")
        pdf_bytes = lsp.export_csv(
            session, report_session, control_id, rsproxy,
            file_name="Invoice", export_format="PDF",
        )
        with open(args.out, "wb") as f:
            f.write(pdf_bytes)
        is_pdf = pdf_bytes[:5] == b"%PDF-"
        print(f"    -> wrote {len(pdf_bytes)} bytes to {args.out}, starts with %PDF- magic bytes: {is_pdf}")

        if not is_pdf:
            print("\nRESULT: FAIL -- export didn't return a real PDF.")
            return 1

        print(f"\nRESULT: resolve -> render -> export chain completed without errors.")
        print(f"IMPORTANT: open {args.out} and eyeball it. A well-formed PDF isn't enough --")
        print("confirm it shows a real customer name, real dates, and nonzero amounts,")
        print("not '1/1/0001' dates / '#Error' / all-$0.00 (that was the previous, silent")
        print("failure mode: a structurally valid PDF of the report's empty-state template).")
        return 0
    finally:
        lsp.logout(session)


if __name__ == "__main__":
    sys.exit(main())
