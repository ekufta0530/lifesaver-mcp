"""Internal data holders for the warehouse pipeline.

These are plain dataclasses, not pydantic models -- they never cross an API
boundary (the MCP read tools build their own response dicts). The one schema that
*is* shared with callers is ``KpiSnapshot``, kept simple enough to serialise with
``dataclasses.asdict``.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

from lifesaver.models import WorkOrder

# Bump when a KPI definition changes so old and new series stay side by side in
# kpi_monthly instead of overwriting each other.
CALC_VERSION = 1


def leading_int(value: str | None) -> int | None:
    """Integer part of a work-order number like ``"514.2"`` -> ``514``.

    The integer part is stable; the ``.2`` suffix is a revision counter and
    churns (DESIGN.md §6), so only this half is safe as an identity component.
    """
    if not value:
        return None
    head = value.strip().split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def natural_key(row: WorkOrder) -> tuple[int, int] | None:
    """Stable dedup key for a line item: ``(work_order_id, line_item_number)``.

    ``work_order_id`` is the integer part of ``work_order_number``; it is present
    on every row and never changes. ``invoice_number`` (usually
    ``work_order_id - 1``) is only a fallback for the rare row whose work-order
    number will not parse. Returns ``None`` if neither is usable -- caller logs
    and skips.
    """
    wid = leading_int(row.work_order_number)
    if wid is None:
        wid = row.invoice_number
    if wid is None:
        return None
    return wid, row.line_item_number or 1


def content_hash(row: WorkOrder) -> str:
    """SHA-256 over the *mutable* fields, to detect a changed row on re-pull.

    Excludes ``work_order_number`` (revision suffix churns) but includes
    ``invoice_number`` -- a late-assigned invoice number is a real change worth
    recording in line_item_history.
    """
    parts = (
        "" if row.invoice_number is None else str(row.invoice_number),
        row.customer,
        row.description,
        row.current_status,
        "" if row.retail is None else f"{row.retail:.2f}",
        "" if row.discount is None else f"{row.discount:.2f}",
        "" if row.order_date is None else row.order_date.isoformat(),
        "" if row.date_due is None else row.date_due.isoformat(),
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RawPull:
    pull_id: str
    pulled_at: str  # ISO-8601 UTC
    range_start: date
    range_end: date
    row_count: int
    raw_sha256: str


@dataclass(slots=True)
class UpsertCounts:
    inserted: int = 0
    changed: int = 0
    unchanged: int = 0
    skipped_no_key: int = 0

    def __add__(self, other: UpsertCounts) -> UpsertCounts:
        return UpsertCounts(
            self.inserted + other.inserted,
            self.changed + other.changed,
            self.unchanged + other.unchanged,
            self.skipped_no_key + other.skipped_no_key,
        )


@dataclass(frozen=True, slots=True)
class SyncResult:
    pull: RawPull
    counts: UpsertCounts


@dataclass(frozen=True, slots=True)
class Visit:
    """One purchase occasion: a customer on a calendar date (DESIGN.md §6).

    Multiple work orders / invoices the same day roll up into one Visit.
    """

    visit_id: str
    customer_id: str
    visit_date: date
    work_order_count: int
    line_item_count: int
    gross_retail: float
    total_discount: float
    revenue: float
    visit_rank: int  # 1 == the customer's first-ever visit
    is_first_visit: bool
    days_since_prev_visit: int | None


@dataclass(frozen=True, slots=True)
class CustomerLifecycle:
    customer_id: str
    first_visit_date: date
    second_visit_date: date | None
    last_visit_date: date
    lifetime_visits: int
    lifetime_revenue: float
    days_first_to_second: int | None


@dataclass(frozen=True, slots=True)
class KpiSnapshot:
    metric: str
    month: date  # first of the reporting month
    value: float | None
    numerator: float | None
    denominator: float | None
    cohort_month: date | None
    is_final: bool
    calc_version: int = CALC_VERSION
