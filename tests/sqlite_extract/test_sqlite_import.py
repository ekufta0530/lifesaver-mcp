import sqlite3

import pytest

from dashboard.build import load, render
from sqlite_extract.importer import import_store

ORDERS = [
    # wo_no, cust_no, order_date, subtotal, status
    (1, 10, "2024-03-02", 200.0, "Delivered"),
    (2, 10, "2024-03-02", 50.0, "Delivered"),   # same customer, same day -> one visit
    (3, 10, "2025-01-15", 120.0, "Delivered"),
    (4, 11, "2024-06-01", 80.0, "Void"),        # voided -> dropped
    (5, 12, "2025-02-10", 300.0, "Live"),
    (6, 12, "2025-03-04", 40.0, "Done"),
]


@pytest.fixture
def src(tmp_path):
    p = tmp_path / "lifesaver.sqlite"
    c = sqlite3.connect(p)
    c.execute("CREATE TABLE wo_facts (wo_no, cust_no, order_date, subtotal, status)")
    c.executemany("INSERT INTO wo_facts VALUES (?, ?, ?, ?, ?)", ORDERS)
    c.commit()
    c.close()
    return p


def test_import_builds_visits_from_customer_numbers(src, tmp_path):
    out = import_store(src, tmp_path / "w.db")
    assert out["work_orders"] == 6
    assert out["visits"] == 4          # wo 1+2 merge, wo 4 is void
    assert out["customers"] == 2       # customer 11 only had a void
    assert out["data_through"] == "2025-03-04"

    c = sqlite3.connect(tmp_path / "w.db")
    rev = dict(c.execute("SELECT visit_id, revenue FROM visits").fetchall())
    assert rev["c10:2024-03-02"] == pytest.approx(250.0)
    assert sum(rev.values()) == pytest.approx(710.0)


def test_snapshot_month_stays_open(src, tmp_path):
    import_store(src, tmp_path / "w.db")
    c = sqlite3.connect(tmp_path / "w.db")
    final = dict(c.execute(
        "SELECT month, MIN(is_final) FROM kpi_monthly GROUP BY month").fetchall())
    assert final["2025-03-01"] == 0    # the extract's last month is still open
    assert final["2025-02-01"] == 1
    assert min(final) == "2024-03-01"


def test_reimport_replaces_rather_than_appends(src, tmp_path):
    import_store(src, tmp_path / "w.db")
    import_store(src, tmp_path / "w.db")
    c = sqlite3.connect(tmp_path / "w.db")
    assert c.execute("SELECT COUNT(*) FROM visits").fetchone()[0] == 4


def test_mason_page_uses_data_date_and_fitted_ceilings(src, tmp_path):
    import_store(src, tmp_path / "w.db")
    data = load(str(tmp_path / "w.db"), "mason")
    assert data["current_month"] == "2025-03"
    assert data["store"]["snapshot"] is True
    assert data["business"]["partial_day"] == 4
    assert data["business"]["outlier"]["total_v"] == 0
    assert data["summary"]["work_orders"] == 6

    html = render(data)
    assert '<a href="mason.html" aria-current="page">' in html
    assert '<a href="index.html">' in html
    assert "Polaris" not in html.split("<script>")[0]
