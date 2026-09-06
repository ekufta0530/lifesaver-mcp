import asyncio

import httpx
import pytest

from mcp_server import server


@pytest.fixture(autouse=True)
def reset_transport():
    yield
    server._transport = None


def _install(handler):
    server._transport = httpx.MockTransport(handler)


def run(coro):
    return asyncio.run(coro)


def test_forwards_to_api_and_returns_body():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        return httpx.Response(200, json={"report": "work-order-list", "count": 2, "rows": [1, 2]})

    _install(handler)
    out = run(server.get_work_order_list_report("2025-08-01", "2025-08-31"))

    assert out["count"] == 2
    assert "start=2025-08-01" in captured["url"]
    assert "end=2025-08-31" in captured["url"]
    assert "/reports/work-order-list" in captured["url"]


def test_bad_date_never_hits_the_api():
    def handler(request):  # pragma: no cover - must not be called
        raise AssertionError("API should not be called for a bad date")

    _install(handler)
    with pytest.raises(server.ReportUnavailable, match="ISO date"):
        run(server.get_work_order_list_report("08/01/2025", "2025-08-31"))


def test_end_before_start_rejected():
    _install(lambda r: httpx.Response(200, json={}))
    with pytest.raises(server.ReportUnavailable, match="before start"):
        run(server.get_work_order_list_report("2025-08-31", "2025-08-01"))


def test_api_down_gives_clean_message():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    _install(handler)
    with pytest.raises(server.ReportUnavailable, match="is the Phase 1 service running"):
        run(server.get_work_order_list_report("2025-08-01", "2025-08-31"))


def test_upstream_502_surfaced():
    def handler(request):
        return httpx.Response(502, json={"detail": "authentication to lsscloud.com failed"})

    _install(handler)
    with pytest.raises(server.ReportUnavailable, match="authentication to lsscloud.com"):
        run(server.get_work_order_list_report("2025-08-01", "2025-08-31"))


def test_tool_is_registered():
    tools = run(server.mcp.list_tools())
    names = {t.name for t in tools}
    assert "get_work_order_list_report" in names
