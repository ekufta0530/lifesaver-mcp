"""High-level operations that string the pure transforms and the store together.

``job.py`` is a thin CLI over this module; the (future) MCP read tools call
``read_*`` helpers on the store directly.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import date, timedelta

from lifesaver.client import LifesaverClient
from lifesaver.parser import ParseError, parse_work_order_csv
from lifesaver.reports import get_report

from . import identity
from .config import WarehouseSettings, get_warehouse_settings
from .kpis import compute_kpis
from .models import KpiSnapshot, SyncResult
from .months import add_months, iter_months, month_start
from .store import Warehouse
from .visits import build_visits

log = logging.getLogger(__name__)

_BACKFILL_CHECKPOINT = "backfill_through"
# The routine sync re-pulls a trailing window, so recent months' visits can
# still shift; recompute a few months of snapshots past that window. Frozen
# (is_final) snapshots are skipped by the store regardless.
_RESNAPSHOT_MONTHS = 6


# --- sync -----------------------------------------------------------------

def sync_range(wh: Warehouse, client: LifesaverClient, start: date, end: date) -> SyncResult:
    """Pull one date range, land it in raw, upsert line_items."""
    report = get_report("work-order-list")
    raw = client.fetch_csv(report, start, end)
    try:
        rows = parse_work_order_csv(raw)
    except ParseError:
        wh.write_raw_pull(start, end, raw, -1)  # keep the evidence, then fail
        raise
    pull = wh.write_raw_pull(start, end, raw, len(rows))
    counts = wh.upsert_line_items(rows, pull.pull_id, pull.pulled_at)
    log.info(
        "sync %s..%s: %d rows (+%d new, ~%d changed, =%d same, !%d no-key)",
        start, end, len(rows), counts.inserted, counts.changed,
        counts.unchanged, counts.skipped_no_key,
    )
    return SyncResult(pull=pull, counts=counts)


# --- derived layers ------------------------------------------------------

def resolve_identities(
    wh: Warehouse, settings: WarehouseSettings
) -> tuple[identity.ResolutionResult, int]:
    result = identity.resolve(
        wh.seen_customer_names(),
        wh.load_aliases(),
        wh.load_customers(),
        split_commercial_contact=settings.warehouse_split_commercial_contact,
    )
    wh.save_resolution(result)
    linked = wh.apply_customer_ids()
    log.info(
        "identity: +%d customers, +%d aliases, %d line_items linked",
        len(result.new_customers), len(result.new_aliases), linked,
    )
    return result, linked


def rebuild_visits(wh: Warehouse, settings: WarehouseSettings) -> tuple[int, int]:
    visits, lifecycles = build_visits(
        wh.lines_for_visits(), non_sale_statuses=settings.non_sale_statuses
    )
    wh.replace_visits(visits, lifecycles)
    log.info("visits: %d visits across %d customers", len(visits), len(lifecycles))
    return len(visits), len(lifecycles)


def snapshot_month(
    wh: Warehouse, settings: WarehouseSettings, month: date, *, today: date
) -> list[KpiSnapshot]:
    snaps = compute_kpis(
        wh.load_all_visits(),
        month,
        today=today,
        cohort_window_months=settings.warehouse_cohort_window_months,
    )
    wh.upsert_kpi_snapshots(snaps)
    return snaps


def snapshot_history(
    wh: Warehouse, settings: WarehouseSettings, *, today: date | None = None
) -> int:
    today = today or date.today()
    lo, _hi = wh.order_date_coverage()
    if lo is None:
        return 0
    visits = wh.load_all_visits()
    written = 0
    for m_start, _ in iter_months(lo, today):
        snaps = compute_kpis(
            visits, m_start, today=today,
            cohort_window_months=settings.warehouse_cohort_window_months,
        )
        written += wh.upsert_kpi_snapshots(snaps)
    log.info("snapshots: wrote/updated %d KPI rows", written)
    return written


# --- orchestration ------------------------------------------------------

def refresh(
    wh: Warehouse,
    client: LifesaverClient,
    settings: WarehouseSettings,
    *,
    today: date | None = None,
) -> tuple[SyncResult, int]:
    """The routine sync: trailing-window pull, then rebuild derived layers and
    recompute the recent (non-frozen) KPI months."""
    today = today or date.today()
    start = today - timedelta(days=settings.warehouse_sync_window_days)
    result = sync_range(wh, client, start, today)
    resolve_identities(wh, settings)
    rebuild_visits(wh, settings)

    visits = wh.load_all_visits()
    written = 0
    first_month = add_months(month_start(today), -_RESNAPSHOT_MONTHS)
    for m_start, _ in iter_months(first_month, today):
        snaps = compute_kpis(
            visits, m_start, today=today,
            cohort_window_months=settings.warehouse_cohort_window_months,
        )
        written += wh.upsert_kpi_snapshots(snaps)
    return result, written


def backfill(
    wh: Warehouse,
    client: LifesaverClient,
    settings: WarehouseSettings,
    *,
    from_month: date | None = None,
    to_month: date | None = None,
    resume: bool = True,
    today: date | None = None,
    on_month: Callable[[date, SyncResult], None] | None = None,
) -> list[tuple[date, SyncResult]]:
    """Pull the full history month by month, oldest first, resumable.

    Honours a stored checkpoint so a crash/interrupt picks up where it stopped.
    An explicit ``from_month`` or ``resume=False`` ignores the checkpoint.
    After the last pull, rebuilds every derived layer and the KPI history.
    """
    today = today or date.today()
    to_month = month_start(to_month or today)
    if from_month is not None:
        resume = False
    from_month = month_start(
        from_month or add_months(month_start(today), -settings.warehouse_retention_months)
    )

    checkpoint = wh.get_checkpoint(_BACKFILL_CHECKPOINT) if resume else None
    results: list[tuple[date, SyncResult]] = []

    for m_start, m_next in iter_months(from_month, to_month):
        if checkpoint is not None and m_start.isoformat() <= checkpoint:
            continue
        if results:
            time.sleep(settings.warehouse_backfill_delay_seconds)
        res = sync_range(wh, client, m_start, m_next - timedelta(days=1))
        wh.set_checkpoint(_BACKFILL_CHECKPOINT, m_start.isoformat())
        results.append((m_start, res))
        if on_month is not None:
            on_month(m_start, res)

    resolve_identities(wh, settings)
    rebuild_visits(wh, settings)
    snapshot_history(wh, settings, today=today)
    return results


# --- status -------------------------------------------------------------

def status(wh: Warehouse) -> dict:
    lo, hi = wh.order_date_coverage()
    return {
        "raw_pulls": wh.raw_pull_count(),
        "line_items": wh.line_item_count(),
        "line_items_unresolved": wh.unresolved_line_item_count(),
        "customers": wh.customer_count(),
        "customers_with_a_purchase": wh.purchasing_customer_count(),
        "aliases_needing_review": len(wh.customers_needing_review()),
        "visits": wh.visit_count(),
        "order_date_min": lo.isoformat() if lo else None,
        "order_date_max": hi.isoformat() if hi else None,
        "backfill_through": wh.get_checkpoint(_BACKFILL_CHECKPOINT),
    }


# --- convenience for the CLI ------------------------------------------

def open_warehouse(settings: WarehouseSettings | None = None) -> Warehouse:
    settings = settings or get_warehouse_settings()
    return Warehouse(settings.warehouse_db_path, settings.warehouse_raw_dir)
