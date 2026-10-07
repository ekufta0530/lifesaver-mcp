"""
Offline tests for lifesaver_report_pull.

These exercise the pure parsing/payload-building logic against saved HTML
fixtures and a fake requests session. No network, no credentials.

What is intentionally NOT covered here (needs a live run against lsscloud.com):
  - real login success/failure response shapes
  - whether the real postback embeds ReportSession in exactly this shape
  - whether the CSV export needs a SessionKeepAlive poll first
  - whether the exported CSV is clean / parseable as-is
See the module docstring in lifesaver_report_pull.py.
"""

import pytest
from bs4 import BeautifulSoup

import lifesaver_report_pull as lrp
from conftest import FakeResponse, load_fixture


@pytest.fixture
def report_soup():
    return BeautifulSoup(load_fixture("report_page.html"), "html.parser")


@pytest.fixture
def customer_export_soup():
    return BeautifulSoup(load_fixture("customer_export_page.html"), "html.parser")


# --- hidden field scraping ---------------------------------------------------

def test_extract_hidden_fields(report_soup):
    fields = lrp.extract_hidden_fields(report_soup)
    assert fields["__VIEWSTATEGENERATOR"] == "CA0B0334"
    assert fields["__VIEWSTATE"].startswith("/wEPDwUK")
    assert fields["__EVENTVALIDATION"]
    assert fields["__EVENTTARGET"] == ""
    assert fields["__RequestVerificationToken"] == "AF-mvc-antiforgery-token-0987654321"


def test_extract_other_ctl_fields_only_collects_ctl00(report_soup):
    fields = lrp.extract_other_ctl_fields(report_soup)
    assert all(name.startswith("ctl00$") for name in fields)
    assert "someOtherField" not in fields
    # the date + submit fields live under ctl00$ and get picked up here too
    assert lrp.START_DATE_FIELD in fields
    assert lrp.END_DATE_FIELD in fields
    assert lrp.VIEW_REPORT_BUTTON_FIELD in fields


# --- postback payload ------------------------------------------------------

def test_submit_date_range_builds_expected_payload(fake_session, report_soup):
    fake_session.next_response = FakeResponse(text="<html>postback</html>")

    html = lrp.submit_date_range(
        fake_session, report_soup, "9/1/2025", "9/1/2026",
    )
    assert html == "<html>postback</html>"

    method, url, kw = fake_session.calls[-1]
    assert method == "POST"
    assert url == lrp.REPORT_PAGE_URL
    data = kw["data"]
    assert data[lrp.START_DATE_FIELD] == "9/1/2025"
    assert data[lrp.END_DATE_FIELD] == "9/1/2026"
    assert data[lrp.VIEW_REPORT_BUTTON_FIELD] == lrp.VIEW_REPORT_BUTTON_VALUE
    # hidden Web Forms state must be echoed back
    assert data["__VIEWSTATE"].startswith("/wEPDwUK")
    assert data["__EVENTVALIDATION"]


# --- ReportSession / ControlID scraping -----------------------------------

def test_extract_report_session_ok():
    html = load_fixture("postback_ok.html")
    session_id, control_id, rsproxy = lrp.extract_report_session(html)
    assert session_id == "2cmn1ovaxf1tl2rpxj0ojb45"
    assert control_id == "4034d0674abc4e9f9b2c1d5e6f7a8b90"
    # RSProxy comes back URL-decoded
    assert rsproxy == "http://lifesaver-sql1.corp.lifesaversoft.com/reportserver"


def test_extract_report_session_falls_back_to_default_rsproxy():
    html = (
        '<img src="/Reserved.ReportViewerWebControl.axd'
        '?ReportSession=abc123&ControlID=deadbeef&OpType=ReportImage" />'
    )
    _, _, rsproxy = lrp.extract_report_session(html)
    assert rsproxy == lrp.DEFAULT_RSPROXY


def test_extract_report_session_raises_on_validation_error():
    html = load_fixture("postback_validation_error.html")
    with pytest.raises(ValueError):
        lrp.extract_report_session(html)


# --- login ---------------------------------------------------------------

def test_login_raises_when_still_on_login_page(fake_session):
    fake_session.next_response = FakeResponse(
        text="<html>bad login</html>",
        url="https://lsscloud.com/Account/Login?ReturnUrl=%2f",
    )
    with pytest.raises(RuntimeError):
        lrp.login(fake_session, "u", "p")


def test_login_ok_when_redirected_away(fake_session):
    fake_session.next_response = FakeResponse(
        text="<html>home</html>", url="https://lsscloud.com/Home",
    )
    resp = lrp.login(fake_session, "u", "p")
    assert resp.url.endswith("/Home")
    method, url, kw = fake_session.calls[-1]
    assert (method, url) == ("POST", lrp.LOGIN_URL)
    assert kw["data"] == {"UserName": "u", "Password": "p"}


# --- export ------------------------------------------------------------------

def test_export_csv_builds_expected_params(fake_session):
    fake_session.next_response = FakeResponse(content=b"col1,col2\n1,2\n")
    out = lrp.export_csv(fake_session, "sess42", "ctrl42", rsproxy="http://rs/x")
    assert out == b"col1,col2\n1,2\n"

    method, url, kw = fake_session.calls[-1]
    assert method == "GET"
    assert url == lrp.EXPORT_PATH
    params = kw["params"]
    assert params["ReportSession"] == "sess42"
    assert params["ControlID"] == "ctrl42"
    assert params["RSProxy"] == "http://rs/x"
    assert params["Format"] == "CSV"
    assert params["OpType"] == "Export"


