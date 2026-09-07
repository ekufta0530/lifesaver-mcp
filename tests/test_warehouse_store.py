from datetime import date

import pytest

from lifesaver.models import WorkOrder
from warehouse.identity import resolve
from warehouse.models import KpiSnapshot
from warehouse.store import Warehouse


def wo(inv, won, cust, *, line=1, status="OnOrder", retail=100.0, discount=0.0,
       order="2025-08-01", due="2025-08-20", desc="Frame"):
    return WorkOrder(
        invoice_number=inv, work_order_number=won, customer=cust, line_item_number=line,
        description=desc, current_status=status, retail=retail, discount=discount,
        order_date=None if order is None else date.fromisoformat(order),
        date_due=None if due is None else date.fromisoformat(due),
    )


@pytest.fixture
def wh(tmp_path):
    with Warehouse(tmp_path / "w.db", tmp_path / "raw") as w:
        yield w


def test_upsert_inserts_then_dedupes(wh):
    rows = [wo(513, "514.2", "Alice"), wo(514, "515.1", "Bob")]
    c1 = wh.upsert_line_items(rows, "p1", "2025-09-01T00:00:00Z")
    assert (c1.inserted, c1.changed, c1.unchanged) == (2, 0, 0)

    c2 = wh.upsert_line_items(rows, "p2", "2025-09-02T00:00:00Z")
    assert (c2.inserted, c2.changed, c2.unchanged) == (0, 0, 2)
    assert wh.line_item_count() == 2


def test_change_detection_writes_history(wh):
    wh.upsert_line_items([wo(513, "514.2", "Alice", status="OnOrder")], "p1", "t1")
    c = wh.upsert_line_items(
        [wo(513, "514.3", "Alice", status="Completed")], "p2", "t2"
    )
    assert c.changed == 1

    hist = wh._conn.execute(
        "SELECT current_status, observed_at FROM line_item_history"
    ).fetchall()
    assert [h["current_status"] for h in hist] == ["OnOrder"]  # the *old* value
    cur = wh._conn.execute("SELECT current_status FROM line_items").fetchone()
    assert cur["current_status"] == "Completed"


def test_revision_suffix_only_change_is_not_a_change(wh):
    wh.upsert_line_items([wo(513, "514.2", "Alice")], "p1", "t1")
    c = wh.upsert_line_items([wo(513, "514.9", "Alice")], "p2", "t2")  # only suffix moved
    assert (c.changed, c.unchanged) == (0, 1)
    assert wh._conn.execute("SELECT COUNT(*) FROM line_item_history").fetchone()[0] == 0


def test_row_with_no_usable_key_is_skipped(wh):
    bad = WorkOrder(invoice_number=None, work_order_number="", customer="Ghost")
    c = wh.upsert_line_items([bad], "p1", "t1")
    assert c.skipped_no_key == 1
    assert wh.line_item_count() == 0


def test_falls_back_to_invoice_number_when_wo_unparseable(wh):
    c = wh.upsert_line_items(
        [WorkOrder(invoice_number=900, work_order_number="n/a", customer="X")], "p1", "t1"
    )
    assert c.inserted == 1
    assert wh._conn.execute("SELECT work_order_id FROM line_items").fetchone()[0] == 900


def test_raw_pull_written_to_disk(wh, tmp_path):
    pull = wh.write_raw_pull(date(2025, 8, 1), date(2025, 8, 31), b"a,b\n1,2\n", 1)
    files = list((tmp_path / "raw").glob("*.csv.gz"))
    assert len(files) == 1
    assert pull.pull_id in files[0].name
    assert wh.raw_pull_count() == 1


def test_identity_resolution_and_linking(wh):
    wh.upsert_line_items(
        [wo(1, "2.1", "John Smith"), wo(2, "3.1", "  john  smith "), wo(3, "4.1", "Jane Doe")],
        "p1", "t1",
    )
    wh.save_resolution(resolve(wh.seen_customer_names(), wh.load_aliases(), wh.load_customers()))
    linked = wh.apply_customer_ids()

    assert linked == 3
    assert wh.unresolved_line_item_count() == 0
    ids = [r["customer_id"] for r in wh._conn.execute(
        "SELECT customer_id FROM line_items ORDER BY work_order_id")]
    assert ids[0] == ids[1] != ids[2]  # the two John spellings merged


def test_resolution_is_incremental_across_runs(wh):
    wh.upsert_line_items([wo(1, "2.1", "John Smith")], "p1", "t1")
    wh.save_resolution(resolve(wh.seen_customer_names(), wh.load_aliases(), wh.load_customers()))

    wh.upsert_line_items([wo(9, "10.1", "john smith")], "p2", "t2")
    r2 = resolve(wh.seen_customer_names(), wh.load_aliases(), wh.load_customers())
    assert [a.customer_raw for a in r2.new_aliases] == ["john smith"]
    assert r2.new_customers == []  # John already exists


def _snap(metric, month, value, final):
    return KpiSnapshot(metric, month, value, value, 1.0, None, final)


def test_final_kpi_rows_are_frozen(wh):
    wh.upsert_kpi_snapshots([_snap("m", date(2025, 8, 1), 1.0, True)])
    wh.upsert_kpi_snapshots([_snap("m", date(2025, 8, 1), 2.0, True)])  # ignored
    assert wh.read_kpi_series("m")[0]["value"] == 1.0


def test_nonfinal_kpi_rows_update(wh):
    wh.upsert_kpi_snapshots([_snap("m", date(2025, 8, 1), 1.0, False)])
    wh.upsert_kpi_snapshots([_snap("m", date(2025, 8, 1), 2.0, False)])
    assert wh.read_kpi_series("m")[0]["value"] == 2.0


def test_read_kpi_series_filters_by_month_range(wh):
    for m in (date(2025, 6, 1), date(2025, 7, 1), date(2025, 8, 1)):
        wh.upsert_kpi_snapshots([_snap("m", m, 1.0, True)])
    got = wh.read_kpi_series("m", from_month=date(2025, 7, 1), to_month=date(2025, 8, 15))
    assert [r["month"] for r in got] == ["2025-07-01", "2025-08-01"]


def test_checkpoints(wh):
    assert wh.get_checkpoint("k") is None
    wh.set_checkpoint("k", "2025-08-01")
    wh.set_checkpoint("k", "2025-09-01")
    assert wh.get_checkpoint("k") == "2025-09-01"
