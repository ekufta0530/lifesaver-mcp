"""
Lifesaver Software (lsscloud.com) automated report pull.

This app is classic ASP.NET Web Forms hosting a Microsoft SSRS ReportViewer
web control, wrapped in a thin MVC shell (there's a POST /Account/Login and
an MVC-style __RequestVerificationToken floating around on authenticated
pages, but the report page itself is pure Web Forms postback/viewstate).

Everything below was confirmed live against the "Work Order List" report
(Store Reporting -> Work Orders -> Work Order List, which lives at
/Reports/WorkOrderList) using an already-authenticated browser session and
DevTools/JS introspection. Field names, the export query-string shape, and
the login form fields are all real, not guesses. Two things changed from
the original skeleton:

  1. ControlID is NOT stable -- it's minted fresh per page load/session
     (confirmed two different values across two different page loads), so
     it must be scraped out of the postback response every time, never
     hardcoded.
  2. ReportSession/ControlID don't show up as `ReportSession: "xxx"` /
     `ReportSession="xxx"` anywhere. They show up as plain query-string
     pairs (`ReportSession=xxx&Culture=...&ControlID=yyy&RSProxy=...`)
     inside the `src` of an <img>/<iframe> the postback response injects
     to render the report body. The regex has been changed to match that
     shape.

Flow:
  1. POST username/password to /Account/Login (plain form fields, no
     hidden CSRF token on that particular form -- session-cookie auth).
  2. GET the report page to harvest __VIEWSTATE / __VIEWSTATEGENERATOR /
     __EVENTVALIDATION / __RequestVerificationToken and the other
     ctl00$... hidden fields that must be echoed back verbatim.
  3. POST back to the same page with the date-range params PLUS the
     "View Report" submit button's own name=value pair (a plain
     <input type=submit>, so the server tells which button fired from
     that pair, not from __EVENTTARGET/__EVENTARGUMENT).
  4. Scrape ReportSession + ControlID out of that response's embedded
     image/iframe src.
  5. GET the export URL with those values to pull the CSV. Format=CSV
     export returns the FULL result set, not just the currently-displayed
     page of the paginated on-screen viewer (SSRS export always ignores
     on-screen pagination).

Invoices (pull_invoice()) are a separate, simpler flow confirmed live
2026-09-17 against /Reports/Invoice/ -- no postback needed. A plain GET with
StoreId + InvoiceId (the same "Invoice #" WorkOrderList already returns) in
the querystring renders the report and embeds a fresh ReportSession/ControlID
directly in that response, so steps 2-3 above collapse into one GET. See
get_invoice_report_session() and scripts/inspect/verify_invoice_endpoint.py
(the script used to confirm this). For bulk-pulling invoices as PDFs, see
scripts/pull_invoices.py.

Still unverified (couldn't test without real creds / without disrupting
the logged-in session used for recon):
  - The exact shape of a *failed* login response (redirect vs. re-rendered
    form) -- adjust `login()`'s success check once you've seen it.
  - Whether a very large report needs a short poll
    (OpType=SessionKeepAlive) before Export is safe to call immediately
    after the postback. Traffic showed the viewer polling
    SessionKeepAlive periodically while idle; for a report this size it
    wasn't required before export, but if you see truncated/HTML-error
    output instead of CSV on a bigger date range, add a short retry loop
    around export_csv().
"""

import argparse
import json
import os
import re
import sys

import requests
from bs4 import BeautifulSoup

BASE = "https://lsscloud.com"
LOGIN_URL = f"{BASE}/Account/Login"
REPORT_PAGE_URL = f"{BASE}/Reports/WorkOrderList"
EXPORT_PATH = f"{BASE}/Reserved.ReportViewerWebControl.axd"

# Confirmed live (2026-09-17, invoice #584): the "Find Invoice" report catalog
# entry (/Reporting/ReprintInvoice) is a plain search box, not a ReportViewer
# parameter page -- entering an invoice number there loads the actual report
# in an iframe pointing here, with the invoice # passed as a querystring
# param. Unlike WorkOrderList, no postback is needed: a plain GET with
# StoreId/InvoiceId already renders the report and embeds a fresh
# ReportSession/ControlID in the response, same as the WorkOrderList postback
# response does. See scripts/inspect/verify_invoice_endpoint.py.
INVOICE_REPORT_URL = f"{BASE}/Reports/Invoice/"
DEFAULT_STORE_ID = "6006"

