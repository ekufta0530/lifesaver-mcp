"""The cloud ingest's warehouse: the core tables plus the raw layers behind them.

The Work Order List report arrives as line items keyed by customer *name*, so
this source keeps three layers the core doesn't need:

  raw_pulls / warehouse_raw/  -- every pull, verbatim (rebuild everything from here)
  line_items (+ history)      -- deduped current truth, upserted on a stable key
  customers / aliases         -- resolved identities (exact + normalised match)

``visits`` and the KPI tables come from ``core.store.Warehouse``.
"""

from __future__ import annotations

import gzip
import hashlib
import uuid
from collections.abc import Iterator, Sequence
from datetime import date
from pathlib import Path

from cloud.lifesaver.models import WorkOrder
from core.store import Warehouse, _date, _iso, _now
from core.visits import LineForVisit

from .identity import AliasRow, CustomerRow, ResolutionResult, SeenName
from .models import RawPull, UpsertCounts, content_hash, natural_key

_CLOUD_SCHEMA = """

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

"""

_LINE_ITEM_COLS = (
    "work_order_id, line_item_number, invoice_number, work_order_number, "
    "customer_raw, customer_id, description, current_status, retail, discount, "
    "order_date, date_due, content_hash, first_seen_at, last_seen_at, "
    "last_changed_at, first_pull_id, last_pull_id"
)


class CloudWarehouse(Warehouse):
    SCHEMA = _CLOUD_SCHEMA + Warehouse.SCHEMA

    def __init__(self, db_path: str | Path, raw_dir: str | Path | None = None) -> None:
        self._raw_dir = Path(raw_dir) if raw_dir is not None else None
        super().__init__(db_path)

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

    def customers_needing_review(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT customer_raw, normalized_name, customer_id, match_method, match_score "
            "FROM customer_aliases WHERE needs_review = 1 ORDER BY customer_raw"
        ).fetchall()
        return [dict(r) for r in rows]

    # --- visits ---------------------------------------------------------

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

    # --- customer drill-down (for the MCP read tool) --------------------

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
