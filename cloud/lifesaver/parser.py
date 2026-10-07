"""CSV (SSRS export bytes) -> structured rows.

Confirmed against a real export (tests/fixtures/export_sample.csv):
  - UTF-8 BOM on the first line       -> decode with utf-8-sig
  - currency cells look like "$371.72" (and could carry thousands separators)
  - dates are M/d/yyyy
  - header row is camelCase:
      invoiceNumber, workOrderNumber, customer, lineItemNumber, description,
      currentStatus, retail, discount, orderDate, dateDue
  - no title/footer rows (clean SSRS CSV)
"""

from __future__ import annotations

import csv
import io
from datetime import date, datetime

from .client import LifesaverError
from .models import WorkOrder

_EXPECTED_HEADER = {
    "invoiceNumber", "workOrderNumber", "customer", "lineItemNumber",
    "description", "currentStatus", "retail", "discount", "orderDate", "dateDue",
}


class ParseError(LifesaverError):
    """The export did not look like the CSV we expect."""


def _money(value: str) -> float | None:
    v = (value or "").strip().replace("$", "").replace(",", "").replace("(", "-").replace(")", "")
    if not v:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _int(value: str) -> int | None:
    v = (value or "").strip()
    try:
        return int(v)
    except ValueError:
        return None


def _date(value: str) -> date | None:
    v = (value or "").strip()
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(v, fmt).date()
        except ValueError:
            continue
    return None


def parse_work_order_csv(raw: bytes) -> list[WorkOrder]:
    text = raw.decode("utf-8-sig")
    reader = csv.DictReader(io.StringIO(text))

    if reader.fieldnames is None:
        raise ParseError("empty export (no header row)")
    header = {h.strip() for h in reader.fieldnames}
    missing = _EXPECTED_HEADER - header
    if missing:
        raise ParseError(
            f"unexpected export columns; missing {sorted(missing)}; got {sorted(header)}"
        )

    rows: list[WorkOrder] = []
    for r in reader:
        r = {(k.strip() if k else k): v for k, v in r.items()}
        rows.append(
            WorkOrder(
                invoice_number=_int(r.get("invoiceNumber", "")),
                work_order_number=(r.get("workOrderNumber") or "").strip(),
                customer=(r.get("customer") or "").strip(),
                line_item_number=_int(r.get("lineItemNumber", "")),
                description=(r.get("description") or "").strip(),
                current_status=(r.get("currentStatus") or "").strip(),
                retail=_money(r.get("retail", "")),
                discount=_money(r.get("discount", "")),
                order_date=_date(r.get("orderDate", "")),
                date_due=_date(r.get("dateDue", "")),
            )
        )
    return rows
