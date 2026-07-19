"""Shared fy/fp-independent SEC fact period normalization."""

from __future__ import annotations

import calendar
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from usinv.data.edgar.client import EdgarPayloadError


def nearest_month_end(value: date) -> date:
    """Round a fact end date to the nearest calendar month-end (FSDS convention)."""
    current = date(value.year, value.month, calendar.monthrange(value.year, value.month)[1])
    if value.month == 1:
        previous = date(value.year - 1, 12, 31)
    else:
        previous_month = value.month - 1
        previous = date(
            value.year,
            previous_month,
            calendar.monthrange(value.year, previous_month)[1],
        )
    candidates = (previous, current)
    return min(candidates, key=lambda candidate: (abs((candidate - value).days), candidate))


def quarter_count(start: date | None, end: date) -> int:
    """Return FSDS-style rounded quarter duration without consulting filing fy/fp."""
    if start is None:
        return 0
    if start > end:
        raise EdgarPayloadError("fact period start is after its end")
    duration_days = (end - start).days + 1
    rounded = int(
        (Decimal(duration_days) / (Decimal("365.25") / 4)).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
    )
    if rounded not in {1, 2, 3, 4}:
        raise EdgarPayloadError(
            f"fact duration {duration_days} days does not map to one through four quarters"
        )
    return rounded


def fsds_period(start: date | None, end: date) -> tuple[date, int]:
    """Normalize an API/XBRL period to canonical FSDS ddate and qtrs."""
    return nearest_month_end(end), quarter_count(start, end)
