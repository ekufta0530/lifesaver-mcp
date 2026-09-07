"""SQLite-backed warehouse.

Why SQLite and not BigQuery (the DESIGN.md §5 target): the whole dataset is tens
of thousands of rows, there is exactly one writer (the sync job), and the KPI
math runs in Python, not in warehouse SQL. SQLite gives zero cost, zero ops, and
a single file that is trivial to back up and to develop against locally. The
immutable raw layer still lands as files under ``warehouse_raw_dir`` for the
off-box copy. BigQuery stays a later swap if a BI tool or data volume calls for
it -- this class is the only thing that would change.

Everything here is storage: rows in, rows out. All analytics live in
``identity``/``visits``/``kpis`` as pure functions.
"""

from __future__ import annotations

import gzip
import hashlib
import sqlite3
import uuid
from collections.abc import Iterator, Sequence
from datetime import date, datetime, timezone
from pathlib import Path

from lifesaver.models import WorkOrder

from .identity import AliasRow, CustomerRow, ResolutionResult, SeenName
from .models import (
    CALC_VERSION,
    CustomerLifecycle,
    KpiSnapshot,
    RawPull,
    UpsertCounts,
    content_hash,
    natural_key,
)
from .visits import LineForVisit, Visit

_SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_pulls (
    pull_id      TEXT PRIMARY KEY,
    pulled_at    TEXT NOT NULL,
    range_start  TEXT NOT NULL,
    range_end    TEXT NOT NULL,
    row_count    INTEGER NOT NULL,
    raw_sha256   TEXT NOT NULL,
    raw_filename TEXT
);

CREATE TABLE IF NOT EXISTS line_items (
    work_order_id     INTEGER NOT NULL,
    line_item_number  INTEGER NOT NULL,
    invoice_number    INTEGER,
    work_order_number TEXT,
    customer_raw      TEXT NOT NULL,
    customer_id       TEXT,
    description       TEXT,
    current_status    TEXT,
    retail            REAL,
    discount          REAL,
    order_date        TEXT,
    date_due          TEXT,
    content_hash      TEXT NOT NULL,
    first_seen_at     TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    last_changed_at   TEXT NOT NULL,
    first_pull_id     TEXT NOT NULL,
    last_pull_id      TEXT NOT NULL,
    PRIMARY KEY (work_order_id, line_item_number)
);
CREATE INDEX IF NOT EXISTS ix_line_items_customer_raw ON line_items(customer_raw);
CREATE INDEX IF NOT EXISTS ix_line_items_customer_id ON line_items(customer_id);
CREATE INDEX IF NOT EXISTS ix_line_items_order_date ON line_items(order_date);

CREATE TABLE IF NOT EXISTS line_item_history (
    work_order_id     INTEGER NOT NULL,
    line_item_number  INTEGER NOT NULL,
    observed_at       TEXT NOT NULL,
    pull_id           TEXT NOT NULL,
    content_hash      TEXT NOT NULL,
    invoice_number    INTEGER,
    work_order_number TEXT,
    customer_raw      TEXT,
    description       TEXT,
    current_status    TEXT,
    retail            REAL,
    discount          REAL,
    order_date        TEXT,
    date_due          TEXT,
    PRIMARY KEY (work_order_id, line_item_number, observed_at)
);

