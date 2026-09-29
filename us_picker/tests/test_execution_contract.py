"""Regression tests for the T-1 data -> T effective-session contract."""

from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.schema import (
    Base,
    Company,
    DailyPrice,
    PortfolioCycleMark,
    PortfolioSelection,
)
from us_picker.portfolio.execution import (
    latest_completed_session_date,
    stamp_missing_signal_dates,
    tradable_company_ids,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_latest_completed_session_uses_friday_for_monday_effective_date():
    session = _session()
    companies = [
        Company(ticker=f"EQ{i}", company_type="OPERATING", is_active=True)
        for i in range(4)
    ]
    session.add_all(companies)
    session.flush()
    friday = date(2026, 7, 10)
    monday = date(2026, 7, 13)
    for company in companies:
        session.add(DailyPrice(company_id=company.id, date=friday, close=100.0))
        # Even if an intraday T row exists, it is forbidden as signal input.
        session.add(DailyPrice(company_id=company.id, date=monday, close=101.0))
    session.commit()

    assert latest_completed_session_date(session, monday) == friday


def test_latest_completed_session_ignores_one_ticker_future_stray():
    session = _session()
    companies = [
        Company(ticker=f"EQ{i}", company_type="OPERATING", is_active=True)
        for i in range(6)
    ]
    session.add_all(companies)
    session.flush()
    broad_day = date(2026, 7, 9)
    stray_day = date(2026, 7, 10)
    for company in companies:
        session.add(DailyPrice(company_id=company.id, date=broad_day, close=100.0))
    session.add(DailyPrice(company_id=companies[0].id, date=stray_day, close=101.0))
    session.commit()

    assert latest_completed_session_date(session, date(2026, 7, 13)) == broad_day


def test_tradable_ids_keep_recent_prices_and_exclude_kap_only_rows():
    session = _session()
    recent = Company(ticker="RECENT", company_type="OPERATING", is_active=True)
    stale = Company(ticker="STALE", company_type="OPERATING", is_active=True)
    kap_only = Company(ticker="KAPONLY", company_type="OPERATING", is_active=True)
    session.add_all([recent, stale, kap_only])
    session.flush()
    as_of = date(2026, 7, 10)
    session.add_all(
        [
            DailyPrice(company_id=recent.id, date=as_of, close=100.0),
            DailyPrice(
                company_id=stale.id,
                date=date(2026, 4, 1),
                close=50.0,
            ),
        ]
    )
    session.commit()

    assert tradable_company_ids(session, as_of) == {recent.id}


def test_stamp_missing_signal_dates_preserves_model_reference_prices():
    session = _session()
    company = Company(
        ticker="ASELS",
        name="Aselsan",
        company_type="OPERATING",
        is_active=True,
    )
    session.add(company)
    session.flush()
    effective = date(2026, 7, 13)
    signal = date(2026, 7, 10)
    selection = PortfolioSelection(
        portfolio="ALPHA",
        selection_date=effective,
        company_id=company.id,
        entry_price=370.0,
        cycle_ref_date=effective,
        cycle_ref_price=370.0,
    )
    mark = PortfolioCycleMark(
        portfolio="ALPHA",
        cycle_date=effective,
        company_id=company.id,
        ref_price=370.0,
    )
    continued = PortfolioSelection(
        portfolio="ALPHA",
        selection_date=date(2026, 7, 1),
        signal_date=date(2026, 6, 30),
        company_id=company.id,
        entry_price=340.0,
        cycle_ref_date=effective,
        cycle_ref_price=370.0,
    )
    session.add_all([selection, mark, continued])
    session.commit()

    assert stamp_missing_signal_dates(session, effective, signal) == (2, 1)
    session.commit()

    assert selection.signal_date == signal
    assert selection.cycle_signal_date == signal
    assert selection.entry_price == 370.0
    assert mark.signal_date == signal
    assert mark.ref_price == 370.0
    assert continued.signal_date == date(2026, 6, 30)
    assert continued.cycle_signal_date == signal
    assert continued.entry_price == 340.0
    assert stamp_missing_signal_dates(session, effective, signal) == (0, 0)
