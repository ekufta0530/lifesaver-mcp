"""SQLite-backed warehouse: the tables every store's dashboard reads.

Why SQLite and not BigQuery (the DESIGN.md §5 target): the whole dataset is tens
of thousands of rows, there is exactly one writer per store, and the KPI math
runs in Python, not in warehouse SQL. SQLite gives zero cost, zero ops, and a
single file that is trivial to back up and to develop against locally. BigQuery
stays a later swap if a BI tool or data volume calls for it.

This holds only the derived layers -- ``visits``, ``customer_lifecycle``,
``kpi_monthly`` and ``sync_checkpoints`` -- which are the same whatever the data
source. A source that keeps its own raw layers (the cloud report's line items
and name-matched customers) extends this class: see ``cloud.warehouse.store``.

Everything here is storage: rows in, rows out. All analytics live in
``visits``/``kpis`` as pure functions.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import date, datetime, timezone
from pathlib import Path

from .models import CALC_VERSION, CustomerLifecycle, KpiSnapshot
from .visits import Visit

SCHEMA = """
CREATE TABLE IF NOT EXISTS visits (
    visit_id             TEXT PRIMARY KEY,
    customer_id          TEXT NOT NULL,
    visit_date           TEXT NOT NULL,
    work_order_count     INTEGER NOT NULL,
    line_item_count      INTEGER NOT NULL,
    gross_retail         REAL NOT NULL,
    total_discount       REAL NOT NULL,
    revenue              REAL NOT NULL,
    visit_rank           INTEGER NOT NULL,
    is_first_visit       INTEGER NOT NULL,
    days_since_prev_visit INTEGER,
    computed_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_visits_customer ON visits(customer_id);
CREATE INDEX IF NOT EXISTS ix_visits_date ON visits(visit_date);

CREATE TABLE IF NOT EXISTS customer_lifecycle (
    customer_id          TEXT PRIMARY KEY,
    first_visit_date     TEXT NOT NULL,
    second_visit_date    TEXT,
    last_visit_date      TEXT NOT NULL,
    lifetime_visits      INTEGER NOT NULL,
    lifetime_revenue     REAL NOT NULL,
    days_first_to_second INTEGER,
    computed_at          TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS kpi_monthly (
    metric       TEXT NOT NULL,
    month        TEXT NOT NULL,
    calc_version INTEGER NOT NULL,
    value        REAL,
    numerator    REAL,
    denominator  REAL,
    cohort_month TEXT,
    is_final     INTEGER NOT NULL DEFAULT 0,
    computed_at  TEXT NOT NULL,
    PRIMARY KEY (metric, month, calc_version)
);

CREATE TABLE IF NOT EXISTS sync_checkpoints (
    name       TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(d: date | None) -> str | None:
    return None if d is None else d.isoformat()


def _date(s: str | None) -> date | None:
    return None if s in (None, "") else date.fromisoformat(s[:10])


class Warehouse:
    SCHEMA = SCHEMA

    def __init__(self, db_path: str | Path) -> None:
        self._path = str(db_path)
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(self.SCHEMA)
        self._conn.commit()

    # --- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Warehouse:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- visits / lifecycle ---------------------------------------------

    def replace_visits(
        self, visits: Sequence[Visit], lifecycles: Sequence[CustomerLifecycle]
    ) -> None:
        now = _now()
        c = self._conn
        c.execute("DELETE FROM visits")
        c.execute("DELETE FROM customer_lifecycle")
        c.executemany(
            "INSERT INTO visits (visit_id, customer_id, visit_date, work_order_count, "
            "line_item_count, gross_retail, total_discount, revenue, visit_rank, "
            "is_first_visit, days_since_prev_visit, computed_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (v.visit_id, v.customer_id, v.visit_date.isoformat(), v.work_order_count,
                 v.line_item_count, v.gross_retail, v.total_discount, v.revenue,
                 v.visit_rank, int(v.is_first_visit), v.days_since_prev_visit, now)
                for v in visits
            ],
        )
        c.executemany(
            "INSERT INTO customer_lifecycle (customer_id, first_visit_date, "
            "second_visit_date, last_visit_date, lifetime_visits, lifetime_revenue, "
            "days_first_to_second, computed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (lc.customer_id, lc.first_visit_date.isoformat(),
                 _iso(lc.second_visit_date), lc.last_visit_date.isoformat(),
                 lc.lifetime_visits, lc.lifetime_revenue, lc.days_first_to_second, now)
                for lc in lifecycles
            ],
        )
        c.commit()

    def load_all_visits(self) -> list[Visit]:
        rows = self._conn.execute(
            "SELECT visit_id, customer_id, visit_date, work_order_count, line_item_count, "
            "gross_retail, total_discount, revenue, visit_rank, is_first_visit, "
            "days_since_prev_visit FROM visits ORDER BY customer_id, visit_date"
        ).fetchall()
        return [
            Visit(
                visit_id=r["visit_id"],
                customer_id=r["customer_id"],
                visit_date=date.fromisoformat(r["visit_date"]),
                work_order_count=r["work_order_count"],
                line_item_count=r["line_item_count"],
                gross_retail=r["gross_retail"],
                total_discount=r["total_discount"],
                revenue=r["revenue"],
                visit_rank=r["visit_rank"],
                is_first_visit=bool(r["is_first_visit"]),
                days_since_prev_visit=r["days_since_prev_visit"],
            )
            for r in rows
        ]

    def visit_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM visits").fetchone()[0]

    def purchasing_customer_count(self) -> int:
        """Customers with >= 1 real visit -- i.e. not counting anyone whose only
        line items were non-sale (e.g. a lone Void). This is the KPI-relevant count."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM customer_lifecycle"
        ).fetchone()[0]

    # --- KPI snapshots -------------------------------------------------

    def upsert_kpi_snapshots(self, snapshots: Sequence[KpiSnapshot]) -> int:
        now = _now()
        written = 0
        c = self._conn
        for s in snapshots:
            frozen = c.execute(
                "SELECT is_final FROM kpi_monthly "
                "WHERE metric = ? AND month = ? AND calc_version = ?",
                (s.metric, s.month.isoformat(), s.calc_version),
            ).fetchone()
            if frozen is not None and frozen["is_final"]:
                continue
            c.execute(
                "INSERT OR REPLACE INTO kpi_monthly (metric, month, calc_version, value, "
                "numerator, denominator, cohort_month, is_final, computed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (s.metric, s.month.isoformat(), s.calc_version, s.value, s.numerator,
                 s.denominator, _iso(s.cohort_month), int(s.is_final), now),
            )
            written += 1
        c.commit()
        return written

    def read_kpi_series(
        self,
        metric: str | None = None,
        from_month: date | None = None,
        to_month: date | None = None,
        calc_version: int = CALC_VERSION,
    ) -> list[dict]:
        clauses = ["calc_version = ?"]
        params: list = [calc_version]
        if metric is not None:
            clauses.append("metric = ?")
            params.append(metric)
        if from_month is not None:
            clauses.append("month >= ?")
            params.append(month_key(from_month))
        if to_month is not None:
            clauses.append("month <= ?")
            params.append(month_key(to_month))
        rows = self._conn.execute(
            "SELECT metric, month, calc_version, value, numerator, denominator, "
            "cohort_month, is_final, computed_at FROM kpi_monthly "
            f"WHERE {' AND '.join(clauses)} ORDER BY metric, month",
            params,
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["is_final"] = bool(d["is_final"])
            out.append(d)
        return out

    # --- checkpoints -------------------------------------------------

    def get_checkpoint(self, name: str) -> str | None:
        r = self._conn.execute(
            "SELECT value FROM sync_checkpoints WHERE name = ?", (name,)
        ).fetchone()
        return None if r is None else r["value"]

    def set_checkpoint(self, name: str, value: str) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO sync_checkpoints (name, value, updated_at) "
            "VALUES (?, ?, ?)",
            (name, value, _now()),
        )
        self._conn.commit()

    # --- customer drill-down (for the MCP read tool) --------------------

    def customer_visits(self, customer_id: str) -> list[dict]:
        rows = self._conn.execute(
            "SELECT visit_date, visit_rank, work_order_count, line_item_count, "
            "revenue, days_since_prev_visit FROM visits "
            "WHERE customer_id = ? ORDER BY visit_date",
            (customer_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def month_key(d: date) -> str:
    return d.replace(day=1).isoformat()
