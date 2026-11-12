from datetime import date

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from scripts_picker.feed_health import price_window, verify_prices
from us_picker.db.schema import Base, Company, DailyPrice, ForwardObservation, PortfolioSelection


@pytest.fixture
def session():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine) as active:
        yield active
    engine.dispose()


def _bar(company, day):
    return DailyPrice(company=company, date=day, close=100, adjusted_close=100)


def _observation(day):
    return ForwardObservation(
        observation_id=day.isoformat(), price_date=day, captured_at="now", payload_json="{}"
    )


def test_window_uses_observations_even_after_prices_are_backfilled(session):
    old, latest = date(2026, 9, 28), date(2026, 11, 9)
    session.add(_bar(Company(ticker="SPY"), latest))
    assert price_window(session, latest, 15) == 15
    session.add(_observation(old))
    assert price_window(session, latest, 15) == (latest - old).days + 7
    assert price_window(session, latest, 75) == 75


def test_verify_counts_only_trading_sessions_and_eligible_positions(session, capsys):
    wednesday, friday, monday = date(2026, 11, 4), date(2026, 11, 6), date(2026, 11, 9)
    spy = Company(ticker="SPY", company_type="INDEX", is_active=False)
    covered = Company(ticker="COVERED", is_active=True)
    missing = Company(ticker="MISSING", is_active=True)
    staged = Company(ticker="STAGED", is_active=True)
    session.add_all([
        _observation(wednesday), _bar(spy, friday), _bar(spy, monday), _bar(covered, monday),
        PortfolioSelection(company=covered, portfolio="ALPHA", selection_date=friday),
        PortfolioSelection(company=missing, portfolio="ALPHA", selection_date=friday),
        PortfolioSelection(company=staged, portfolio="ALPHA", selection_date=date(2026, 11, 10)),
    ])
    verify_prices(session, monday)
    output = capsys.readouterr().out
    assert "2 completed XNYS sessions" in output
    assert "1 still lack a SPY bar" in output
    assert "1 of 2 eligible positions" in output
    assert "COVERED" not in output
    assert "MISSING" not in output


def test_verify_rejects_missing_latest_spy_even_with_equity_bars(session):
    friday, monday = date(2026, 11, 6), date(2026, 11, 9)
    session.add_all([
        _bar(Company(ticker="SPY"), friday),
        _bar(Company(ticker="EQUITY", is_active=True), monday),
    ])
    with pytest.raises(SystemExit, match="valid SPY close"):
        verify_prices(session, monday)
