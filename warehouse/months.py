"""Small month-arithmetic helpers (no python-dateutil dependency)."""

from __future__ import annotations

import calendar
from collections.abc import Iterator
from datetime import date


def month_start(d: date) -> date:
    return d.replace(day=1)


def add_months(d: date, n: int) -> date:
    """Shift by ``n`` whole months, clamping the day to the target month's length."""
    total = (d.year * 12 + (d.month - 1)) + n
    year, month0 = divmod(total, 12)
    month = month0 + 1
    day = min(d.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)


def month_bounds(d: date) -> tuple[date, date]:
    """``(first_of_month, first_of_next_month)`` -- a half-open ``[lo, hi)`` range."""
    lo = month_start(d)
    return lo, add_months(lo, 1)


def iter_months(start: date, end: date) -> Iterator[tuple[date, date]]:
    """Yield ``(first_of_month, first_of_next_month)`` for every month that
    overlaps ``[start, end]``, oldest first."""
    cur = month_start(start)
    last = month_start(end)
    while cur <= last:
        nxt = add_months(cur, 1)
        yield cur, nxt
        cur = nxt
