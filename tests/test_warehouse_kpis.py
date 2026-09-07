from datetime import date

import pytest

from warehouse.kpis import (
    ACTIVE_CUSTOMERS_TTM,
    FIRST_TO_SECOND_RATE,
    MEDIAN_DAYS_TO_SECOND,
    REACTIVATION_RATE,
    REPEAT_REVENUE_SHARE,
    compute_kpis,
)
from warehouse.visits import LineForVisit, build_visits

EMPTY = frozenset()


def L(cid, d, retail):
    return LineForVisit(cid, hash((cid, d)) & 0xFFFF, date.fromisoformat(d), retail, 0.0, "OnOrder")


# Report month 2026-09. cohort_window=12 => first-visit cohort is [2024-09, 2025-09).
SCENARIO = [
    # cA: in cohort, returned 66 days later -> counts toward first-to-second
    L("cA", "2024-10-05", 100), L("cA", "2024-12-10", 50),
    # cB: in cohort, second visit >365 days later -> did NOT return in window
    L("cB", "2025-01-15", 80), L("cB", "2026-03-01", 200),
    # cC: in cohort, never returned
    L("cC", "2025-02-20", 90),
    # cD: first visit predates the cohort; a repeat visit lands in the TTM window
    L("cD", "2023-05-01", 500), L("cD", "2026-08-15", 300),
    # cE: first visit after the cohort; single visit in the TTM window
    L("cE", "2026-05-01", 150),
    # cF: ancient first visit, long lapsed, reactivates in the report month
    L("cF", "2022-01-01", 40), L("cF", "2026-09-10", 100),
]


@pytest.fixture
def kpis_final():
    visits, _ = build_visits(SCENARIO, non_sale_statuses=EMPTY)
    snaps = compute_kpis(visits, date(2026, 9, 1), today=date(2026, 10, 1))
    return {s.metric: s for s in snaps}


def test_first_to_second_rate(kpis_final):
    s = kpis_final[FIRST_TO_SECOND_RATE]
    assert s.denominator == 3  # cA, cB, cC
    assert s.numerator == 1  # only cA returned within 365 days
    assert s.value == pytest.approx(1 / 3)
    assert s.cohort_month == date(2024, 9, 1)
    assert s.is_final is True


def test_median_days_to_second(kpis_final):
    s = kpis_final[MEDIAN_DAYS_TO_SECOND]
    assert s.denominator == 1  # only cA
    assert s.value == 66.0


def test_repeat_revenue_share(kpis_final):
    s = kpis_final[REPEAT_REVENUE_SHARE]
    # TTM window [2025-10, 2026-10): cB 2026-03 (rank2, 200), cD 2026-08 (rank2, 300),
    # cE 2026-05 (rank1, 150), cF 2026-09 (rank2, 100)
    assert s.denominator == pytest.approx(750.0)
    assert s.numerator == pytest.approx(600.0)
    assert s.value == pytest.approx(600.0 / 750.0)


def test_active_customers_ttm(kpis_final):
    s = kpis_final[ACTIVE_CUSTOMERS_TTM]
    assert s.value == 4.0  # cB, cD, cE, cF


def test_reactivation_rate(kpis_final):
    s = kpis_final[REACTIVATION_RATE]
    # lapsed at 2026-09-01 (bought before, nothing in the prior 365d): cA, cC, cF
    assert s.denominator == 3
    assert s.numerator == 1  # cF came back on 2026-09-10
    assert s.value == pytest.approx(1 / 3)


def test_not_final_before_month_closes():
    visits, _ = build_visits(SCENARIO, non_sale_statuses=EMPTY)
    snaps = compute_kpis(visits, date(2026, 9, 1), today=date(2026, 9, 20))
    assert all(s.is_final is False for s in snaps)


def test_empty_cohort_gives_none_value():
    visits, _ = build_visits(SCENARIO, non_sale_statuses=EMPTY)
    snaps = compute_kpis(
        visits, date(2026, 9, 1), today=date(2026, 10, 1), cohort_window_months=1
    )
    by = {s.metric: s for s in snaps}
    assert by[FIRST_TO_SECOND_RATE].denominator == 0
    assert by[FIRST_TO_SECOND_RATE].value is None
    assert by[MEDIAN_DAYS_TO_SECOND].value is None