CREATE TABLE IF NOT EXISTS customers (
    customer_id     TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    normalized_name TEXT NOT NULL,
    is_commercial   INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_customers_normalized ON customers(normalized_name);

CREATE TABLE IF NOT EXISTS customer_aliases (
    customer_raw    TEXT PRIMARY KEY,
    normalized_name TEXT NOT NULL,
    customer_id     TEXT NOT NULL,
    match_method    TEXT NOT NULL,
    match_score     REAL,
    needs_review    INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    reviewed_at     TEXT,
    reviewed_by     TEXT
);
CREATE INDEX IF NOT EXISTS ix_aliases_customer_id ON customer_aliases(customer_id);

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

_LINE_ITEM_COLS = (
    "work_order_id, line_item_number, invoice_number, work_order_number, "
    "customer_raw, customer_id, description, current_status, retail, discount, "
    "order_date, date_due, content_hash, first_seen_at, last_seen_at, "
    "last_changed_at, first_pull_id, last_pull_id"
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _iso(d: date | None) -> str | None:
    return None if d is None else d.isoformat()


def _date(s: str | None) -> date | None:
    return None if s in (None, "") else date.fromisoformat(s[:10])


class Warehouse:
    def __init__(self, db_path: str | Path, raw_dir: str | Path | None = None) -> None:
        self._path = str(db_path)
        self._raw_dir = Path(raw_dir) if raw_dir is not None else None
        self._conn = sqlite3.connect(self._path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    # --- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> Warehouse:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- raw layer ---------------------------------------------------------

    def write_raw_pull(
        self, range_start: date, range_end: date, raw_bytes: bytes, row_count: int
    ) -> RawPull:
        pull_id = uuid.uuid4().hex
        pulled_at = _now()
        sha = hashlib.sha256(raw_bytes).hexdigest()

        filename: str | None = None
        if self._raw_dir is not None:
            self._raw_dir.mkdir(parents=True, exist_ok=True)
            filename = f"{range_start.isoformat()}_{range_end.isoformat()}.{pull_id}.csv.gz"
            (self._raw_dir / filename).write_bytes(gzip.compress(raw_bytes))

        self._conn.execute(
            "INSERT INTO raw_pulls "
            "(pull_id, pulled_at, range_start, range_end, row_count, raw_sha256, raw_filename) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (pull_id, pulled_at, range_start.isoformat(), range_end.isoformat(),
             row_count, sha, filename),
        )
        self._conn.commit()
        return RawPull(pull_id, pulled_at, range_start, range_end, row_count, sha)

    def raw_pull_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM raw_pulls").fetchone()[0]

    # --- line_items upsert -------------------------------------------------

    def upsert_line_items(
        self, rows: Sequence[WorkOrder], pull_id: str, observed_at: str
    ) -> UpsertCounts:
        counts = UpsertCounts()
        c = self._conn
        for row in rows:
            key = natural_key(row)
            if key is None:
                counts.skipped_no_key += 1
                continue
            wid, lin = key
            h = content_hash(row)
            existing = c.execute(
                "SELECT content_hash FROM line_items "
                "WHERE work_order_id = ? AND line_item_number = ?",
                (wid, lin),
            ).fetchone()

            fields = (
                row.invoice_number,
                row.work_order_number,
                row.customer,
                row.description,
                row.current_status,
                row.retail,
                row.discount,
                _iso(row.order_date),
                _iso(row.date_due),
                h,
            )

            if existing is None:
                c.execute(
                    f"INSERT INTO line_items ({_LINE_ITEM_COLS}) VALUES "
                    "(?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (wid, lin, row.invoice_number, row.work_order_number, row.customer,
                     row.description, row.current_status, row.retail, row.discount,
                     _iso(row.order_date), _iso(row.date_due), h,
                     observed_at, observed_at, observed_at, pull_id, pull_id),
                )
                counts.inserted += 1
            elif existing["content_hash"] != h:
                c.execute(
                    "INSERT INTO line_item_history "
                    "(work_order_id, line_item_number, observed_at, pull_id, content_hash, "
                    " invoice_number, work_order_number, customer_raw, description, "
                    " current_status, retail, discount, order_date, date_due) "
                    "SELECT work_order_id, line_item_number, ?, ?, content_hash, "
                    " invoice_number, work_order_number, customer_raw, description, "
                    " current_status, retail, discount, order_date, date_due "
                    "FROM line_items WHERE work_order_id = ? AND line_item_number = ?",
                    (observed_at, pull_id, wid, lin),
                )
                c.execute(
                    "UPDATE line_items SET "
                    "invoice_number = ?, work_order_number = ?, customer_raw = ?, "
                    "description = ?, current_status = ?, retail = ?, discount = ?, "
                    "order_date = ?, date_due = ?, content_hash = ?, "
                    "last_seen_at = ?, last_changed_at = ?, last_pull_id = ? "
                    "WHERE work_order_id = ? AND line_item_number = ?",
                    (*fields, observed_at, observed_at, pull_id, wid, lin),
                )
                counts.changed += 1
            else:
                c.execute(
                    "UPDATE line_items SET last_seen_at = ?, last_pull_id = ? "
                    "WHERE work_order_id = ? AND line_item_number = ?",
                    (observed_at, pull_id, wid, lin),
                )
                counts.unchanged += 1

        c.commit()
        return counts

    def line_item_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM line_items").fetchone()[0]

    def order_date_coverage(self) -> tuple[date | None, date | None]:
        r = self._conn.execute(
            "SELECT MIN(order_date), MAX(order_date) FROM line_items"
        ).fetchone()
        return _date(r[0]), _date(r[1])

    # --- identity --------------------------------------------------------

    def seen_customer_names(self) -> list[SeenName]:
        rows = self._conn.execute(
            "SELECT customer_raw, "
            "       COALESCE(MIN(order_date), MIN(substr(first_seen_at, 1, 10))) AS first_seen "
            "FROM line_items GROUP BY customer_raw"
        ).fetchall()
        return [SeenName(r["customer_raw"], r["first_seen"] or _now()[:10]) for r in rows]

    def load_aliases(self) -> dict[str, AliasRow]:
        rows = self._conn.execute(
            "SELECT customer_raw, normalized_name, customer_id, match_method, "
            "match_score, needs_review FROM customer_aliases"
        ).fetchall()
        return {
            r["customer_raw"]: AliasRow(
                r["customer_raw"], r["normalized_name"], r["customer_id"],
                r["match_method"], r["match_score"], bool(r["needs_review"]),
            )
            for r in rows
        }

    def load_customers(self) -> dict[str, CustomerRow]:
        rows = self._conn.execute(
            "SELECT customer_id, display_name, normalized_name, is_commercial, "
            "first_seen_at FROM customers"
        ).fetchall()
        return {
            r["customer_id"]: CustomerRow(
                r["customer_id"], r["display_name"], r["normalized_name"],
                bool(r["is_commercial"]), r["first_seen_at"],
            )
            for r in rows
        }

    def save_resolution(self, result: ResolutionResult) -> None:
        now = _now()
        c = self._conn
        c.executemany(
            "INSERT OR IGNORE INTO customers "
            "(customer_id, display_name, normalized_name, is_commercial, "
            " first_seen_at, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (cu.customer_id, cu.display_name, cu.normalized_name,
                 int(cu.is_commercial), cu.first_seen_at, now, now)
                for cu in result.new_customers
            ],
        )
        c.executemany(
            "INSERT OR IGNORE INTO customer_aliases "
            "(customer_raw, normalized_name, customer_id, match_method, match_score, "
            " needs_review, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (a.customer_raw, a.normalized_name, a.customer_id, a.match_method,
                 a.match_score, int(a.needs_review), now)
                for a in result.new_aliases
            ],
        )
        c.commit()

    def apply_customer_ids(self) -> int:
        cur = self._conn.execute(
            "UPDATE line_items SET customer_id = ("
            "  SELECT a.customer_id FROM customer_aliases a "
            "  WHERE a.customer_raw = line_items.customer_raw) "
            "WHERE EXISTS ("
            "  SELECT 1 FROM customer_aliases a WHERE a.customer_raw = line_items.customer_raw)"
        )
        self._conn.commit()
        return cur.rowcount

    def unresolved_line_item_count(self) -> int:
        return self._conn.execute(
            "SELECT COUNT(*) FROM line_items WHERE customer_id IS NULL"
        ).fetchone()[0]

    def customer_count(self) -> int:
        return self._conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0]

    def purchasing_customer_count(self) -> int:
        """Customers with >= 1 real visit -- i.e. not counting anyone whose only
        line items were non-sale (e.g. a lone Void). This is the KPI-relevant count."""
        return self._conn.execute(
            "SELECT COUNT(*) FROM customer_lifecycle"
        ).fetchone()[0]

    def customers_needing_review(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT customer_raw, normalized_name, customer_id, match_method, match_score "
            "FROM customer_aliases WHERE needs_review = 1 ORDER BY customer_raw"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- visits / lifecycle ---------------------------------------------

    def lines_for_visits(self) -> Iterator[LineForVisit]:
        for r in self._conn.execute(
            "SELECT customer_id, work_order_id, order_date, retail, discount, current_status "
            "FROM line_items WHERE customer_id IS NOT NULL"
        ):
            yield LineForVisit(
                customer_id=r["customer_id"],
                work_order_id=r["work_order_id"],
                order_date=_date(r["order_date"]),
                retail=r["retail"],
                discount=r["discount"],
                current_status=r["current_status"] or "",
            )

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

    def find_customers(self, text: str, limit: int = 10) -> list[dict]:
        like = f"%{text.strip()}%"
        rows = self._conn.execute(
            "SELECT c.customer_id, c.display_name, c.is_commercial, "
            "       lc.first_visit_date, lc.last_visit_date, lc.lifetime_visits, "
            "       lc.lifetime_revenue "
            "FROM customers c LEFT JOIN customer_lifecycle lc USING (customer_id) "
            "WHERE c.customer_id = ? OR c.display_name LIKE ? OR c.normalized_name LIKE ? "
            "ORDER BY lc.lifetime_revenue IS NULL, lc.lifetime_revenue DESC LIMIT ?",
            (text.strip(), like, like.lower(), limit),
        ).fetchall()
        return [dict(r) for r in rows]

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
