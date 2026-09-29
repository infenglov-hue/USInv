"""Regression tests for finalized XU100 daily-bar persistence."""

from datetime import date, timedelta

import pandas as pd
import pytest
from rich.console import Console
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.data.fetcher import DataFetcher
from us_picker.db.schema import Base, Company, DailyPrice


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    sess = sessionmaker(bind=engine)()
    yield sess
    sess.close()


class _FakeYahoo:
    def __init__(self, close: float):
        self.close = close
        self.calls = []

    def fetch_index_data(self, start_date, end_date):
        self.calls.append((start_date, end_date))
        return pd.DataFrame(
            [
                {
                    "date": end_date,
                    "open": self.close - 10,
                    "high": self.close + 20,
                    "low": self.close - 20,
                    "close": self.close,
                    "volume": 123,
                    "adjusted_close": self.close,
                    "source": "XU100",
                }
            ]
        )


def _fetcher(session, close=14_000.0):
    fetcher = DataFetcher.__new__(DataFetcher)
    fetcher._session = session
    fetcher._console = Console(quiet=True)
    fetcher._yahoo = _FakeYahoo(close)
    return fetcher


def test_benchmark_excludes_today_and_upserts_legacy_partial_row(session):
    fetcher = _fetcher(session, close=14_000.0)
    fetcher.fetch_benchmark_prices(days_back=30)

    expected_end = date.today() - timedelta(days=1)
    assert fetcher._yahoo.calls[-1][1] == expected_end
    company = session.query(Company).filter_by(ticker="XU100").one()
    row = session.query(DailyPrice).filter_by(
        company_id=company.id, date=expected_end
    ).one()
    assert row.close == 14_000.0

    # A later fetch of the same completed date must replace a partial/incorrect
    # value instead of being frozen by OR IGNORE.
    fetcher._yahoo.close = 14_350.6
    fetcher.fetch_benchmark_prices(days_back=30)
    session.expire_all()
    row = session.query(DailyPrice).filter_by(
        company_id=company.id, date=expected_end
    ).one()
    assert row.close == pytest.approx(14_350.6)
    assert row.adjusted_close == pytest.approx(14_350.6)


def test_default_equity_bulk_insert_remains_insert_only(session):
    company = Company(
        ticker="TESTP", name="TESTP", company_type="OPERATING", is_active=True
    )
    session.add(company)
    session.flush()
    d = date(2026, 7, 1)
    session.add(
        DailyPrice(
            company_id=company.id,
            date=d,
            close=100.0,
            adjusted_close=90.0,
            source="ISYATIRIM",
        )
    )
    session.commit()

    fetcher = _fetcher(session)
    fetcher._bulk_insert_prices(
        [
            {
                "company_id": company.id,
                "date": d,
                "open": 200.0,
                "high": 200.0,
                "low": 200.0,
                "close": 200.0,
                "volume": 1,
                "adjusted_close": 200.0,
                "source": "ISYATIRIM",
            }
        ]
    )
    session.expire_all()
    row = session.query(DailyPrice).filter_by(company_id=company.id, date=d).one()
    assert row.close == 100.0
    assert row.adjusted_close == 90.0
