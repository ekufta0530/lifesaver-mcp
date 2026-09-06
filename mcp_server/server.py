"""MCP server exposing the Lifesaver Work Order List report to Claude.

Transport: stdio (default). Talks to the Phase 1 API at LIFESAVER_API_URL
(default http://localhost:8000) -- that service must be running.

Run directly:      .venv/bin/python -m mcp_server.server
Claude Code config (.mcp.json):
    {
      "mcpServers": {
        "lifesaver": {
          "command": ".venv/bin/python",
          "args": ["-m", "mcp_server.server"],
          "env": { "LIFESAVER_API_URL": "http://localhost:8000" }
        }
      }
    }
"""

from __future__ import annotations

import os
from datetime import date

import httpx
from mcp.server.mcpserver import MCPServer

API_URL = os.environ.get("LIFESAVER_API_URL", "http://localhost:8000").rstrip("/")
HTTP_TIMEOUT = float(os.environ.get("LIFESAVER_MCP_TIMEOUT", "180"))

mcp = MCPServer(
    name="lifesaver",
    instructions=(
        "Read-only access to Lifesaver Software (lsscloud.com) store reports. "
        "Currently exposes the Work Order List report."
    ),
)


class ReportUnavailable(Exception):
    """Raised so the MCP client shows a clean message instead of a stack trace."""


# Test seam: tests set this to an httpx.MockTransport. None -> real network.
_transport: httpx.MockTransport | None = None


def _parse_iso(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ReportUnavailable(
            f"{field} must be an ISO date like 2025-08-01 (got {value!r})"
        ) from None


@mcp.tool(
    title="Get Work Order List report",
    description=(
        "Pull the Lifesaver 'Work Order List' report for a date range. Covers work "
        "orders whose order date falls within [start_date, end_date] inclusive. "
        "Dates are ISO format (YYYY-MM-DD). Returns one row per work-order line item "
        "with: invoice_number, work_order_number, customer, line_item_number, "
        "description, current_status, retail (USD), discount (USD), order_date, date_due."
    ),
)
async def get_work_order_list_report(start_date: str, end_date: str) -> dict:
    start = _parse_iso(start_date, "start_date")
    end = _parse_iso(end_date, "end_date")
    if end < start:
        raise ReportUnavailable("end_date is before start_date")

    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT, transport=_transport) as client:
            resp = await client.get(
                f"{API_URL}/reports/work-order-list",
                params={"start": start.isoformat(), "end": end.isoformat()},
            )
    except httpx.ConnectError:
        raise ReportUnavailable(
            f"cannot reach the Lifesaver API at {API_URL} -- is the Phase 1 "
            "service running? (uvicorn lifesaver.api:app)"
        ) from None
    except httpx.HTTPError as e:
        raise ReportUnavailable(f"request to the Lifesaver API failed: {e}") from None

    if resp.status_code == 200:
        return resp.json()

    detail = _detail(resp)
    if resp.status_code == 422:
        raise ReportUnavailable(f"invalid request: {detail}")
    if resp.status_code == 502:
        raise ReportUnavailable(f"Lifesaver upstream error: {detail}")
    raise ReportUnavailable(f"Lifesaver API returned HTTP {resp.status_code}: {detail}")


def _detail(resp: httpx.Response) -> str:
    try:
        return str(resp.json().get("detail", resp.text))
    except ValueError:
        return resp.text[:500]


def main() -> None:
    mcp.run()  # stdio


if __name__ == "__main__":
    main()