# Confirmed live (2026-09-17): unlike every other non-WorkOrderList report
# (see "Report coverage" in spec.md), this one exports real per-customer
# columns (firstName/lastName/emailAddress/cellPhone/address*/firstPurchase/
# lastPurchase/totalPurchase/...) as CSV -- once its "Filter:" dropdown is
# set to "No filter, show all customers.", which also makes it return every
# customer regardless of order date, not just customers within some date
# range (confirmed: 672 rows, no truncation, dates spanning years).
# CustomerInfoExport_Mailchimp / _ConstantContact share the same shape if a
# narrower column set (built for those specific integrations) is ever wanted
# instead.
CUSTOMER_EXPORT_URL = f"{BASE}/Reports/ConsumerInfoReport"
CUSTOMER_EXPORT_NO_FILTER_MARKERS = ("No filter", "show all customers")

# Confirmed live (2026-09-17): the InvoiceId that /Reports/Invoice/ actually
# needs is NOT the human-readable "Invoice #" (WorkOrderList's invoiceNumber
# column) -- it's a separate internal id, resolved via this endpoint first.
# Skipping this step doesn't fail loudly: /Reports/Invoice/ still returns 200
# with a valid-looking ReportSession/ControlID and a well-formed PDF, but
# every field is unbound (Order Date/Last Revised show 1/1/0001 --
# DateTime.MinValue -- "Invoice #" itself shows #Error, all amounts $0.00).
# See resolve_invoice_id().
RESOLVE_INVOICE_ID_URL = f"{BASE}/Invoice/GetStoreInvoiceId/"

# Confirmed live: the RS backend this org's ReportViewer proxies to. This is
# tied to the customer's on-prem SQL/reporting box, not to a session, so it's
# safe to keep as a constant -- but we also try to scrape it fresh below in
# case it ever differs per store/session.
DEFAULT_RSPROXY = "http://lifesaver-sql1.corp.lifesaversoft.com/reportserver"

# Confirmed field names on the report page's <form> (master page
# ctl00$ContentPlaceHolder1$reportViewer$... naming). These are stable across
# sessions -- it's the *values*, not the names, that regenerate each time.
START_DATE_FIELD = "ctl00$ContentPlaceHolder1$reportViewer$ctl08$ctl03$txtValue"
END_DATE_FIELD = "ctl00$ContentPlaceHolder1$reportViewer$ctl08$ctl05$txtValue"
VIEW_REPORT_BUTTON_FIELD = "ctl00$ContentPlaceHolder1$reportViewer$ctl08$ctl00"
VIEW_REPORT_BUTTON_VALUE = "View Report"


