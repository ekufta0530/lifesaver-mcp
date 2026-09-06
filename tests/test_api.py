import datetime

import pytest
from fastapi.testclient import TestClient

from conftest import load_fixture_bytes
from lifesaver.api import app, get_client
from lifesaver.client import AuthError, ReportError


class StubClient:
    def __init__(self, *, csv=b"", exc=None):
        self.csv = csv
        self.exc = exc
        self.calls = []

    def fetch_csv(self, report, start, end):
        self.calls.append((report.key, start, end))
        if self.exc:
            raise self.exc
        return self.csv


@pytest.fixture
def client_with():
    def _install(stub):
        app.dependency_overrides[get_client] = lambda: stub
        return TestClient(app)
    yield _install
    app.dependency_overrides.clear()


def test_health():
    assert TestClient(app).get("/health").json() == {"status": "ok"}


def test_work_order_list_json(client_with):
    stub = StubClient(csv=load_fixture_bytes("export_sample.csv"))
    resp = client_with(stub).get(
        "/reports/work-order-list", params={"start": "2025-08-01", "end": "2025-08-31"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["count"] == 51
    assert body["rows"][0]["work_order_number"] == "514.2"
    assert body["rows"][0]["retail"] == 371.72
    # the service converted ISO dates to the SSRS M/d/yyyy the client expects
    assert stub.calls == [("work-order-list", datetime.date(2025, 8, 1), datetime.date(2025, 8, 31))]


def test_work_order_list_raw_csv(client_with):
    stub = StubClient(csv=load_fixture_bytes("export_sample.csv"))
    resp = client_with(stub).get(
        "/reports/work-order-list",
        params={"start": "2025-08-01", "end": "2025-08-31", "format": "csv"},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    assert resp.text.splitlines()[0].startswith("invoiceNumber,")


def test_end_before_start_is_422(client_with):
    resp = client_with(StubClient()).get(
        "/reports/work-order-list", params={"start": "2025-08-31", "end": "2025-08-01"}
    )
    assert resp.status_code == 422


def test_bad_date_is_422(client_with):
    resp = client_with(StubClient()).get(
        "/reports/work-order-list", params={"start": "not-a-date", "end": "2025-08-01"}
    )
    assert resp.status_code == 422


def test_auth_error_becomes_502(client_with):
    resp = client_with(StubClient(exc=AuthError("nope"))).get(
        "/reports/work-order-list", params={"start": "2025-08-01", "end": "2025-08-31"}
    )
    assert resp.status_code == 502
    assert "authentication" in resp.json()["detail"].lower()


def test_report_error_becomes_502(client_with):
    resp = client_with(StubClient(exc=ReportError("bad params"))).get(
        "/reports/work-order-list", params={"start": "2025-08-01", "end": "2025-08-31"}
    )
    assert resp.status_code == 502
