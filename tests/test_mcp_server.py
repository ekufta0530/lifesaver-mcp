import asyncio

import pytest

from conftest import load_fixture_bytes
from lifesaver.client import AuthError, ReportError
from mcp_server import server


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


@pytest.fixture(autouse=True)
def stub_client():
    real_get_client = server.get_client
    real_client = server._client

    def install(stub):
        server.get_client = lambda: stub  # type: ignore[assignment]
        return stub

    yield install

    server.get_client = real_get_client
    server._client = real_client


def run(coro):
    return asyncio.run(coro)


def test_returns_parsed_rows(stub_client):
    stub = stub_client(StubClient(csv=load_fixture_bytes("export_sample.csv")))
    out = run(server.get_work_order_list_report("2025-08-01", "2025-08-31"))

    assert out["report"] == "work-order-list"
    assert out["start"] == "2025-08-01"
    assert out["count"] == 51
    assert out["rows"][0]["work_order_number"] == "514.2"
    assert out["rows"][0]["retail"] == 371.72
    assert out["rows"][0]["order_date"] == "2025-08-01"

    import datetime
    assert stub.calls == [
        ("work-order-list", datetime.date(2025, 8, 1), datetime.date(2025, 8, 31))
    ]


def test_bad_date_never_calls_the_client(stub_client):
    stub = stub_client(StubClient())
    with pytest.raises(server.ReportUnavailable, match="ISO date"):
        run(server.get_work_order_list_report("08/01/2025", "2025-08-31"))
    assert stub.calls == []


def test_end_before_start_rejected(stub_client):
    stub_client(StubClient())
    with pytest.raises(server.ReportUnavailable, match="before start"):
        run(server.get_work_order_list_report("2025-08-31", "2025-08-01"))


def test_auth_error_surfaced_cleanly(stub_client):
    stub_client(StubClient(exc=AuthError("bad creds")))
    with pytest.raises(server.ReportUnavailable, match="authenticate to lsscloud.com"):
        run(server.get_work_order_list_report("2025-08-01", "2025-08-31"))


def test_report_error_surfaced_cleanly(stub_client):
    stub_client(StubClient(exc=ReportError("no rows")))
    with pytest.raises(server.ReportUnavailable, match="did not render"):
        run(server.get_work_order_list_report("2025-08-01", "2025-08-31"))


def test_tool_is_registered():
    tools = run(server.mcp.list_tools())
    assert "get_work_order_list_report" in {t.name for t in tools}


# --- streamable-http wiring -------------------------------------------------

def test_build_http_app_requires_token(monkeypatch):
    monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    with pytest.raises(RuntimeError, match="MCP_AUTH_TOKEN"):
        server.build_http_app()


def test_bearer_ok():
    assert server._bearer_ok("Bearer s3cret", "s3cret")
    assert server._bearer_ok("bearer s3cret", "s3cret")
    assert not server._bearer_ok("Bearer wrong", "s3cret")
    assert not server._bearer_ok("s3cret", "s3cret")
    assert not server._bearer_ok("", "s3cret")


def test_health_route_is_public_and_unauthed(monkeypatch):
    monkeypatch.setenv("MCP_AUTH_TOKEN", "s3cret")
    from starlette.testclient import TestClient

    app = server.build_http_app()
    client = TestClient(app)

    assert client.get("/health").json() == {"status": "ok"}
    # the MCP endpoint rejects a request with no bearer token
    assert client.post("/mcp", json={"jsonrpc": "2.0", "method": "ping", "id": 1}).status_code == 401
    # ...and with the wrong token
    bad = client.post("/mcp", headers={"Authorization": "Bearer nope"}, json={})
    assert bad.status_code == 401
