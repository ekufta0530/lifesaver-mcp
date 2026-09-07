"""visits -> kpi_monthly. The five retention KPIs (DESIGN.md §7).

Pure: give it the full list of visits and a reporting month, get back one
``KpiSnapshot`` per metric. The store persists them, keyed on
``(metric, month, calc_version)``, and never rewrites a row once ``is_final``.

All arithmetic is in Python (not warehouse SQL) so it is dialect-free and fully
unit-tested; the data is small enough that "load every visit" is fine.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import date, timedelta

from .models import KpiSnapshot
from .months import add_months, month_start
from .visits import Visit

FIRST_TO_SECOND_RATE = "first_to_second_rate"
REPEAT_REVENUE_SHARE = "repeat_revenue_share"
MEDIAN_DAYS_TO_SECOND = "median_days_to_second"
ACTIVE_CUSTOMERS_TTM = "active_customers_ttm"
REACTIVATION_RATE = "reactivation_rate"

METRICS = (
    FIRST_TO_SECOND_RATE,
    REPEAT_REVENUE_SHARE,
    MEDIAN_DAYS_TO_SECOND,
    ACTIVE_CUSTOMERS_TTM,
    REACTIVATION_RATE,
)

# A second purchase counts toward "first-to-second" only if it lands within this
# many days of the first (the "12-month window" in the KPI name).
SECOND_PURCHASE_WINDOW_DAYS = 365


def _rate(num: float, den: float) -> float | None:
    return None if den == 0 else num / den


def _visit_dates_by_customer(visits: Iterable[Visit]) -> dict[str, list[date]]:
    out: dict[str, list[date]] = defaultdict(list)
    for v in visits:
        out[v.customer_id].append(v.visit_date)
    for dates in out.values():
        dates.sort()
    return out


def compute_kpis(
    visits: Sequence[Visit],
    report_month: date,
    *,
    today: date,
    cohort_window_months: int = 12,
) -> list[KpiSnapshot]:
    r0 = month_start(report_month)
    r_next = add_months(r0, 1)
    is_final = today >= r_next  # the reporting month has fully closed

    ttm_lo = add_months(r0, -11)  # 12 whole months: the report month + 11 before

    dates_by_customer = _visit_dates_by_customer(visits)

    return [
        _first_to_second(dates_by_customer, r0, cohort_window_months, is_final),
        _repeat_revenue_share(visits, ttm_lo, r_next, r0, is_final),
        _median_days_to_second(dates_by_customer, r0, cohort_window_months, is_final),
        _active_customers_ttm(visits, ttm_lo, r_next, r0, is_final),
        _reactivation_rate(dates_by_customer, r0, r_next, is_final),
    ]


# --- cohort metrics --------------------------------------------------------

def _cohort_bounds(r0: date, window_months: int) -> tuple[date, date]:
    """First-visit range for the cohort whose 12-month window just closed.

    ``[cohort_lo, cohort_hi)``: ``cohort_hi`` is 12 months before the report
    month; the window reaches back ``window_months`` further.
    """
    cohort_hi = add_months(r0, -12)
    cohort_lo = add_months(cohort_hi, -window_months)
    return cohort_lo, cohort_hi


def _cohort_first_and_second(
    dates_by_customer: dict[str, list[date]], cohort_lo: date, cohort_hi: date
) -> list[tuple[date, date | None]]:
    """(first_visit, second_visit_or_None) for each customer first seen in the cohort."""
    out: list[tuple[date, date | None]] = []
    for dates in dates_by_customer.values():
        first = dates[0]
        if cohort_lo <= first < cohort_hi:
            out.append((first, dates[1] if len(dates) > 1 else None))
    return out


def _returned_within_window(first: date, second: date | None) -> bool:
    return second is not None and 0 <= (second - first).days <= SECOND_PURCHASE_WINDOW_DAYS


def _first_to_second(
    dates_by_customer: dict[str, list[date]], r0: date, window_months: int, is_final: bool
) -> KpiSnapshot:
    cohort_lo, cohort_hi = _cohort_bounds(r0, window_months)
    pairs = _cohort_first_and_second(dates_by_customer, cohort_lo, cohort_hi)
    returned = sum(_returned_within_window(f, s) for f, s in pairs)
    return KpiSnapshot(
        metric=FIRST_TO_SECOND_RATE,
        month=r0,
        value=_rate(returned, len(pairs)),
        numerator=float(returned),
        denominator=float(len(pairs)),
        cohort_month=cohort_lo,
        is_final=is_final,
    )


def _median_days_to_second(
    dates_by_customer: dict[str, list[date]], r0: date, window_months: int, is_final: bool
) -> KpiSnapshot:
    cohort_lo, cohort_hi = _cohort_bounds(r0, window_months)
    pairs = _cohort_first_and_second(dates_by_customer, cohort_lo, cohort_hi)
    gaps = [(s - f).days for f, s in pairs if _returned_within_window(f, s)]
    return KpiSnapshot(
        metric=MEDIAN_DAYS_TO_SECOND,
        month=r0,
        value=None if not gaps else float(statistics.median(gaps)),
        numerator=None,
        denominator=float(len(gaps)),
        cohort_month=cohort_lo,
        is_final=is_final,
    )


# --- trailing-window metrics --------------------------------------------------

def _repeat_revenue_share(
    visits: Iterable[Visit], lo: date, hi: date, r0: date, is_final: bool
) -> KpiSnapshot:
    total = repeat = 0.0
    for v in visits:
        if lo <= v.visit_date < hi:
            total += v.revenue
            if v.visit_rank >= 2:
                repeat += v.revenue
    return KpiSnapshot(
        metric=REPEAT_REVENUE_SHARE,
        month=r0,
        value=_rate(round(repeat, 2), round(total, 2)),
        numerator=round(repeat, 2),
        denominator=round(total, 2),
        cohort_month=None,
        is_final=is_final,
    )


def _active_customers_ttm(
    visits: Iterable[Visit], lo: date, hi: date, r0: date, is_final: bool
) -> KpiSnapshot:
    active = {v.customer_id for v in visits if lo <= v.visit_date < hi}
    return KpiSnapshot(
        metric=ACTIVE_CUSTOMERS_TTM,
        month=r0,
        value=float(len(active)),
        numerator=None,
        denominator=None,
        cohort_month=None,
        is_final=is_final,
    )


def _reactivation_rate(
    dates_by_customer: dict[str, list[date]], r0: date, r_next: date, is_final: bool
) -> KpiSnapshot:
    """Of customers 12+ months lapsed at the start of the month, the share who
    bought during the month."""
    lapse_cutoff = r0 - timedelta(days=SECOND_PURCHASE_WINDOW_DAYS)
    lapsed = 0
    reactivated = 0
    for dates in dates_by_customer.values():
        purchased_before_month = any(d < r0 for d in dates)
        if not purchased_before_month:
            continue
        active_recently = any(lapse_cutoff <= d < r0 for d in dates)
        if active_recently:
            continue
        lapsed += 1
        if any(r0 <= d < r_next for d in dates):
            reactivated += 1
    return KpiSnapshot(
        metric=REACTIVATION_RATE,
        month=r0,
        value=_rate(reactivated, lapsed),
        numerator=float(reactivated),
        denominator=float(lapsed),
        cohort_month=None,
        is_final=is_final,
    )
