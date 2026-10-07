"""LifeSaver desktop SQLite extract -> warehouse tables (visits, lifecycle, KPIs).

    python -m sqlite_extract.importer lifesaver.sqlite mason.db

For a store whose data arrives as a full SQLite extract of the LifeSaver POS
(``wo_facts`` view, one row per work order, latest revision) rather than via the
cloud Work Order List report. It feeds the same pure transforms the sync
pipeline uses -- ``build_visits`` then ``compute_kpis`` -- so the KPI math is
identical; only the input differs:

- **Customer identity** is LifeSaver's own customer number, so the name-matching
  layer (``identity.py``) is not needed.
- **Revenue** is the ticket subtotal: pre-tax and net of discounts, the same
  "Sales" figure the store's MCP server reports.
- **"Today"** is the extract's latest order date, not the wall clock -- the
  extract is a periodic snapshot, so the month it ends in is the open month.

The destination is rebuilt from scratch on every run (it is fully derived).
"""

from __future__ import annotations

import argparse
import sqlite3
from datetime import date
from pathlib import Path

from core.config import get_kpi_settings
from core.kpis import compute_kpis
from core.months import iter_months
from core.store import Warehouse
from core.visits import LineForVisit, build_visits

# sync_checkpoints keys the dashboard reads back
DATA_THROUGH = "data_through"
SOURCE_WORK_ORDERS = "source_work_orders"


def read_work_orders(src: str | Path) -> list[LineForVisit]:
    c = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        rows = c.execute(
            "SELECT wo_no, cust_no, order_date, subtotal, status FROM wo_facts"
        ).fetchall()
    finally:
        c.close()
    return [
        LineForVisit(
            customer_id=f"c{cust_no}",
            work_order_id=int(wo_no),
            order_date=date.fromisoformat(order_date[:10]) if order_date else None,
            retail=subtotal,
            discount=0.0,  # subtotal is already net of discount
            current_status=status or "",
        )
        for wo_no, cust_no, order_date, subtotal, status in rows
    ]


def import_store(src: str | Path, dest: str | Path) -> dict:
    settings = get_kpi_settings()
    lines = read_work_orders(src)
    visits, lifecycles = build_visits(lines, non_sale_statuses=settings.non_sale_statuses)
    as_of = max(v.visit_date for v in visits)

    dest = Path(dest)
    for p in (dest, Path(f"{dest}-wal"), Path(f"{dest}-shm")):
        p.unlink(missing_ok=True)

    with Warehouse(dest) as wh:
        wh.replace_visits(visits, lifecycles)
        first = min(v.visit_date for v in visits)
        kpi_rows = 0
        for m_start, _ in iter_months(first, as_of):
            kpi_rows += wh.upsert_kpi_snapshots(compute_kpis(
                visits, m_start, today=as_of,
                cohort_window_months=settings.warehouse_cohort_window_months,
            ))
        wh.set_checkpoint(DATA_THROUGH, as_of.isoformat())
        wh.set_checkpoint(SOURCE_WORK_ORDERS, str(len(lines)))

    return {
        "work_orders": len(lines),
        "visits": len(visits),
        "customers": len(lifecycles),
        "first_day": first.isoformat(),
        "data_through": as_of.isoformat(),
        "kpi_rows": kpi_rows,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src", help="LifeSaver SQLite extract (has the wo_facts view)")
    ap.add_argument("dest", help="warehouse db to (re)create")
    args = ap.parse_args(argv)
    for k, v in import_store(args.src, args.dest).items():
        print(f"{k:>14}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
