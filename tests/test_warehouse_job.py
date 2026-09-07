"""Smoke tests for the CLI wiring (arg parsing, command dispatch, JSON output)."""

from datetime import date

import pytest

from conftest import load_fixture_bytes
from warehouse import job, pipeline
from warehouse.config import WarehouseSettings
from warehouse.store import Warehouse


@pytest.fixture
def settings(tmp_path):
    return WarehouseSettings(
        warehouse_db_path=str(tmp_path / "w.db"),
        warehouse_raw_dir=str(tmp_path / "raw"),
        warehouse_backfill_delay_seconds=0.0,
    )


@pytest.fixture(autouse=True)
def wire(monkeypatch, settings):
    monkeypatch.setattr(job, "get_warehouse_settings", lambda: settings)


def _seed(settings):
    class FakeClient:
        def fetch_csv(self, report, start, end):
            return load_fixture_bytes("export_sample.csv")

        def logout(self):
            pass

    with Warehouse(settings.warehouse_db_path, settings.warehouse_raw_dir) as wh:
        pipeline.sync_range(wh, FakeClient(), date(2025, 8, 1), date(2025, 8, 31))
        pipeline.resolve_identities(wh, settings)
        pipeline.rebuild_visits(wh, settings)


def test_status_runs_on_empty_db(capsys):
    assert job.main(["status"]) == 0
    assert '"line_items": 0' in capsys.readouterr().out


def test_snapshot_and_kpis_roundtrip(capsys, settings):
    _seed(settings)
    assert job.main(["snapshot", "--month", "2025-08"]) == 0
    assert "first_to_second_rate" in capsys.readouterr().out

    assert job.main(["kpis", "--metric", "active_customers_ttm"]) == 0
    assert "active_customers_ttm" in capsys.readouterr().out


def test_rejects_bad_month():
    with pytest.raises(SystemExit):
        job.main(["snapshot", "--month", "August"])


def test_unknown_metric_rejected():
    with pytest.raises(SystemExit):
        job.main(["kpis", "--metric", "nonsense"])
