"""Lifesaver (lsscloud.com) report-pull service — Phase 1.

See spec.md. This package is the standalone API service: it owns everything
about talking to lsscloud.com (auth, the 3-step SSRS ReportViewer scrape, CSV
parsing) and exposes it over a small HTTP API. Phase 2 (the MCP server) calls
this over HTTP and knows none of the scraping details.
"""

__all__ = ["config", "client", "parser", "models", "reports"]
