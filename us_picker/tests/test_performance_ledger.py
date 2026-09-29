"""Canonical cohort/cycle-mark live NAV regression tests."""

from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.schema import (
    Base,
    Company,
    DailyPrice,
    PortfolioCycleMark,
    PortfolioSelection,
)
from us_picker.output.performance import PerformanceTracker
from us_picker.portfolio.performance_ledger import calculate_live_performance


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


def _company(session, ticker, company_type="OPERATING"):
    company = Company(
        ticker=ticker,
        name=ticker,
        company_type=company_type,
        is_active=company_type != "INDEX",
    )
    session.add(company)
    session.flush()
    return company


def _price(session, company, d, close):
    session.add(
        DailyPrice(
            company_id=company.id,
            date=d,
            close=close,
            adjusted_close=close,
        )
    )


def _selection(
    session,
    company,
    selected,
    entry,
    *,
    exit_date=None,
    exit_price=None,
):
    session.add(
        PortfolioSelection(
            portfolio="ALPHA",
            selection_date=selected,
            company_id=company.id,
            entry_price=entry,
            exit_date=exit_date,
            exit_price=exit_price,
        )
    )


def test_legacy_cohorts_compound_period_nav_and_exclude_zero_trade_from_win_rate(session):
    xu100 = _company(session, "XU100", "INDEX")
    companies = [_company(session, f"P{i}") for i in range(7)]

    # Benchmark periods: 0%, +10%, 0% => +10% cumulative.
    for d, close in [
        (date(2026, 5, 21), 100),
        (date(2026, 6, 1), 100),
        (date(2026, 6, 8), 110),
        (date(2026, 7, 1), 120),
        (date(2026, 7, 9), 120),
    ]:
        _price(session, xu100, d, close)

    # Period 1: +10%, -10%, 0% => 0% cohort.
    _selection(
        session, companies[0], date(2026, 5, 21), 100,
        exit_date=date(2026, 6, 1), exit_price=110,
    )
    _selection(
        session, companies[1], date(2026, 5, 21), 100,
        exit_date=date(2026, 6, 1), exit_price=90,
    )
    _selection(
        session, companies[2], date(2026, 5, 21), 100,
        exit_date=date(2026, 6, 1), exit_price=100,
    )
    # Period 2: +10%.
    for company in companies[3:5]:
        _selection(
            session, company, date(2026, 6, 1), 100,
            exit_date=date(2026, 6, 8), exit_price=110,
        )
    # Active period: one open +20%, one realized stop -10% => +5%.
    _selection(session, companies[5], date(2026, 7, 1), 100)
    _price(session, companies[5], date(2026, 7, 9), 120)
    _selection(
        session, companies[6], date(2026, 7, 1), 100,
        exit_date=date(2026, 7, 8), exit_price=90,
    )
    session.commit()

    result = calculate_live_performance(session, "alpha")
    tracker_result = PerformanceTracker(session).calculate_portfolio_performance("alpha")

    assert result["period_count"] == 3
    assert result["cumulative_return_pct"] == pytest.approx(15.5)
    assert result["total_return_avg"] == pytest.approx(15.5)
    assert result["active_return_avg"] == pytest.approx(5.0)
    assert result["benchmark_cumulative_return_pct"] == pytest.approx(10.0)
    assert result["alpha_pct"] == pytest.approx(5.5)
    # Decisive closed trades: 3 wins, 2 losses; the 0% duplicate is excluded.
    assert result["closed_trade_count"] == 5
    assert result["win_rate"] == pytest.approx(60.0)
    assert result["active_position_count"] == 1
    assert result["return_basis"] == "gross_recorded_execution"
    assert tracker_result == result


def test_cycle_marks_replace_continuity_rows_without_double_counting(session):
    xu100 = _company(session, "XU100", "INDEX")
    a, b, c = [_company(session, ticker) for ticker in ("A", "B", "C")]
    for d in (date(2026, 7, 1), date(2026, 7, 15), date(2026, 7, 20)):
        _price(session, xu100, d, 100)

    _selection(session, a, date(2026, 7, 1), 100)
    _selection(
        session, b, date(2026, 7, 1), 100,
        exit_date=date(2026, 7, 10), exit_price=90,
    )
    _selection(session, c, date(2026, 7, 15), 100)
    _price(session, a, date(2026, 7, 20), 121)
    _price(session, c, date(2026, 7, 20), 90)

    session.add_all(
        [
            PortfolioCycleMark(
                portfolio="ALPHA", cycle_date=date(2026, 7, 1),
                company_id=a.id, ref_price=100,
            ),
            PortfolioCycleMark(
                portfolio="ALPHA", cycle_date=date(2026, 7, 1),
                company_id=b.id, ref_price=100,
            ),
            PortfolioCycleMark(
                portfolio="ALPHA", cycle_date=date(2026, 7, 15),
                company_id=a.id, ref_price=110,
            ),
            PortfolioCycleMark(
                portfolio="ALPHA", cycle_date=date(2026, 7, 15),
                company_id=c.id, ref_price=100,
            ),
        ]
    )
    session.commit()

    result = calculate_live_performance(session, "ALPHA")

    # Jul1→Jul15: (+10%-10%)/2 = 0; active: (+10%-10%)/2 = 0.
    assert result["period_count"] == 2
    assert result["cumulative_return_pct"] == pytest.approx(0.0)
    assert result["active_return_avg"] == pytest.approx(0.0)
    assert result["performance_method"] == "cycle_marks_plus_legacy_gross"
