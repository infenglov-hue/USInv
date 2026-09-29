"""Point-in-time signal and effective-session helpers.

The live contract is explicit: a portfolio effective on session ``T`` may
only use completed market data through ``T-1``.  Calendar subtraction is not
safe around weekends and holidays, so the cutoff is resolved from broad price
coverage already present in the runtime database.
"""

from __future__ import annotations

from datetime import date, timedelta

from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from us_picker.db.schema import (
    Company,
    DailyPrice,
    PortfolioCycleMark,
    PortfolioSelection,
)


SIGNAL_LOOKBACK_CALENDAR_DAYS = 14
MIN_BROAD_COVERAGE_RATIO = 0.50
TRADABLE_PRICE_LOOKBACK_DAYS = 45


def stamp_missing_signal_dates(
    session: Session,
    effective_date: date,
    signal_date: date,
) -> tuple[int, int]:
    """Backfill the explicit cutoff on legacy rows for one effective session.

    This never changes entry/exit/reference prices or re-runs selection. It
    lets the first schema-13 publish describe an already-created portfolio
    with the legal completed-session date.
    """
    selections = (
        session.query(PortfolioSelection)
        .filter(
            or_(
                and_(
                    PortfolioSelection.selection_date == effective_date,
                    PortfolioSelection.signal_date.is_(None),
                ),
                and_(
                    PortfolioSelection.cycle_ref_date == effective_date,
                    PortfolioSelection.cycle_signal_date.is_(None),
                ),
            ),
        )
        .all()
    )
    for row in selections:
        if row.selection_date == effective_date and row.signal_date is None:
            row.signal_date = signal_date
        if row.cycle_ref_date == effective_date and row.cycle_signal_date is None:
            row.cycle_signal_date = signal_date

    marks = (
        session.query(PortfolioCycleMark)
        .filter(
            PortfolioCycleMark.cycle_date == effective_date,
            PortfolioCycleMark.signal_date.is_(None),
        )
        .all()
    )
    for row in marks:
        row.signal_date = signal_date

    return len(selections), len(marks)


def tradable_company_ids(
    session: Session,
    as_of_date: date,
    *,
    lookback_days: int = TRADABLE_PRICE_LOOKBACK_DAYS,
) -> set[int]:
    """Active equity IDs with a positive recent price as of ``as_of_date``.

    KAP-only legal entities remain in ``companies`` for completeness/audit,
    but they do not consume factor-scoring work until a tradable market print
    exists.
    """
    cutoff = as_of_date - timedelta(days=max(1, int(lookback_days)))
    rows = (
        session.query(DailyPrice.company_id)
        .join(Company, Company.id == DailyPrice.company_id)
        .filter(
            Company.is_active.is_(True),
            Company.ticker != "SPY",
            or_(Company.company_type.is_(None), Company.company_type != "INDEX"),
            DailyPrice.date >= cutoff,
            DailyPrice.date <= as_of_date,
            or_(DailyPrice.adjusted_close > 0, DailyPrice.close > 0),
        )
        .distinct()
        .all()
    )
    return {int(row[0]) for row in rows}


def latest_completed_session_date(
    session: Session,
    effective_date: date,
    *,
    lookback_days: int = SIGNAL_LOOKBACK_CALENDAR_DAYS,
) -> date | None:
    """Return the latest broadly covered equity date strictly before ``T``.

    A single stale/synthetic ticker must not move the cutoff.  We therefore
    count active non-index equities by date and accept the newest date whose
    coverage is at least half of the best date in the lookback window.  XU100
    is used only as a fallback for small/test databases.
    """
    start = effective_date - timedelta(days=max(1, int(lookback_days)))
    rows = (
        session.query(
            DailyPrice.date,
            func.count(func.distinct(DailyPrice.company_id)).label("coverage"),
        )
        .join(Company, Company.id == DailyPrice.company_id)
        .filter(
            DailyPrice.date >= start,
            DailyPrice.date < effective_date,
            Company.is_active.is_(True),
            Company.ticker != "SPY",
            or_(Company.company_type.is_(None), Company.company_type != "INDEX"),
            or_(DailyPrice.adjusted_close > 0, DailyPrice.close > 0),
        )
        .group_by(DailyPrice.date)
        .order_by(DailyPrice.date.desc())
        .all()
    )
    if rows:
        best_coverage = max(int(row.coverage or 0) for row in rows)
        floor = max(1, int(best_coverage * MIN_BROAD_COVERAGE_RATIO))
        for row in rows:
            if int(row.coverage or 0) >= floor:
                return row.date

    benchmark_row = (
        session.query(func.max(DailyPrice.date))
        .join(Company, Company.id == DailyPrice.company_id)
        .filter(
            Company.ticker == "SPY",
            DailyPrice.date < effective_date,
            or_(DailyPrice.adjusted_close > 0, DailyPrice.close > 0),
        )
        .scalar()
    )
    return benchmark_row
