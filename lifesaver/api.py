"""FastAPI service (Phase 1).

The only component that knows lsscloud.com exists. Phase 2 (MCP) calls this
over HTTP.

Run:  uvicorn lifesaver.api:app --port 8000
Docs: http://localhost:8000/docs
"""

from __future__ import annotations

import contextlib
import logging
from datetime import date
from functools import lru_cache
from typing import Literal

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import PlainTextResponse

from .client import AuthError, LifesaverClient, LifesaverError, ReportError
from .config import get_settings
from .models import WorkOrderListResponse
from .parser import ParseError, parse_work_order_csv
from .reports import get_report

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    yield
    # release the lsscloud.com session so the next process start isn't blocked
    # by LifeSaver's one-session-per-user limit
    with contextlib.suppress(Exception):
        _client().logout()


app = FastAPI(
    title="Lifesaver Report Service",
    version="0.1.0",
    summary="Pulls SSRS reports from lsscloud.com and returns them as structured data.",
    lifespan=lifespan,
)


@lru_cache
def _client() -> LifesaverClient:
    # one client (one session/cookie jar) for the life of the process
    return LifesaverClient(get_settings())


def get_client() -> LifesaverClient:  # dependency seam for tests
    return _client()


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.get(
    "/reports/work-order-list",
    response_model=WorkOrderListResponse,
    response_model_exclude_none=False,
)
def work_order_list(
    start: date = Query(..., description="inclusive start date (ISO: 2025-08-01)"),
    end: date = Query(..., description="inclusive end date (ISO: 2025-08-31)"),
    format: Literal["json", "csv"] = Query("json"),
    client: LifesaverClient = Depends(get_client),
):
    if end < start:
        raise HTTPException(422, "end date is before start date")

    report = get_report("work-order-list")
    try:
        raw = client.fetch_csv(report, start, end)
    except AuthError as e:
        raise HTTPException(502, f"authentication to lsscloud.com failed: {e}")
    except ReportError as e:
        raise HTTPException(502, f"report did not render: {e}")
    except LifesaverError as e:
        raise HTTPException(502, f"upstream error: {e}")

    if format == "csv":
        return PlainTextResponse(raw.decode("utf-8-sig"), media_type="text/csv")

    try:
        rows = parse_work_order_csv(raw)
    except ParseError as e:
        raise HTTPException(502, f"could not parse export: {e}")

    return WorkOrderListResponse(
        start=start, end=end, count=len(rows), rows=rows,
    )
