"""Tests for data/fetcher.py."""

import io
from datetime import date

import pandas as pd
import pytest
from rich.console import Console
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.data.fetcher import DataFetcher
from us_picker.db.schema import Base, Company


class _StubIsYatirim:
    def fetch_company_overview(self):
        return pd.DataFrame(
            [
                {
                    "ticker": "AAA",
                    "name": "AAA IsY",
                    "sector": "Industrial",
                    "free_float_pct": 35.0,
                },
                {
                    "ticker": "BBB",
                    "name": "BBB IsY",
                    "sector": "Retail",
                    "free_float_pct": 22.0,
                },
            ]
        )

    def fetch_bist100_tickers(self):
        return ["AAA"]


class _StubKAP:
    def fetch_company_list(self):
        return pd.DataFrame(
            [
                {"ticker": "BBB", "name": "BBB KAP"},
                {"ticker": "CCC", "name": "CCC KAP"},
            ]
        )


def _make_fetcher(session):
    fetcher = DataFetcher.__new__(DataFetcher)
    fetcher._session = session
    fetcher._console = Console(file=io.StringIO(), force_terminal=False)
    fetcher._isy = _StubIsYatirim()
    fetcher._kap = _StubKAP()
    fetcher._tcmb = None
    fetcher._yahoo = None
    fetcher._fetch_settings = {}
    return fetcher


class _StubIsYatirimWithoutBist100(_StubIsYatirim):
    def fetch_bist100_tickers(self):
        return []


def test_fetch_universe_uses_union_of_isyatirim_and_kap_sources():
    """Universe loading should not drop tickers that only exist in KAP."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()

    session.add(
        Company(
            ticker="OLD1",
            name="Old Company",
            is_active=True,
        )
    )
    session.commit()

    fetcher = _make_fetcher(session)
    stats = DataFetcher.fetch_universe(fetcher)
    session.commit()

    companies = {
        company.ticker: company
        for company in session.query(Company).order_by(Company.ticker).all()
    }

    assert stats["total"] == 3
    assert set(companies) == {"AAA", "BBB", "CCC", "OLD1"}
    assert companies["AAA"].is_active is True
    assert companies["AAA"].is_bist100 is True
    assert companies["BBB"].name == "BBB KAP"
    assert companies["BBB"].free_float_pct == 22.0
    assert companies["CCC"].is_active is True
    assert companies["CCC"].name == "CCC KAP"
    assert companies["CCC"].free_float_pct is None
    assert companies["OLD1"].is_active is False

    session.close()


def test_fetch_universe_refuses_to_erase_bist100_on_empty_source_result():
    """A transient index-source outage must not clear every membership flag."""
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    session.add(
        Company(
            ticker="AAA",
            name="AAA Existing",
            is_active=True,
            is_bist100=True,
        )
    )
    session.commit()

    fetcher = _make_fetcher(session)
    fetcher._isy = _StubIsYatirimWithoutBist100()

    with pytest.raises(RuntimeError, match="BIST 100 constituent fetch returned no tickers"):
        DataFetcher.fetch_universe(fetcher)

    company = session.query(Company).filter_by(ticker="AAA").one()
    assert company.is_bist100 is True
    assert session.query(Company).count() == 1

    session.close()
