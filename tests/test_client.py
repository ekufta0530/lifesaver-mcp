import pytest

from conftest import FakeResponse, load_fixture
from lifesaver.client import AuthError, LifesaverClient, ReportError, format_ssrs_date
from lifesaver.config import Settings
from lifesaver.reports import get_report

import datetime

REPORT = get_report("work-order-list")
START = datetime.date(2025, 9, 1)
END = datetime.date(2026, 9, 1)


def make_settings():
    return Settings(lifesaver_username="u", lifesaver_password="p")


def csv_response(body=b"a,b\n1,2\n"):
    return FakeResponse(content=body, headers={"Content-Type": "text/csv"})


def test_format_ssrs_date_no_zero_padding():
    assert format_ssrs_date(datetime.date(2025, 7, 1)) == "7/1/2025"


def test_fetch_csv_happy_path(fake_session):
    fake_session.responses = [
        FakeResponse(text="<html>home</html>", url="https://lsscloud.com/Home"),  # login
        FakeResponse(text=load_fixture("report_page.html")),                       # GET page
        FakeResponse(text=load_fixture("postback_ok.html")),                       # POST dates
        csv_response(b"col1,col2\n1,2\n"),                                         # export
    ]
    client = LifesaverClient(make_settings(), session=fake_session)
    out = client.fetch_csv(REPORT, START, END)

    assert out == b"col1,col2\n1,2\n"
    methods = [c[0] for c in fake_session.calls]
    assert methods == ["POST", "GET", "POST", "GET"]

    # date range + submit button went into the postback
    post_dates = fake_session.calls[2][2]["data"]
    assert post_dates[REPORT.start_date_field] == "9/1/2025"
    assert post_dates[REPORT.end_date_field] == "9/1/2026"
    assert post_dates[REPORT.view_button_field] == "View Report"


def test_login_failure_raises_auth_error(fake_session):
    fake_session.next_response = FakeResponse(
        text="bad", url="https://lsscloud.com/Account/Login?ReturnUrl=%2f"
    )
    client = LifesaverClient(make_settings(), session=fake_session)
    with pytest.raises(AuthError):
        client.fetch_csv(REPORT, START, END)


def test_reauthenticates_once_when_session_expires_midflow(fake_session):
    fake_session.responses = [
        FakeResponse(text="home", url="https://lsscloud.com/Home"),               # login #1
        FakeResponse(text="login", url="https://lsscloud.com/Account/Login"),     # GET page -> expired
        FakeResponse(text="home", url="https://lsscloud.com/Home"),               # login #2
        FakeResponse(text=load_fixture("report_page.html")),                      # GET page (retry)
        FakeResponse(text=load_fixture("postback_ok.html")),                      # POST dates
        csv_response(),                                                           # export
    ]
    client = LifesaverClient(make_settings(), session=fake_session)
    out = client.fetch_csv(REPORT, START, END)
    assert out == b"a,b\n1,2\n"
    # two login POSTs happened
    assert sum(1 for c in fake_session.calls if c[1].endswith("/Account/Login")) == 2


def test_gives_up_after_second_auth_failure(fake_session):
    fake_session.responses = [
        FakeResponse(text="home", url="https://lsscloud.com/Home"),           # login #1
        FakeResponse(text="login", url="https://lsscloud.com/Account/Login"), # GET page -> expired
        FakeResponse(text="home", url="https://lsscloud.com/Home"),           # login #2
        FakeResponse(text="login", url="https://lsscloud.com/Account/Login"), # GET page -> expired again
    ]
    client = LifesaverClient(make_settings(), session=fake_session)
    with pytest.raises(AuthError):
        client.fetch_csv(REPORT, START, END)


def _already_logged_in_html(username="u", user_id=14648, sessions=None):
    if sessions is None:
        sessions = [{"UserAccount": {"UserName": username}, "UniqueID": "sess-abc", "DateCreated": "/Date(1)/"}]
    model = {
        "UserName": username, "UserId": user_id,
        "Errors": ["UserAlreadyLoggedIn"], "ActiveSessions": sessions,
    }
    return f"<html><script>var loginModel = {__import__('json').dumps(model)};</script></html>"


def _login_page(url="https://lsscloud.com/Account/Login"):
    return FakeResponse(text=_already_logged_in_html(), url=url)


def test_auto_terminates_own_stale_session_then_logs_in(fake_session):
    fake_session.responses = [
        _login_page(),                                              # login -> already logged in
        FakeResponse(text='"Success"'),                             # TerminateSession
        FakeResponse(text="home", url="https://lsscloud.com/Home"),  # login retry -> ok
        FakeResponse(text=load_fixture("report_page.html")),
        FakeResponse(text=load_fixture("postback_ok.html")),
        csv_response(),
    ]
    client = LifesaverClient(make_settings(), session=fake_session)
    assert client.fetch_csv(REPORT, START, END) == b"a,b\n1,2\n"

    terminate = [c for c in fake_session.calls if c[1].endswith("/Account/TerminateSession/")]
    assert len(terminate) == 1
    import json
    assert json.loads(terminate[0][2]["data"]) == {"Session": "sess-abc", "UserId": 14648}


def test_does_not_terminate_other_users_sessions(fake_session):
    other = [{"UserAccount": {"UserName": "someone-else"}, "UniqueID": "x", "DateCreated": "/Date(1)/"}]
    fake_session.responses = [
        FakeResponse(text=_already_logged_in_html(sessions=other), url="https://lsscloud.com/Account/Login"),
    ]
    client = LifesaverClient(make_settings(), session=fake_session)
    with pytest.raises(AuthError, match="other users"):
        client.fetch_csv(REPORT, START, END)
    assert not any(c[1].endswith("/Account/TerminateSession/") for c in fake_session.calls)


def test_terminate_can_be_disabled(fake_session):
    fake_session.responses = [_login_page()]
    settings = Settings(lifesaver_username="u", lifesaver_password="p",
                        lifesaver_terminate_own_session=False)
    client = LifesaverClient(settings, session=fake_session)
    with pytest.raises(AuthError, match="LIFESAVER_TERMINATE_OWN_SESSION"):
        client.fetch_csv(REPORT, START, END)


def test_logout_hits_logoff(fake_session):
    fake_session.responses = [
        FakeResponse(text="home", url="https://lsscloud.com/Home"),
        FakeResponse(text=load_fixture("report_page.html")),
        FakeResponse(text=load_fixture("postback_ok.html")),
        csv_response(),
        FakeResponse(text="bye"),  # logoff
    ]
    client = LifesaverClient(make_settings(), session=fake_session)
    client.fetch_csv(REPORT, START, END)
    client.logout()
    assert fake_session.calls[-1][:2] == ("GET", "https://lsscloud.com/Account/LogOff")


def test_validation_error_page_raises_report_error(fake_session):
    fake_session.responses = [
        FakeResponse(text="home", url="https://lsscloud.com/Home"),
        FakeResponse(text=load_fixture("report_page.html")),
        FakeResponse(text=load_fixture("postback_validation_error.html")),
    ]
    client = LifesaverClient(make_settings(), session=fake_session)
    with pytest.raises(ReportError):
        client.fetch_csv(REPORT, START, END)
