"""Data holders for the cloud ingest: line-item identity and pull bookkeeping."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date

from cloud.lifesaver.models import WorkOrder


def leading_int(value: str | None) -> int | None:
    """Integer part of a work-order number like ``"514.2"`` -> ``514``.

    The integer part is stable; the ``.2`` suffix is a revision counter and
    churns (DESIGN.md §6), so only this half is safe as an identity component.
    """
    if not value:
        return None
    head = value.strip().split(".", 1)[0]
    try:
        return int(head)
    except ValueError:
        return None


def natural_key(row: WorkOrder) -> tuple[int, int] | None:
    """Stable dedup key for a line item: ``(work_order_id, line_item_number)``.

    ``work_order_id`` is the integer part of ``work_order_number``; it is present
    on every row and never changes. ``invoice_number`` (usually
    ``work_order_id - 1``) is only a fallback for the rare row whose work-order
    number will not parse. Returns ``None`` if neither is usable -- caller logs
    and skips.
    """
    wid = leading_int(row.work_order_number)
    if wid is None:
        wid = row.invoice_number
    if wid is None:
        return None
    return wid, row.line_item_number or 1


def content_hash(row: WorkOrder) -> str:
    """SHA-256 over the *mutable* fields, to detect a changed row on re-pull.

    Excludes ``work_order_number`` (revision suffix churns) but includes
    ``invoice_number`` -- a late-assigned invoice number is a real change worth
    recording in line_item_history.
    """
    parts = (
        "" if row.invoice_number is None else str(row.invoice_number),
        row.customer,
        row.description,
        row.current_status,
        "" if row.retail is None else f"{row.retail:.2f}",
        "" if row.discount is None else f"{row.discount:.2f}",
        "" if row.order_date is None else row.order_date.isoformat(),
        "" if row.date_due is None else row.date_due.isoformat(),
    )
    return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()


@dataclass(frozen=True, slots=True)
class RawPull:
    pull_id: str
    pulled_at: str  # ISO-8601 UTC
    range_start: date
    range_end: date
    row_count: int
    raw_sha256: str


@dataclass(slots=True)
class UpsertCounts:
    inserted: int = 0
    changed: int = 0
    unchanged: int = 0
    skipped_no_key: int = 0

    def __add__(self, other: UpsertCounts) -> UpsertCounts:
        return UpsertCounts(
            self.inserted + other.inserted,
            self.changed + other.changed,
            self.unchanged + other.unchanged,
            self.skipped_no_key + other.skipped_no_key,
        )


@dataclass(frozen=True, slots=True)
class SyncResult:
    pull: RawPull
    counts: UpsertCounts
