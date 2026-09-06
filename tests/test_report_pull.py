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
