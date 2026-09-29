"""Canonical live portfolio performance ledger.

The old home KPI averaged every ``PortfolioSelection`` row. That is neither a
portfolio return nor a NAV: duplicate/zero legacy rows diluted win rate and a
10% winner plus 10% loser was mixed with every other historical trade instead
of first forming a period return and then compounding periods.

This module mirrors the PWA History contract:

* legacy history groups rows by selection cohort;
* B1 continuity history uses consecutive ``PortfolioCycleMark`` snapshots;
* a stop/target exit contributes its realized return and the freed slot stays
  in cash for the remainder of the period;
* equal-weight period returns compound into one live-tracking NAV.

The result is explicitly gross of fees/slippage because live selections do not
yet persist those amounts. The field is labelled so it cannot be mistaken for
a net investor return.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from us_picker.db.schema import (
    Company,
    DailyPrice,
    PortfolioCycleMark,
    PortfolioSelection,
)


LIVE_TRACKING_START_DATE = date(2026, 5, 21)
_EPSILON = 1e-12


@dataclass(frozen=True)
class PositionReturn:
    company_id: int
    return_fraction: float


@dataclass(frozen=True)
class PeriodReturn:
    start_date: date
    end_date: date
    portfolio_return: float
    benchmark_return: Optional[float]
    is_completed: bool
    positions: tuple[PositionReturn, ...]


def _mean(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _position_return(
    company_id: int,
    entry_price: Optional[float],
    exit_price: Optional[float],
) -> Optional[PositionReturn]:
    if entry_price is None or exit_price is None:
        return None
    entry = float(entry_price)
    end = float(exit_price)
    if entry <= 0 or end <= 0:
        return None
    return PositionReturn(company_id, end / entry - 1.0)


def _latest_prices(session: Session, company_ids: list[int]) -> dict[int, float]:
    ids = sorted(set(company_ids))
    if not ids:
        return {}
    latest = (
        session.query(
            DailyPrice.company_id,
            func.max(DailyPrice.date).label("max_date"),
        )
        .filter(
            DailyPrice.company_id.in_(ids),
            DailyPrice.close.isnot(None),
        )
        .group_by(DailyPrice.company_id)
        .subquery()
    )
    rows = (
        session.query(
            DailyPrice.company_id,
            DailyPrice.close,
            DailyPrice.adjusted_close,
        )
        .join(
            latest,
            (DailyPrice.company_id == latest.c.company_id)
            & (DailyPrice.date == latest.c.max_date),
        )
        .all()
    )
    return {
        company_id: float(adjusted if adjusted is not None else close)
        for company_id, close, adjusted in rows
        if adjusted is not None or close is not None
    }


def _latest_price_date(session: Session, company_ids: list[int]) -> Optional[date]:
    if not company_ids:
        return None
    return (
        session.query(func.max(DailyPrice.date))
        .filter(
            DailyPrice.company_id.in_(sorted(set(company_ids))),
            DailyPrice.close.isnot(None),
        )
        .scalar()
    )


def _benchmark_company_id(session: Session) -> Optional[int]:
    row = session.query(Company.id).filter(Company.ticker == "XU100").first()
    return int(row[0]) if row else None


def _price_on_or_before(
    session: Session,
    company_id: Optional[int],
    price_date: date,
) -> Optional[float]:
    if company_id is None:
        return None
    row = (
        session.query(DailyPrice.close, DailyPrice.adjusted_close)
        .filter(
            DailyPrice.company_id == company_id,
            DailyPrice.date <= price_date,
            DailyPrice.close.isnot(None),
        )
        .order_by(DailyPrice.date.desc())
        .first()
    )
    if not row:
        return None
    return float(row.adjusted_close if row.adjusted_close is not None else row.close)


def _benchmark_return(
    session: Session,
    benchmark_id: Optional[int],
    start_date: date,
    end_date: date,
) -> Optional[float]:
    start = _price_on_or_before(session, benchmark_id, start_date)
    end = _price_on_or_before(session, benchmark_id, end_date)
    if start is None or end is None or start <= 0:
        return None
    return end / start - 1.0


def _legacy_periods(
    session: Session,
    portfolio: str,
    start_date: date,
    benchmark_id: Optional[int],
    open_rows: list[PortfolioSelection],
    completed_rows: list[PortfolioSelection],
) -> list[PeriodReturn]:
    active_start = max(
        (row.selection_date for row in open_rows if row.selection_date),
        default=None,
    )
    cohorts: dict[date, list[PortfolioSelection]] = {}
    for row in completed_rows:
        if row.selection_date < start_date:
            continue
        if active_start is not None and row.selection_date == active_start:
            continue
        cohorts.setdefault(row.selection_date, []).append(row)

    periods: list[PeriodReturn] = []
    for cohort_date, rows in sorted(cohorts.items()):
        positions = tuple(
            position
            for row in rows
            if (
                position := _position_return(
                    row.company_id, row.entry_price, row.exit_price
                )
            )
            is not None
        )
        if not positions:
            continue
        end_date = max(row.exit_date for row in rows if row.exit_date is not None)
        periods.append(
            PeriodReturn(
                start_date=cohort_date,
                end_date=end_date,
                portfolio_return=_mean(
                    [position.return_fraction for position in positions]
                ) or 0.0,
                benchmark_return=_benchmark_return(
                    session, benchmark_id, cohort_date, end_date
                ),
                is_completed=True,
                positions=positions,
            )
        )

    if active_start is not None and active_start >= start_date:
        active_ids = [row.company_id for row in open_rows]
        latest_prices = _latest_prices(session, active_ids)
        positions_list: list[PositionReturn] = []
        for row in open_rows:
            if row.selection_date != active_start:
                continue
            position = _position_return(
                row.company_id,
                row.entry_price,
                latest_prices.get(row.company_id),
            )
            if position is not None:
                positions_list.append(position)
        for row in completed_rows:
            if row.selection_date != active_start:
                continue
            position = _position_return(
                row.company_id, row.entry_price, row.exit_price
            )
            if position is not None:
                positions_list.append(position)

        if positions_list:
            latest_date = _latest_price_date(session, active_ids) or active_start
            periods.append(
                PeriodReturn(
                    start_date=active_start,
                    end_date=latest_date,
                    portfolio_return=_mean(
                        [position.return_fraction for position in positions_list]
                    ) or 0.0,
                    benchmark_return=_benchmark_return(
                        session, benchmark_id, active_start, latest_date
                    ),
                    is_completed=False,
                    positions=tuple(positions_list),
                )
            )
    return periods


def _cycle_mark_periods(
    session: Session,
    portfolio: str,
    start_date: date,
    benchmark_id: Optional[int],
    marks: list[PortfolioCycleMark],
    open_rows: list[PortfolioSelection],
    completed_rows: list[PortfolioSelection],
) -> list[PeriodReturn]:
    by_cycle: dict[date, list[PortfolioCycleMark]] = {}
    for mark in marks:
        by_cycle.setdefault(mark.cycle_date, []).append(mark)
    cycle_dates = sorted(by_cycle)
    periods: list[PeriodReturn] = []

    def find_exit(company_id: int, after: date, upto: Optional[date]):
        return next(
            (
                row
                for row in completed_rows
                if row.company_id == company_id
                and row.exit_date is not None
                and row.exit_date > after
                and (upto is None or row.exit_date <= upto)
            ),
            None,
        )

    for current_date, next_date in zip(cycle_dates, cycle_dates[1:]):
        next_by_company = {
            mark.company_id: mark for mark in by_cycle[next_date]
        }
        positions: list[PositionReturn] = []
        for mark in by_cycle[current_date]:
            if mark.ref_price is None or mark.ref_price <= 0:
                continue
            next_mark = next_by_company.get(mark.company_id)
            end_price = next_mark.ref_price if next_mark else None
            if end_price is None:
                exit_row = find_exit(mark.company_id, current_date, next_date)
                end_price = exit_row.exit_price if exit_row else None
            position = _position_return(mark.company_id, mark.ref_price, end_price)
            if position is not None:
                positions.append(position)
        if positions:
            periods.append(
                PeriodReturn(
                    start_date=current_date,
                    end_date=next_date,
                    portfolio_return=_mean(
                        [position.return_fraction for position in positions]
                    ) or 0.0,
                    benchmark_return=_benchmark_return(
                        session, benchmark_id, current_date, next_date
                    ),
                    is_completed=True,
                    positions=tuple(positions),
                )
            )

    latest_cycle = cycle_dates[-1]
    open_by_company = {row.company_id: row for row in open_rows}
    latest_prices = _latest_prices(session, list(open_by_company))
    active_positions: list[PositionReturn] = []
    for mark in by_cycle[latest_cycle]:
        if mark.company_id in open_by_company:
            end_price = latest_prices.get(mark.company_id)
        else:
            exit_row = find_exit(mark.company_id, latest_cycle, None)
            end_price = exit_row.exit_price if exit_row else None
        position = _position_return(mark.company_id, mark.ref_price, end_price)
        if position is not None:
            active_positions.append(position)

    if active_positions:
        latest_date = _latest_price_date(
            session, [position.company_id for position in active_positions]
        ) or latest_cycle
        periods.append(
            PeriodReturn(
                start_date=latest_cycle,
                end_date=latest_date,
                portfolio_return=_mean(
                    [position.return_fraction for position in active_positions]
                ) or 0.0,
                benchmark_return=_benchmark_return(
                    session, benchmark_id, latest_cycle, latest_date
                ),
                is_completed=False,
                positions=tuple(active_positions),
            )
        )
    return periods


def calculate_live_performance(
    session: Session,
    portfolio_name: str,
    tracking_start: date = LIVE_TRACKING_START_DATE,
) -> dict:
    """Build canonical gross live NAV metrics for one portfolio."""

    portfolio = portfolio_name.upper()
    rows = (
        session.query(PortfolioSelection)
        .filter(PortfolioSelection.portfolio == portfolio)
        .order_by(PortfolioSelection.selection_date, PortfolioSelection.id)
        .all()
    )
    if not rows:
        return {
            "total_return_avg": None,
            "active_return_avg": None,
            "win_rate": None,
            "cumulative_return_pct": None,
            "benchmark_cumulative_return_pct": None,
            "alpha_pct": None,
            "performance_method": "cohort_nav_gross",
            "return_basis": "gross_recorded_execution",
            "period_count": 0,
            "closed_trade_count": 0,
            "active_position_count": 0,
        }

    open_rows = [row for row in rows if row.exit_date is None]
    completed_rows = [
        row
        for row in rows
        if row.exit_date is not None and row.selection_date >= tracking_start
    ]
    benchmark_id = _benchmark_company_id(session)
    marks = (
        session.query(PortfolioCycleMark)
        .filter(
            PortfolioCycleMark.portfolio == portfolio,
            PortfolioCycleMark.cycle_date >= tracking_start,
        )
        .order_by(PortfolioCycleMark.cycle_date, PortfolioCycleMark.id)
        .all()
    )

    if marks:
        first_mark = min(mark.cycle_date for mark in marks)
        legacy = _legacy_periods(
            session,
            portfolio,
            tracking_start,
            benchmark_id,
            open_rows,
            completed_rows,
        )
        periods = [
            period
            for period in legacy
            if period.start_date < first_mark and period.is_completed
        ]
        periods.extend(
            _cycle_mark_periods(
                session,
                portfolio,
                tracking_start,
                benchmark_id,
                marks,
                open_rows,
                completed_rows,
            )
        )
        method = "cycle_marks_plus_legacy_gross"
    else:
        periods = _legacy_periods(
            session,
            portfolio,
            tracking_start,
            benchmark_id,
            open_rows,
            completed_rows,
        )
        method = "legacy_cohort_nav_gross"

    periods.sort(key=lambda period: period.start_date)
    portfolio_nav = 1.0
    benchmark_nav = 1.0
    benchmark_complete = bool(periods)
    for period in periods:
        portfolio_nav *= 1.0 + period.portfolio_return
        if period.benchmark_return is None:
            benchmark_complete = False
        else:
            benchmark_nav *= 1.0 + period.benchmark_return

    cumulative_pct = (portfolio_nav - 1.0) * 100.0 if periods else None
    benchmark_pct = (
        (benchmark_nav - 1.0) * 100.0 if benchmark_complete else None
    )
    active_period = next(
        (period for period in reversed(periods) if not period.is_completed),
        None,
    )

    decisive_closed_returns = [
        position.return_fraction
        for row in completed_rows
        if (
            position := _position_return(
                row.company_id, row.entry_price, row.exit_price
            )
        )
        is not None
        and abs(position.return_fraction) > _EPSILON
    ]
    win_rate = (
        sum(value > 0 for value in decisive_closed_returns)
        / len(decisive_closed_returns)
        * 100.0
        if decisive_closed_returns
        else None
    )

    return {
        # Backward-compatible field names; semantics are now portfolio-level.
        "total_return_avg": cumulative_pct,
        "active_return_avg": (
            active_period.portfolio_return * 100.0 if active_period else None
        ),
        "win_rate": win_rate,
        "cumulative_return_pct": cumulative_pct,
        "benchmark_cumulative_return_pct": benchmark_pct,
        "alpha_pct": (
            cumulative_pct - benchmark_pct
            if cumulative_pct is not None and benchmark_pct is not None
            else None
        ),
        "performance_method": method,
        "return_basis": "gross_recorded_execution",
        "period_count": len(periods),
        "closed_trade_count": len(decisive_closed_returns),
        "active_position_count": len(open_rows),
    }
