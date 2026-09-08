from datetime import date

import pytest

from dashboard.build import OUTLIER_VISIT_IDS, load
from warehouse.models import CustomerLifecycle, Visit
from warehouse.store import Warehouse


def visit(vid, cust, day, rev, *, rank=1):
    return Visit(
        visit_id=vid, customer_id=cust, visit_date=date.fromisoformat(day),
        work_order_count=1, line_item_count=1, gross_retail=rev, total_discount=0.0,
        revenue=rev, visit_rank=rank, is_first_visit=rank == 1, days_since_prev_visit=None,
    )


def lifecycle(cust, first, rev):
    d = date.fromisoformat(first)
    return CustomerLifecycle(
        customer_id=cust, first_visit_date=d, second_visit_date=None,
        last_visit_date=d, lifetime_visits=1, lifetime_revenue=rev, days_first_to_second=None,
    )


@pytest.fixture
def db(tmp_path):
    outlier_a, outlier_b = OUTLIER_VISIT_IDS
    cust = outlier_a.split(":")[0]
    visits = [
        visit(outlier_a, cust, "2024-10-05", 30000.0),
        visit(outlier_b, cust, "2024-11-06", 9000.0, rank=2),
        visit("normal:2024-10-20", "normal", "2024-10-20", 500.0),
        visit("normal2:2025-09-04", "normal2", "2025-09-04", 250.0),
    ]
    lifecycles = [lifecycle(cust, "2024-10-05", 39000.0), lifecycle("normal", "2024-10-20", 500.0),
                  lifecycle("normal2", "2025-09-04", 250.0)]
    with Warehouse(tmp_path / "w.db", tmp_path / "raw") as w:
        w.replace_visits(visits, lifecycles)
        w._conn.commit()
    return str(tmp_path / "w.db")


def test_outlier_contribution_is_isolated(db):
    out = load(db)["business"]["outlier"]
    assert out["months"] == ["2024-10", "2024-11"]
    assert out["total_v"] == 2
    assert out["total_rev"] == pytest.approx(39000.0)
    assert out["monthly"]["2024-10"] == {"rev": 30000.0, "v": 1}


def test_outlier_excluded_from_monthly_totals_on_demand(db):
    business = load(db)["business"]
    # the raw monthly series still carries the outlier ...
    assert business["monthly"]["2024-10"]["rev"] == pytest.approx(30500.0)
    assert business["monthly"]["2024-10"]["v"] == 2
    # ... and the outlier block lets the client back it out to $500 / 1 order.
    net = business["monthly"]["2024-10"]["rev"] - business["outlier"]["monthly"]["2024-10"]["rev"]
    assert net == pytest.approx(500.0)


def test_partial_prior_year_excluded_variant_present(db):
    business = load(db)["business"]
    assert "partial_ly_rev_ex" in business
    assert "partial_ly_v_ex" in business
    # no outlier visit lands in the current partial month's prior-year window
    assert business["partial_ly_rev_ex"] == business["partial_ly_rev"]
