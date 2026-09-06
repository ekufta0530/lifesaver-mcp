"""Phase 2 — MCP server.

Exposes the Work Order List report as an MCP tool over stdio (local) or
streamable-http (remote, e.g. a claude.ai custom connector). It runs the
lsscloud.com scrape in-process via ``lifesaver.client`` -- there is no separate
Phase 1 HTTP service in the deployed setup. ``lifesaver.api`` (the FastAPI app)
is kept for local development and tests.
"""
