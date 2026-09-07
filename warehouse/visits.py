"""line_items -> visits -> customer_lifecycle.

A **visit** is one customer on one calendar date -- the store's "a purchase"
grain. Several work orders the same day are one visit (DESIGN.md §6). Non-sale
line items (``Void`` etc.) are dropped before rollup.

Pure functions; the store feeds them ``LineForVisit`` rows and persists the
result. Both output lists are fully recomputed each run -- they are derived, and
the data is small.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date

from .models import CustomerLifecycle, Visit


@dataclass(frozen=True, slots=True)
class LineForVisit:
    customer_id: str
    work_order_id: int
    order_date: date | None
    retail: float | None
    discount: float | None
    current_status: str


def _net(retail: float | None, discount: float | None) -> float:
    return (retail or 0.0) - (discount or 0.0)


def build_visits(
    lines: Iterable[LineForVisit],
    *,
    non_sale_statuses: frozenset[str],
) -> tuple[list[Visit], list[CustomerLifecycle]]:
    by_day: dict[tuple[str, date], list[LineForVisit]] = defaultdict(list)
    for ln in lines:
        if ln.order_date is None:
            continue
        if ln.current_status.strip().lower() in non_sale_statuses:
            continue
        by_day[(ln.customer_id, ln.order_date)].append(ln)

    # Roll each (customer, day) group into one visit, without ranks yet.
    per_customer: dict[str, list[Visit]] = defaultdict(list)
    for (customer_id, day), group in by_day.items():
        gross = round(sum(g.retail or 0.0 for g in group), 2)
        disc = round(sum(g.discount or 0.0 for g in group), 2)
        per_customer[customer_id].append(
            Visit(
                visit_id=f"{customer_id}:{day.isoformat()}",
                customer_id=customer_id,
                visit_date=day,
                work_order_count=len({g.work_order_id for g in group}),
                line_item_count=len(group),
                gross_retail=gross,
                total_discount=disc,
                revenue=round(sum(_net(g.retail, g.discount) for g in group), 2),
                visit_rank=0,  # filled below
                is_first_visit=False,
                days_since_prev_visit=None,
            )
        )

    visits: list[Visit] = []
    lifecycles: list[CustomerLifecycle] = []
    for customer_id, cvisits in per_customer.items():
        cvisits.sort(key=lambda v: v.visit_date)
        ranked: list[Visit] = []
        prev: date | None = None
        for i, v in enumerate(cvisits, start=1):
            ranked.append(
                Visit(
                    visit_id=v.visit_id,
                    customer_id=v.customer_id,
                    visit_date=v.visit_date,
                    work_order_count=v.work_order_count,
                    line_item_count=v.line_item_count,
                    gross_retail=v.gross_retail,
                    total_discount=v.total_discount,
                    revenue=v.revenue,
                    visit_rank=i,
                    is_first_visit=i == 1,
                    days_since_prev_visit=None if prev is None else (v.visit_date - prev).days,
                )
            )
            prev = v.visit_date

        visits.extend(ranked)
        first = ranked[0].visit_date
        second = ranked[1].visit_date if len(ranked) > 1 else None
        lifecycles.append(
            CustomerLifecycle(
                customer_id=customer_id,
                first_visit_date=first,
                second_visit_date=second,
                last_visit_date=ranked[-1].visit_date,
                lifetime_visits=len(ranked),
                lifetime_revenue=round(sum(v.revenue for v in ranked), 2),
                days_first_to_second=None if second is None else (second - first).days,
            )
        )

    visits.sort(key=lambda v: (v.customer_id, v.visit_date))
    lifecycles.sort(key=lambda c: c.customer_id)
    return visits, lifecycles
