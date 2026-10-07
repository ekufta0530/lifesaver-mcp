"""
Bulk-pull LifeSaver (lsscloud.com) customer invoices as PDFs.

Builds on lifesaver_report_pull.pull_invoice() (one login, one session,
looping over invoice numbers) -- see that module's docstring and
get_invoice_report_session() for how the underlying /Reports/Invoice/ flow
was confirmed live (plain GET, no postback needed).

Invoice numbers come from either:
  --csv <path>   a CSV with an "invoiceNumber" column -- the same column
                 name lifesaver/parser.py already maps to invoice_number,
                 so this works directly against a lifesaver_report_pull.py
                 WorkOrderList export or a CSV dump of the
                 get_work_order_list_report MCP tool's rows. Duplicates and
                 blanks are dropped, order preserved.
  --ids          a comma-separated list of invoice numbers, for a manual/
                 ad-hoc pull.

Politeness/resilience pattern matches cloud/scripts/inspect/inspect_report.py:
a delay between requests (--delay, default 1.5s), one invoice failing
doesn't stop the batch, and each PDF is written to disk as soon as it's
pulled so a crash partway through doesn't lose what's already done.

Usage:
    export LIFESAVER_USERNAME=...
    export LIFESAVER_PASSWORD=...

    python cloud/scripts/pull_invoices.py --csv workorderlist.csv --out-dir invoices
    python cloud/scripts/pull_invoices.py --ids 584,591,602 --out-dir invoices
"""

import argparse
import csv
import os
import sys
import time

# Lives in scripts/ but imports the repo-root module; make that work when run
# as `python scripts/<name>.py` from anywhere.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import lifesaver_report_pull as lsp  # noqa: E402


def load_invoice_ids_from_csv(path: str, column: str = "invoiceNumber") -> list[str]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        if not reader.fieldnames or column not in reader.fieldnames:
            raise ValueError(
                f"column {column!r} not found in {path} -- header was "
                f"{reader.fieldnames!r}"
            )
        seen: set[str] = set()
        ids: list[str] = []
        for row in reader:
            value = (row.get(column) or "").strip()
            if value and value not in seen:
                seen.add(value)
                ids.append(value)
    return ids


def bulk_pull_invoices(
    session,
    invoice_ids: list[str],
    out_dir: str,
    store_id: str = lsp.DEFAULT_STORE_ID,
    delay: float = 1.5,
) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    ok: list[str] = []
    failed: list[tuple[str, str]] = []

    for i, invoice_id in enumerate(invoice_ids, 1):
        out_path = os.path.join(out_dir, f"invoice_{invoice_id}.pdf")
        print(f"[{i}/{len(invoice_ids)}] invoice {invoice_id} ...", end=" ", flush=True)
        try:
            pdf_bytes = lsp.pull_invoice(session, invoice_id, store_id=store_id)
            with open(out_path, "wb") as f:
                f.write(pdf_bytes)
            print(f"ok, {len(pdf_bytes)} bytes -> {out_path}")
            ok.append(invoice_id)
        except Exception as exc:  # noqa: BLE001 -- keep the batch going on any single failure
            print(f"ERROR: {exc}")
            failed.append((invoice_id, str(exc)))
        if i < len(invoice_ids):
            time.sleep(delay)

    return {"ok": ok, "failed": failed}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bulk-pull LifeSaver invoices as PDFs.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--csv", help="CSV file with an 'invoiceNumber' column")
    source.add_argument("--ids", help="comma-separated invoice numbers")
    parser.add_argument("--out-dir", default="invoices", help="directory for invoice_<id>.pdf files (default: invoices)")
    parser.add_argument("--store-id", default=lsp.DEFAULT_STORE_ID)
    parser.add_argument("--delay", type=float, default=1.5, help="seconds between requests (default 1.5)")
    args = parser.parse_args(argv)

    if args.csv:
        invoice_ids = load_invoice_ids_from_csv(args.csv)
    else:
        invoice_ids = [x.strip() for x in args.ids.split(",") if x.strip()]

    if not invoice_ids:
        parser.error("no invoice numbers to pull")

    username = os.environ.get("LIFESAVER_USERNAME")
    password = os.environ.get("LIFESAVER_PASSWORD")
    if not username or not password:
        parser.error("set LIFESAVER_USERNAME and LIFESAVER_PASSWORD environment variables")

    session = lsp.requests.Session()
    lsp.login(session, username=username, password=password)
    try:
        results = bulk_pull_invoices(
            session, invoice_ids, out_dir=args.out_dir, store_id=args.store_id, delay=args.delay,
        )
    finally:
        lsp.logout(session)  # release the single-session slot for the next run

    print(f"\nDone. {len(results['ok'])} ok, {len(results['failed'])} failed.")
    if results["failed"]:
        print("Failed:")
        for invoice_id, err in results["failed"]:
            print(f"  {invoice_id}: {err}")

    return 1 if results["failed"] and not results["ok"] else 0


if __name__ == "__main__":
    sys.exit(main())
