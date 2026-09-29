"""Financial-statement freshness against the SPK filing calendar.

The scoring layer's point-in-time filter (``scoring/context.py``) makes a
period visible once its SPK deadline estimate has passed. This module answers
the inverse question for monitoring: "which quarter SHOULD be broadly present
in ``financial_statements`` by now?" — so a scheduled workflow can alarm when
the fundamentals pipeline silently stops keeping up.

Motivation (2026-07 incident): prices stayed fresh daily while the newest
statement period froze at 2025-12-31 — Q1-2026 had been filed for weeks, was
already inside its PIT visibility window, and every value/quality factor was
silently scoring on a two-quarter-old base. Nothing alarmed; the gap was only
noticed through degraded live performance.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from us_picker.db.schema import AdjustedMetric, FinancialStatement
from us_picker.scoring.context import (
    _ESTIMATED_LAG_H1_DAYS,
    _ESTIMATED_LAG_QUARTER_DAYS,
    _LEGACY_PUBLICATION_LAG_DAYS,
)

# Extra slack on top of the PIT visibility lag before we call the DB stale.
# The PIT lag already includes a small buffer over the SPK deadline; the grace
# absorbs late filers and upstream (IsYatirim) publication delay without
# turning every filing season into a false alarm.
DEFAULT_GRACE_DAYS = 5

# A period only counts as "arrived" when at least this many distinct companies
# have a statement for it — a handful of early filers must not mask a
# universe-wide gap.
DEFAULT_MIN_COMPANIES = 50

_QUARTER_MONTH_DAY = ((3, 31), (6, 30), (9, 30), (12, 31))


def visibility_lag_days(period_end: date) -> int:
    """Days after ``period_end`` when the period becomes PIT-visible.

    Mirrors the scoring PIT filter exactly (Q1/Q3: 46d, H1: 56d, annual and
    non-standard: 76d) so monitoring and scoring can never drift apart.
    """
    if period_end.month in (3, 9):
        return _ESTIMATED_LAG_QUARTER_DAYS
    if period_end.month == 6:
        return _ESTIMATED_LAG_H1_DAYS
    return _LEGACY_PUBLICATION_LAG_DAYS


def _quarter_ends_desc(today: date, count: int = 8) -> list[date]:
    """The last ``count`` calendar quarter-ends on or before ``today``."""
    ends: list[date] = []
    year = today.year
    while len(ends) < count:
        for month, day in reversed(_QUARTER_MONTH_DAY):
            qe = date(year, month, day)
            if qe <= today:
                ends.append(qe)
                if len(ends) >= count:
                    break
        year -= 1
    return ends


def expected_latest_period_end(
    today: date, grace_days: int = DEFAULT_GRACE_DAYS
) -> date:
    """Newest quarter-end whose SPK deadline estimate (+grace) has passed.

    This is the period the database is EXPECTED to carry: by this date the
    scoring PIT filter already admits it, so its absence means scorers are
    running on older fundamentals than their own rules allow.
    """
    for quarter_end in _quarter_ends_desc(today):
        visible_from = quarter_end + timedelta(
            days=visibility_lag_days(quarter_end) + grace_days
        )
        if today >= visible_from:
            return quarter_end
    raise ValueError(f"no visible quarter-end found for {today}")


def check_financial_freshness(
    session: Session,
    today: Optional[date] = None,
    min_companies: int = DEFAULT_MIN_COMPANIES,
    grace_days: int = DEFAULT_GRACE_DAYS,
) -> dict:
    """Compare the DB's newest broad statement period with the SPK calendar.

    Returns a report dict; ``fresh`` is False when the newest period carried
    by at least ``min_companies`` companies is older than the newest period
    whose filing deadline (+grace) already passed.
    """
    today = today or date.today()
    expected = expected_latest_period_end(today, grace_days=grace_days)

    rows = (
        session.query(
            FinancialStatement.period_end,
            func.count(func.distinct(FinancialStatement.company_id)),
        )
        .group_by(FinancialStatement.period_end)
        .order_by(FinancialStatement.period_end.desc())
        .all()
    )

    actual: Optional[date] = None
    actual_companies = 0
    for period_end, company_count in rows:
        if company_count >= min_companies:
            if isinstance(period_end, str):
                period_end = date.fromisoformat(period_end)
            actual = period_end
            actual_companies = company_count
            break

    fresh = actual is not None and actual >= expected
    return {
        "layer": "raw_statements",
        "checked_at": today.isoformat(),
        "expected_period_end": expected.isoformat(),
        "actual_period_end": actual.isoformat() if actual else None,
        "companies_at_actual": actual_companies,
        "min_companies": min_companies,
        "grace_days": grace_days,
        "fresh": fresh,
    }


def check_score_input_freshness(
    session: Session,
    today: Optional[date] = None,
    min_companies: int = DEFAULT_MIN_COMPANIES,
    grace_days: int = DEFAULT_GRACE_DAYS,
) -> dict:
    """Verify that broadly-fetched periods reached audited score inputs.

    Raw statements being fresh is necessary but not sufficient. A row counts
    only when the clean stage produced an ``AdjustedMetric`` with explicit
    annual/TTM basis and an input fingerprint. This closes the 2026-07 false-
    green where Q1 existed in ``financial_statements`` but every factor still
    consumed 2025 annual metrics.
    """

    today = today or date.today()
    expected = expected_latest_period_end(today, grace_days=grace_days)
    rows = (
        session.query(
            AdjustedMetric.period_end,
            func.count(func.distinct(AdjustedMetric.company_id)),
        )
        .filter(
            AdjustedMetric.calculation_basis.in_(("ANNUAL", "TTM")),
            AdjustedMetric.input_hash.isnot(None),
        )
        .group_by(AdjustedMetric.period_end)
        .order_by(AdjustedMetric.period_end.desc())
        .all()
    )

    actual: Optional[date] = None
    actual_companies = 0
    for period_end, company_count in rows:
        if company_count >= min_companies:
            if isinstance(period_end, str):
                period_end = date.fromisoformat(period_end)
            actual = period_end
            actual_companies = company_count
            break

    fresh = actual is not None and actual >= expected
    return {
        "layer": "score_inputs",
        "checked_at": today.isoformat(),
        "expected_period_end": expected.isoformat(),
        "actual_period_end": actual.isoformat() if actual else None,
        "companies_at_actual": actual_companies,
        "min_companies": min_companies,
        "grace_days": grace_days,
        "fresh": fresh,
    }
