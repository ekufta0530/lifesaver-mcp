"""
Pull the full LifeSaver (lsscloud.com) customer list as CSV.

Wraps lifesaver_report_pull.pull_customer_export() -- see that function and
CUSTOMER_EXPORT_URL for how this was confirmed live: the "Customer Export"
report (/Reports/ConsumerInfoReport) exports real columns (firstName,
lastName, emailAddress, cellPhone, address*, firstPurchase, lastPurchase,
totalPurchase, ...) once its "Filter:" dropdown is set to "No filter, show
all customers." -- which also means the result isn't scoped to any date
range or invoice; it's every customer in the database, confirmed live as
672 rows spanning years with no truncation.

Usage:
    export LIFESAVER_USERNAME=...
    export LIFESAVER_PASSWORD=...
    python scripts/pull_customers.py --out customers.csv

    # or one of the narrower-column siblings, same shape:
    python scripts/pull_customers.py --report /Reports/CustomerInfoExport_Mailchimp --out customers_mailchimp.csv
"""

import argparse
import os
import sys

# Lives in scripts/ but imports the repo-root module; make that work when run
# as `python scripts/<name>.py` from anywhere.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

import lifesaver_report_pull as lsp  # noqa: E402

REPORT_CHOICES = [
    "/Reports/ConsumerInfoReport",
    "/Reports/CustomerInfoExport_Mailchimp",
    "/Reports/CustomerInfoExport_ConstantContact",
]


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pull the full LifeSaver customer list as CSV.")
    parser.add_argument("--out", default="customers.csv")
    parser.add_argument("--report", default=REPORT_CHOICES[0], choices=REPORT_CHOICES)
    args = parser.parse_args(argv)

    username = os.environ.get("LIFESAVER_USERNAME")
    password = os.environ.get("LIFESAVER_PASSWORD")
    if not username or not password:
        parser.error("set LIFESAVER_USERNAME and LIFESAVER_PASSWORD environment variables")

    session = lsp.requests.Session()
    lsp.login(session, username=username, password=password)
    try:
        csv_bytes = lsp.pull_customer_export(session, report_url=f"{lsp.BASE}{args.report}")
    finally:
        lsp.logout(session)  # release the single-session slot for the next run

    with open(args.out, "wb") as f:
        f.write(csv_bytes)

    print(f"Saved {args.out} ({len(csv_bytes)} bytes)")


if __name__ == "__main__":
    sys.exit(main())
