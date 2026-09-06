"""Phase 2 — MCP server.

A thin adapter: it exposes MCP tools and forwards each call to the Phase 1
FastAPI service over HTTP. It contains no scraping, auth, or parsing logic --
that all lives in Phase 1 and is reached only through its HTTP API.
"""
