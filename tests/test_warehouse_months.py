from datetime import date

from warehouse.months import add_months, iter_months, month_bounds, month_start


def test_add_months_forward_and_back():
    assert add_months(date(2025, 1, 15), 1) == date(2025, 2, 15)
    assert add_months(date(2025, 1, 15), -1) == date(2024, 12, 15)
    assert add_months(date(2025, 6, 1), 12) == date(2026, 6, 1)
    assert add_months(date(2025, 6, 1), -36) == date(2022, 6, 1)


def test_add_months_clamps_day():
    assert add_months(date(2025, 1, 31), 1) == date(2025, 2, 28)
    assert add_months(date(2024, 1, 31), 1) == date(2024, 2, 29)  # leap year


def test_month_bounds_is_half_open():
    lo, hi = month_bounds(date(2025, 8, 17))
    assert lo == date(2025, 8, 1)
    assert hi == date(2025, 9, 1)


def test_iter_months_inclusive_of_both_ends():
    got = list(iter_months(date(2025, 7, 15), date(2025, 9, 2)))
    assert got == [
        (date(2025, 7, 1), date(2025, 8, 1)),
        (date(2025, 8, 1), date(2025, 9, 1)),
        (date(2025, 9, 1), date(2025, 10, 1)),
    ]


def test_iter_months_single_month():
    assert list(iter_months(date(2025, 8, 1), date(2025, 8, 31))) == [
        (date(2025, 8, 1), date(2025, 9, 1))
    ]


def test_month_start():
    assert month_start(date(2025, 8, 31)) == date(2025, 8, 1)