# --- full wiring -----------------------------------------------------------

def test_pull_report_wires_steps_together(fake_session):
    fake_session.responses = [
        FakeResponse(text=load_fixture("report_page.html")),   # get_report_page
        FakeResponse(text=load_fixture("postback_ok.html")),   # submit_date_range
        FakeResponse(content=b"a,b\n1,2\n"),                   # export_csv
    ]
    out = lrp.pull_report(fake_session, "9/1/2025", "9/1/2026")
    assert out == b"a,b\n1,2\n"
    assert [c[0] for c in fake_session.calls] == ["GET", "POST", "GET"]


# --- invoices ----------------------------------------------------------------

def test_export_csv_supports_other_formats(fake_session):
    fake_session.next_response = FakeResponse(content=b"%PDF-1.4 fake")
    out = lrp.export_csv(fake_session, "sess42", "ctrl42", export_format="PDF")
    assert out == b"%PDF-1.4 fake"
    _, _, kw = fake_session.calls[-1]
    assert kw["params"]["Format"] == "PDF"


def test_resolve_invoice_id_parses_known_key(fake_session):
    fake_session.next_response = FakeResponse(json_data={"storeInvoiceId": 10044999})
    resolved = lrp.resolve_invoice_id(fake_session, "584")
    assert resolved == "10044999"

    method, url, kw = fake_session.calls[-1]
    assert method == "POST"
    assert url == lrp.RESOLVE_INVOICE_ID_URL
    assert kw["json"] == {"invoiceId": "584"}


def test_resolve_invoice_id_raises_with_raw_body_on_unknown_shape(fake_session):
    fake_session.next_response = FakeResponse(json_data={"someUnexpectedKey": 123})
    with pytest.raises(ValueError, match="someUnexpectedKey"):
        lrp.resolve_invoice_id(fake_session, "584")


def test_get_invoice_report_session_plain_get_no_postback(fake_session):
    fake_session.next_response = FakeResponse(text=load_fixture("postback_ok.html"))
    session_id, control_id, rsproxy = lrp.get_invoice_report_session(fake_session, "10044999")

    assert session_id == "2cmn1ovaxf1tl2rpxj0ojb45"
    assert control_id == "4034d0674abc4e9f9b2c1d5e6f7a8b90"

    method, url, kw = fake_session.calls[-1]
    assert method == "GET"
    assert url == lrp.INVOICE_REPORT_URL
    assert kw["params"] == {"StoreId": lrp.DEFAULT_STORE_ID, "InvoiceId": "10044999"}


def test_pull_invoice_wires_steps_together(fake_session):
    fake_session.responses = [
        FakeResponse(json_data={"storeInvoiceId": 10044999}),  # resolve_invoice_id
        FakeResponse(text=load_fixture("postback_ok.html")),   # get_invoice_report_session
        FakeResponse(content=b"%PDF-1.4 fake"),                 # export_csv
    ]
    out = lrp.pull_invoice(fake_session, "584")
    assert out == b"%PDF-1.4 fake"
    assert [c[0] for c in fake_session.calls] == ["POST", "GET", "GET"]

    resolve_params = fake_session.calls[0][2]["json"]
    assert resolve_params == {"invoiceId": "584"}

    render_params = fake_session.calls[1][2]["params"]
    assert render_params["InvoiceId"] == "10044999"

    export_params = fake_session.calls[-1][2]["params"]
    assert export_params["Format"] == "PDF"
    assert export_params["ReportSession"] == "2cmn1ovaxf1tl2rpxj0ojb45"


# --- customer export ---------------------------------------------------------

def test_find_customer_export_filter_field(customer_export_soup):
    filter_field, no_filter_value, button_field = lrp.find_customer_export_filter_field(
        customer_export_soup
    )
    assert filter_field == "ctl00$ContentPlaceHolder1$reportViewer$ctl08$ctl09$ddValue"
    assert no_filter_value == "1"
    assert button_field == lrp.VIEW_REPORT_BUTTON_FIELD


def test_find_customer_export_filter_field_raises_without_matching_option():
    soup = BeautifulSoup(
        '<input type="submit" name="ctl00$ContentPlaceHolder1$reportViewer$ctl08$ctl00" value="View Report" />'
        '<select name="foo"><option value="x">Something else</option></select>',
        "html.parser",
    )
    with pytest.raises(ValueError, match="No filter"):
        lrp.find_customer_export_filter_field(soup)


def test_pull_customer_export_sets_no_filter_and_exports_csv(fake_session):
    fake_session.responses = [
        FakeResponse(text=load_fixture("customer_export_page.html")),  # get_report_page
        FakeResponse(text=load_fixture("postback_ok.html")),           # postback
        FakeResponse(content=b"firstName,lastName\nJane,Doe\n"),        # export_csv
    ]
    out = lrp.pull_customer_export(fake_session)
    assert out == b"firstName,lastName\nJane,Doe\n"
    assert [c[0] for c in fake_session.calls] == ["GET", "POST", "GET"]

    method, url, kw = fake_session.calls[1]
    assert url == lrp.CUSTOMER_EXPORT_URL
    data = kw["data"]
    assert data["ctl00$ContentPlaceHolder1$reportViewer$ctl08$ctl09$ddValue"] == "1"
    assert data[lrp.VIEW_REPORT_BUTTON_FIELD] == lrp.VIEW_REPORT_BUTTON_VALUE
