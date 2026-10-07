"""Shared data holders for the KPI core: visits, customer lifecycles, KPI rows.

These are plain dataclasses, not pydantic models -- they never cross an API
boundary. The one schema that *is* shared with callers is ``KpiSnapshot``, kept
simple enough to serialise with ``dataclasses.asdict``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Bump when a KPI definition changes so old and new series stay side by side in
# kpi_monthly instead of overwriting each other.
CALC_VERSION = 1


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