def login(session: requests.Session, username: str, password: str) -> requests.Response:
    """
    Confirmed live: /Account/Login's form has exactly two named inputs --
    UserName (text) and Password (password) -- plus an unnamed submit button.
    No hidden __RequestVerificationToken on this form, so it's a plain POST.

    Confirmed live (the hard way): LifeSaver enforces ONE active session per
    user (license-limited). If a previous run didn't log out, the POST comes
    back 200 still on /Account/Login, with an embedded

        var loginModel = {"Errors":["UserAlreadyLoggedIn"],
                          "ActiveSessions":[{"UniqueID":"...","UserAccount":{...}}],
                          "UserId":14648, ...};

    Clearing it = POST /Account/TerminateSession/ with JSON
    {"Session": <UniqueID>, "UserId": <UserId>}. We only ever terminate a
    session whose UserAccount.UserName is our own, then retry the login once.
    Call logout() when done so this doesn't happen next time.
    """
    resp = session.post(LOGIN_URL, data={"UserName": username, "Password": password})
    resp.raise_for_status()
    if "/Account/Login" not in resp.url:
        return resp

    m = re.search(r"var loginModel = (\{.*?\});", resp.text, re.S)
    if not m:
        raise RuntimeError(
            "Still on the login page after POST and no loginModel found -- "
            "login likely failed. Inspect resp.text for the error message."
        )
    model = json.loads(m.group(1))
    mine = [
        s for s in (model.get("ActiveSessions") or [])
        if (s.get("UserAccount") or {}).get("UserName", "").lower() == username.lower()
    ]
    if not mine:
        raise RuntimeError(f"Login rejected: {model.get('Errors')}")

    for s in mine:
        t = session.post(
            f"{BASE}/Account/TerminateSession/",
            data=json.dumps({"Session": s["UniqueID"], "UserId": model.get("UserId")}),
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        t.raise_for_status()

    resp = session.post(LOGIN_URL, data={"UserName": username, "Password": password})
    resp.raise_for_status()
    if "/Account/Login" in resp.url:
        raise RuntimeError("Still blocked after terminating our own session(s).")
    return resp


def logout(session: requests.Session) -> None:
    """Best-effort: release the server-side session (avoids the one-session block
    on the next run). LogOff is a plain GET on this app."""
    try:
        session.get(f"{BASE}/Account/LogOff", timeout=30)
    except requests.RequestException:
        pass


def get_report_page(session: requests.Session, report_url: str = REPORT_PAGE_URL) -> BeautifulSoup:
    resp = session.get(report_url)
    resp.raise_for_status()
    return BeautifulSoup(resp.text, "html.parser")


def extract_hidden_fields(soup: BeautifulSoup) -> dict:
    fields = {}
    for name in (
        "__VIEWSTATE",
        "__VIEWSTATEGENERATOR",
        "__EVENTVALIDATION",
        "__EVENTTARGET",
        "__EVENTARGUMENT",
        "__RequestVerificationToken",  # MVC anti-forgery token present on this page even though the report control itself is Web Forms
    ):
        tag = soup.find("input", {"name": name})
        if tag and tag.get("value") is not None:
            fields[name] = tag["value"]
    return fields


def extract_other_ctl_fields(soup: BeautifulSoup) -> dict:
    """
    Grab the other ctl00$... hidden/form fields from the page so we can echo
    them back unchanged in the postback, same as the browser does.
    """
    fields = {}
    for tag in soup.find_all("input"):
        name = tag.get("name", "")
        if name.startswith("ctl00$") and tag.get("value") is not None:
            fields[name] = tag["value"]
    return fields


def submit_date_range(
    session: requests.Session,
    soup: BeautifulSoup,
    start_date: str,  # e.g. "9/1/2025" -- M/d/yyyy, no zero-padding, confirmed against the live textbox value
    end_date: str,    # e.g. "9/1/2026"
    report_url: str = REPORT_PAGE_URL,
) -> str:
    payload = extract_hidden_fields(soup)
    payload.update(extract_other_ctl_fields(soup))

    payload[START_DATE_FIELD] = start_date
    payload[END_DATE_FIELD] = end_date

    # Mimic actually clicking "View Report" -- it's a plain <input
    # type=submit>, so its own name=value pair is what tells the server
    # which button fired (there's no __EVENTTARGET for it).
    payload[VIEW_REPORT_BUTTON_FIELD] = VIEW_REPORT_BUTTON_VALUE

    resp = session.post(report_url, data=payload)
    resp.raise_for_status()
    return resp.text


def extract_report_session(html: str) -> tuple[str, str, str]:
    """
    Confirmed live: after the postback, the response embeds an <img>/<iframe>
    whose src is a plain query string against Reserved.ReportViewerWebControl.axd,
    e.g. ...axd?ReportSession=2cmn1ovaxf1tl2rpxj0ojb45&Culture=1033&...&ControlID=4034d0674...&RSProxy=http%3a%2f%2f...

    Returns (report_session, control_id, rsproxy).
    """
    session_match = re.search(r"ReportSession=([a-zA-Z0-9]+)", html)
    control_match = re.search(r"ControlID=([a-fA-F0-9]+)", html)
    rsproxy_match = re.search(r"RSProxy=([^&\"'\s]+)", html)

    if not session_match or not control_match:
        raise ValueError(
            "Could not find ReportSession/ControlID in postback response -- "
            "save the HTML (`open('debug.html','w').write(html)`) and check "
            "whether the report actually rendered (e.g. a validation error "
            "on the date fields would skip straight past rendering)."
        )

    report_session = session_match.group(1)
    control_id = control_match.group(1)
    rsproxy = (
        requests.utils.unquote(rsproxy_match.group(1))
        if rsproxy_match
        else DEFAULT_RSPROXY
    )
    return report_session, control_id, rsproxy


def export_csv(
    session: requests.Session,
    report_session: str,
    control_id: str,
    rsproxy: str = DEFAULT_RSPROXY,
    file_name: str = "LifeSaver Reports",
    export_format: str = "CSV",
) -> bytes:
    """
    Despite the name (kept for backward compatibility -- this is the
    original WorkOrderList CSV puller), this works for any SSRS export
    format via export_format: "CSV", "PDF" (used by pull_invoice()),
    "EXCELOPENXML", etc.
    """
    params = {
        "ReportSession": report_session,
        "Culture": "1033",
        "CultureOverrides": "True",
        "UICulture": "1033",
        "UICultureOverrides": "True",
        "ReportStack": "1",
        "ControlID": control_id,
        "RSProxy": rsproxy,
        "OpType": "Export",
        "FileName": file_name,
        "ContentDisposition": "OnlyHtmlInline",
        "Format": export_format,
    }
    resp = session.get(EXPORT_PATH, params=params)
    resp.raise_for_status()
    # Confirmed live: clicking this in a real browser triggers a genuine
    # file download (Content-Disposition: attachment) rather than an XHR --
    # that's just browser UI behavior and doesn't matter here, resp.content
    # is the raw bytes either way (CSV text, PDF binary, etc, depending on
    # export_format).
    return resp.content


def pull_report(
    session: requests.Session,
    start_date: str,
    end_date: str,
    report_url: str = REPORT_PAGE_URL,
    file_name: str = "LifeSaver Reports",
) -> bytes:
    """
    Convenience wrapper: GET page -> postback date range -> export CSV.
    Same pattern should work for the other Store Reporting pages
    (Payments, Orders, Paid In Full, Ticket Sales, Customer, Production,
    Material Usage, Invoices, Closing, Inventory, Tax Exempt, Store Admin)
    since they all share the same ctl00$ContentPlaceHolder1$reportViewer
    master-page naming -- just point report_url at the right
    /Reports/<Whatever> path and confirm that report's date-field IDs
    match ctl08$ctl03/ctl08$ctl05 (worth a quick DevTools check per report;
    some reports may have more/different parameters than just a date range).
    """
    soup = get_report_page(session, report_url)
    html = submit_date_range(session, soup, start_date, end_date, report_url)
    report_session, control_id, rsproxy = extract_report_session(html)
    return export_csv(session, report_session, control_id, rsproxy, file_name)


def resolve_invoice_id(session: requests.Session, invoice_number: str) -> str:
    """
    Confirmed live (2026-09-17): POSTing {"invoiceId": "<human Invoice #>"}
    as JSON to RESOLVE_INVOICE_ID_URL resolves it to the internal id
    /Reports/Invoice/ actually needs (e.g. human invoice #584 -> internal
    10044999). The exact response-JSON key wasn't captured before the source
    session expired, so this tries the likely candidates and raises with the
    raw body if none match -- if you hit that error, check the real key in
    the response and add it to the `key in (...)` tuple below.
    """
    resp = session.post(RESOLVE_INVOICE_ID_URL, json={"invoiceId": str(invoice_number)})
    resp.raise_for_status()
    data = resp.json()
    for key in (
        "storeInvoiceId", "StoreInvoiceId", "invoiceId", "InvoiceId", "id", "Id",
    ):
        if isinstance(data, dict) and data.get(key) not in (None, ""):
            return str(data[key])
    raise ValueError(
        f"Could not find a resolved invoice id in the GetStoreInvoiceId "
        f"response for invoice {invoice_number!r} -- raw response: {data!r}. "
        f"Inspect this in DevTools and add the right key to resolve_invoice_id()."
    )


def get_invoice_report_session(
    session: requests.Session,
    resolved_invoice_id: str,
    store_id: str = DEFAULT_STORE_ID,
) -> tuple[str, str, str]:
    """
    Confirmed live (2026-09-17, invoice #584): a plain GET on
    INVOICE_REPORT_URL with StoreId/InvoiceId in the querystring renders the
    invoice immediately -- no postback needed, unlike WorkOrderList. The
    ReportSession/ControlID are already embedded in this response, same
    regex shape as the WorkOrderList postback response.

    `resolved_invoice_id` must already be the internal id from
    resolve_invoice_id() -- NOT the human-readable "Invoice #" -- or the
    report renders its empty-state template (see RESOLVE_INVOICE_ID_URL).
    """
    resp = session.get(
        INVOICE_REPORT_URL, params={"StoreId": store_id, "InvoiceId": resolved_invoice_id}
    )
    resp.raise_for_status()
    return extract_report_session(resp.text)


def pull_invoice(
    session: requests.Session,
    invoice_number: str,
    store_id: str = DEFAULT_STORE_ID,
    file_name: str = "Invoice",
) -> bytes:
    """
    Convenience wrapper: resolve the human Invoice # to the internal id ->
    GET the invoice page -> export as PDF.
    """
    resolved_id = resolve_invoice_id(session, invoice_number)
    report_session, control_id, rsproxy = get_invoice_report_session(
        session, resolved_id, store_id
    )
    return export_csv(
        session, report_session, control_id, rsproxy,
        file_name=file_name, export_format="PDF",
    )


def find_customer_export_filter_field(soup: BeautifulSoup) -> tuple[str, str, str]:
    """
    The "Filter:" dropdown's field name isn't a stable ctl-number -- it
    shifts slightly between CUSTOMER_EXPORT_URL and its ConstantContact/
    Mailchimp siblings (Customer Groups/Filter/dates get renumbered when
    ConsumerInfoReport's two extra fields -- "Show these fields" and the
    email-only radio -- aren't present). Found by matching option text
    instead, same as confirmed live in
    scripts/inspect/verify_customer_export.py.

    Returns (filter_field_name, no_filter_option_value, submit_button_field_name).
    """
    button = soup.find("input", attrs={"name": lambda n: n and n.endswith("ctl08$ctl00")})
    if button is None:
        raise ValueError("No View Report button found on the customer export page.")

    for select in soup.find_all("select"):
        for option in select.find_all("option"):
            text = option.get_text(strip=True).replace("\xa0", " ")
            if all(marker in text for marker in CUSTOMER_EXPORT_NO_FILTER_MARKERS):
                return select.get("name"), option.get("value"), button.get("name")

    raise ValueError(
        "Could not find the 'Filter:' dropdown's 'No filter, show all "
        "customers.' option on the customer export page."
    )


def pull_customer_export(
    session: requests.Session,
    report_url: str = CUSTOMER_EXPORT_URL,
    file_name: str = "CustomerExport",
) -> bytes:
    """
    Pull the full customer list as CSV -- every customer, regardless of
    order history (see CUSTOMER_EXPORT_URL / find_customer_export_filter_field()).
    """
    soup = get_report_page(session, report_url)
    filter_field, no_filter_value, button_field = find_customer_export_filter_field(soup)

    payload = extract_hidden_fields(soup)
    payload.update(extract_other_ctl_fields(soup))
    payload[filter_field] = no_filter_value
    payload[button_field] = VIEW_REPORT_BUTTON_VALUE

    resp = session.post(report_url, data=payload)
    resp.raise_for_status()
    report_session, control_id, rsproxy = extract_report_session(resp.text)
    return export_csv(session, report_session, control_id, rsproxy, file_name)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Pull a LifeSaver (lsscloud.com) report to CSV.",
    )
    parser.add_argument("--start", required=True, help="start date, M/d/yyyy e.g. 7/1/2025")
    parser.add_argument("--end", required=True, help="end date, M/d/yyyy e.g. 9/1/2026")
    parser.add_argument("--out", default="workorderlist.csv", help="output CSV path")
    parser.add_argument(
        "--report-url",
        default=REPORT_PAGE_URL,
        help=f"report page URL (default: {REPORT_PAGE_URL})",
    )
    args = parser.parse_args(argv)

    username = os.environ.get("LIFESAVER_USERNAME")
    password = os.environ.get("LIFESAVER_PASSWORD")
    if not username or not password:
        parser.error(
            "set LIFESAVER_USERNAME and LIFESAVER_PASSWORD environment variables"
        )

    session = requests.Session()
    login(session, username=username, password=password)
    try:
        csv_bytes = pull_report(
            session, start_date=args.start, end_date=args.end, report_url=args.report_url,
        )
    finally:
        logout(session)  # release the single-session slot for the next run

    with open(args.out, "wb") as f:
        f.write(csv_bytes)

    print(f"Saved {args.out} ({len(csv_bytes)} bytes)")


if __name__ == "__main__":
    sys.exit(main())
