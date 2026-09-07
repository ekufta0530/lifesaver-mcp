from datetime import date

from warehouse.visits import LineForVisit, build_visits

EMPTY = frozenset()


def line(cid, wo, d, retail=100.0, discount=0.0, status="OnOrder"):
    return LineForVisit(cid, wo, date.fromisoformat(d), retail, discount, status)


def test_same_day_work_orders_are_one_visit():
    lines = [
        line("cA", 534, "2025-08-15", 1444.28, 100.0),
        line("cA", 535, "2025-08-15", 512.94, 50.0),
        line("cA", 536, "2025-08-15", 590.79, 50.0),
    ]
    visits, lifecycles = build_visits(lines, non_sale_statuses=EMPTY)

    assert len(visits) == 1
    v = visits[0]
    assert v.visit_date == date(2025, 8, 15)
    assert v.work_order_count == 3
    assert v.line_item_count == 3
    assert v.revenue == round(1444.28 + 512.94 + 590.79 - 200.0, 2)
    assert v.visit_rank == 1 and v.is_first_visit is True
    assert v.days_since_prev_visit is None

    assert lifecycles[0].lifetime_visits == 1
    assert lifecycles[0].second_visit_date is None


def test_ranks_and_gaps_across_visits():
    lines = [
        line("cA", 1, "2024-10-05"),
        line("cA", 2, "2024-12-10"),
        line("cA", 3, "2025-03-01"),
    ]
    visits, (lc,) = build_visits(lines, non_sale_statuses=EMPTY)

    assert [v.visit_rank for v in visits] == [1, 2, 3]
    assert [v.days_since_prev_visit for v in visits] == [None, 66, 81]
    assert lc.first_visit_date == date(2024, 10, 5)
    assert lc.second_visit_date == date(2024, 12, 10)
    assert lc.days_first_to_second == 66
    assert lc.lifetime_visits == 3


def test_non_sale_status_excluded():
    lines = [
        line("cA", 1, "2025-01-01", status="Void"),
        line("cA", 2, "2025-02-01", status="OnOrder"),
    ]
    visits, (lc,) = build_visits(lines, non_sale_statuses=frozenset({"void"}))
    assert len(visits) == 1
    assert visits[0].visit_date == date(2025, 2, 1)
    assert lc.first_visit_date == date(2025, 2, 1)


def test_none_order_date_dropped():
    lines = [LineForVisit("cA", 1, None, 100.0, 0.0, "OnOrder")]
    visits, lifecycles = build_visits(lines, non_sale_statuses=EMPTY)
    assert visits == [] and lifecycles == []


def test_discount_and_none_money_handled():
    lines = [
        LineForVisit("cA", 1, date(2025, 1, 1), None, None, "OnOrder"),
        LineForVisit("cA", 2, date(2025, 1, 1), 100.0, 10.0, "OnOrder"),
    ]
    visits, _ = build_visits(lines, non_sale_statuses=EMPTY)
    assert visits[0].revenue == 90.0
    assert visits[0].gross_retail == 100.0
    assert visits[0].total_discount == 10.0
