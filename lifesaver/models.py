"""Response schemas. These become the OpenAPI schema, which Phase 2's MCP tool
schema mirrors almost 1:1.
"""

from __future__ import annotations

from datetime import date

from pydantic import BaseModel, Field


class WorkOrder(BaseModel):
    invoice_number: int | None = Field(None, description="invoiceNumber column")
    work_order_number: str = Field(..., description="e.g. '514.2' -- kept as a string")
    customer: str
    line_item_number: int | None = None
    description: str = ""
    current_status: str = ""
    retail: float | None = Field(None, description="USD, '$' and thousands separators stripped")
    discount: float | None = Field(None, description="USD")
    order_date: date | None = None
    date_due: date | None = None


class WorkOrderListResponse(BaseModel):
    report: str = "work-order-list"
    start: date
    end: date
    count: int
    rows: list[WorkOrder]
