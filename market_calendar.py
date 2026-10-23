"""NYSE full-day holidays (vendored from Market-Monitor's market-monitor/breadth.py)."""

from __future__ import annotations

from datetime import date, timedelta

# Good Friday is not on a fixed weekday rule; extend this table each year.
_GOOD_FRIDAY = {
    2024: date(2024, 3, 29),
    2025: date(2025, 4, 18),
    2026: date(2026, 4, 3),
    2027: date(2027, 3, 26),
    2028: date(2028, 4, 14),
    2029: date(2029, 3, 30),
    2030: date(2030, 4, 19),
    2031: date(2031, 4, 11),
    2032: date(2032, 3, 26),
}


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date:
    first = date(year, month, 1)
    shift = (weekday - first.weekday()) % 7
    return first + timedelta(days=shift + 7 * (n - 1))


def _last_weekday(year: int, month: int, weekday: int) -> date:
    if month == 12:
        cursor = date(year + 1, 1, 1) - timedelta(days=1)
    else:
        cursor = date(year, month + 1, 1) - timedelta(days=1)
    return cursor - timedelta(days=(cursor.weekday() - weekday) % 7)


def _observed(day: date) -> date:
    if day.weekday() == 5:
        return day - timedelta(days=1)
    if day.weekday() == 6:
        return day + timedelta(days=1)
    return day


def nyse_holidays(year: int) -> set[date]:
    days = {
        _observed(date(year, 1, 1)),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _last_weekday(year, 5, 0),
        _observed(date(year, 6, 19)),
        _observed(date(year, 7, 4)),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed(date(year, 12, 25)),
    }
    friday = _GOOD_FRIDAY.get(year)
    if friday:
        days.add(friday)
    return days


def holiday_set(year: int) -> set[date]:
    found: set[date] = set()
    for offset in (-1, 0, 1):
        found |= nyse_holidays(year + offset)
    return found
