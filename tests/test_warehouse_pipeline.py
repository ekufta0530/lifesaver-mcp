from datetime import date

import pytest

from conftest import load_fixture_bytes
from warehouse import pipeline
from warehouse.config import WarehouseSettings
from warehouse.kpis import METRICS
from warehouse.store import Warehouse

HEADER_ONLY = (
    "invoiceNumber,workOrderNumber,customer,lineItemNumber,description,"
    "currentStatus,retail,discount,orderDate,dateDue\r\n"
).encode("utf-8-sig")


class FakeClient:
    """Stands in for LifesaverClient: returns canned CSV bytes per date range."""

    def __init__(self, default: bytes, by_month: dict[str, bytes] | None = None):
        self.default = default
        self.by_month = by_month or {}
        self.calls: list[tuple[date, date]] = []

    def fetch_csv(self, report, start, end):
        self.calls.append((start, end))
        return self.by_month.get(f"{start:%Y-%m}", self.default)

    def logout(self):
        pass


@pytest.fixture
def settings():
    return WarehouseSettings(warehouse_backfill_delay_seconds=0.0)


@pytest.fixture
def wh(tmp_path):
    with Warehouse(tmp_path / "w.db", tmp_path / "raw") as w:
        yield w


def test_sync_range_end_to_end(wh, settings):
    client = FakeClient(load_fixture_bytes("export_sample.csv"))

    result = pipeline.sync_range(wh, client, date(2025, 8, 1), date(2025, 8, 31))
    assert result.counts.inserted == 51
    assert client.calls == [(date(2025, 8, 1), date(2025, 8, 31))]
    assert wh.raw_pull_count() == 1

    pipeline.resolve_identities(wh, settings)
    assert wh.unresolved_line_item_count() == 0

    n_visits, n_customers = pipeline.rebuild_visits(wh, settings)
    assert 0 < n_visits < wh.line_item_count()  # same-day work orders collapsed
    assert n_customers <= n_visits

    snaps = pipeline.snapshot_month(wh, settings, date(2025, 8, 1), today=date(2025, 9, 1))
    assert {s.metric for s in snaps} == set(METRICS)
    assert wh.read_kpi_series(from_month=date(2025, 8, 1), to_month=date(2025, 8, 1))


def test_sync_range_keeps_raw_even_when_parse_fails(wh, settings):
    client = FakeClient(b"totally not a csv")
    with pytest.raises(Exception):
        pipeline.sync_range(wh, client, date(2025, 8, 1), date(2025, 8, 31))
    assert wh.raw_pull_count() == 1  # evidence kept


def test_backfill_iterates_and_checkpoints(wh, settings):
    client = FakeClient(HEADER_ONLY)
    pipeline.backfill(
        wh, client, settings,
        from_month=date(2025, 6, 1), to_month=date(2025, 8, 1), today=date(2025, 8, 15),
    )
    assert [f"{s:%Y-%m}" for s, _ in client.calls] == ["2025-06", "2025-07", "2025-08"]
    assert wh.get_checkpoint("backfill_through") == "2025-08-01"


def test_backfill_resumes_from_checkpoint(wh, settings):
    wh.set_checkpoint("backfill_through", "2025-07-01")
    client = FakeClient(HEADER_ONLY)
    pipeline.backfill(
        wh, client, settings, to_month=date(2025, 8, 1), today=date(2025, 8, 15)
    )
    assert [f"{s:%Y-%m}" for s, _ in client.calls] == ["2025-08"]  # Jun/Jul skipped


def test_backfill_restart_ignores_checkpoint(wh, settings):
    wh.set_checkpoint("backfill_through", "2025-07-01")
    client = FakeClient(HEADER_ONLY)
    pipeline.backfill(
        wh, client, settings,
        to_month=date(2025, 8, 1), today=date(2025, 8, 15), resume=False,
    )
    # from_month defaults to retention floor; at least Jun..Aug are re-pulled
    assert (date(2025, 8, 1), date(2025, 8, 31)) in client.calls


def test_status_shape(wh, settings):
    client = FakeClient(load_fixture_bytes("export_sample.csv"))
    pipeline.sync_range(wh, client, date(2025, 8, 1), date(2025, 8, 31))
    st = pipeline.status(wh)
    assert st["line_items"] == 51
    assert st["order_date_min"] == "2025-08-01"
