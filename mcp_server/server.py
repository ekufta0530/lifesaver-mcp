"""MCP server for the Lifesaver 'Work Order List' report.

Two transports, chosen by ``MCP_TRANSPORT``:

  stdio (default)          -- local use, e.g. Claude Code via .mcp.json
      python -m mcp_server.server

  streamable-http          -- remote use, e.g. a claude.ai custom connector
      MCP_TRANSPORT=streamable-http python -m mcp_server.server
      serves the MCP endpoint at /mcp and a public /health check, and requires
          Authorization: Bearer $MCP_AUTH_TOKEN
      on every request except /health.

Unlike the earlier design, this runs the lsscloud.com scrape **in-process** via
``LifesaverClient`` -- there is no separate Phase 1 HTTP service to deploy. It
keeps one login session for the life of the process and serialises report pulls
with a lock, because LifeSaver allows only one active session per user: **deploy
exactly one instance** (Cloud Run: ``--max-instances=1``).
"""

from __future__ import annotations

import hmac
import os
import threading
from datetime import date

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

from lifesaver.client import AuthError, LifesaverClient, LifesaverError, ReportError
from lifesaver.config import get_settings
from lifesaver.parser import ParseError, parse_work_order_csv
from lifesaver.reports import get_report

INSTRUCTIONS = (
    "Read-only access to the Lifesaver Software (lsscloud.com) 'Work Order List' "
    "report for a picture-framing store. One tool -- get_work_order_list_report -- "
    "returns work-order line items whose order date falls in a date range."
)

mcp = MCPServer(name="lifesaver", version="0.2.0", instructions=INSTRUCTIONS)


class ReportUnavailable(ToolError):
    """Raised so the MCP client shows a clean message instead of a stack trace.

    Subclasses the SDK's ToolError: its text is an *anticipated* failure and is
    passed through to the caller (a bare Exception would be masked as a generic
    "Error executing tool ...")."""


# --- lsscloud.com client: one login session for the life of the process -----
_client: LifesaverClient | None = None
_client_lock = threading.Lock()  # the 3-step SSRS flow is stateful; serialise it


def get_client() -> LifesaverClient:  # test seam
    global _client
    if _client is None:
        _client = LifesaverClient(get_settings())
    return _client


def _parse_iso(value: str, field: str) -> date:
    try:
        return date.fromisoformat(value.strip())
    except ValueError:
        raise ReportUnavailable(
            f"{field} must be an ISO date like 2025-08-01 (got {value!r})"
        ) from None


def _fetch_rows(start: date, end: date) -> list[dict]:
    """Blocking: the full login -> postback -> export -> parse flow."""
    report = get_report("work-order-list")
    client = get_client()
    with _client_lock:
        try:
            raw = client.fetch_csv(report, start, end)
        except AuthError as e:
            raise ReportUnavailable(f"could not authenticate to lsscloud.com: {e}") from None
        except ReportError as e:
            raise ReportUnavailable(f"the report did not render: {e}") from None
        except LifesaverError as e:
            raise ReportUnavailable(f"upstream error from lsscloud.com: {e}") from None
    try:
        rows = parse_work_order_csv(raw)
    except ParseError as e:
        raise ReportUnavailable(f"could not parse the export: {e}") from None
    return [r.model_dump(mode="json") for r in rows]


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
    rows = await anyio.to_thread.run_sync(_fetch_rows, start, end)
    return {
        "report": "work-order-list",
        "start": start.isoformat(),
        "end": end.isoformat(),
        "count": len(rows),
        "rows": rows,
    }


@mcp.custom_route("/health", methods=["GET"])
async def health(request):  # noqa: ARG001 -- Starlette signature
    from starlette.responses import JSONResponse

    return JSONResponse({"status": "ok"})


# --- streamable-http transport: bearer-token auth around the MCP app --------
class BearerAuth:
    """ASGI middleware: require  Authorization: Bearer <token>  on every HTTP
    path except /health. Constant-time compare; no token -> 401."""

    _EXEMPT = frozenset({"/health"})

    def __init__(self, app, token: str) -> None:
        self._app = app
        self._token = token

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or scope["path"] in self._EXEMPT:
            await self._app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        if not _bearer_ok(headers.get(b"authorization", b"").decode(), self._token):
            from starlette.responses import JSONResponse

            await JSONResponse({"error": "unauthorized"}, status_code=401)(scope, receive, send)
            return
        await self._app(scope, receive, send)


def _bearer_ok(header_value: str, expected: str) -> bool:
    scheme, _, given = header_value.partition(" ")
    return scheme.lower() == "bearer" and hmac.compare_digest(given.strip(), expected)


def build_http_app():
    token = os.environ.get("MCP_AUTH_TOKEN")
    if not token:
        raise RuntimeError("MCP_AUTH_TOKEN must be set for MCP_TRANSPORT=streamable-http")

    allowed_hosts = [h for h in os.environ.get("MCP_ALLOWED_HOSTS", "").split(",") if h]
    security = TransportSecuritySettings(
        enable_dns_rebinding_protection=bool(allowed_hosts),
        allowed_hosts=allowed_hosts or ["*"],
        allowed_origins=allowed_hosts or ["*"],
    )
    app = mcp.streamable_http_app(stateless_http=True, transport_security=security)
    return BearerAuth(app, token)


def main() -> None:
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "stdio":
        mcp.run("stdio")
    elif transport in ("streamable-http", "http"):
        import uvicorn

        uvicorn.run(
            build_http_app(),
            host="0.0.0.0",  # noqa: S104 -- container; ingress is fronted by Cloud Run
            port=int(os.environ.get("PORT", "8080")),
            log_level=os.environ.get("LOG_LEVEL", "info"),
        )
    else:
        raise SystemExit(f"unknown MCP_TRANSPORT {transport!r} (stdio | streamable-http)")


if __name__ == "__main__":
    main()
