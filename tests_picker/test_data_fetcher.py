"""US DataFetcher: universe, statements, macro and price-adjustment wiring."""

from datetime import date, timedelta

import pandas as pd
import pytest
from rich.console import Console
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import us_picker.data.fetcher as fetcher_module
from us_picker.data.fetcher import DataFetcher, _cpi_yoy_by_availability, infer_split_ratio
from us_picker.data.sources.sp500 import MembershipInterval
from us_picker.db.schema import (
    Base,
    Company,
    CorporateAction,
    DailyPrice,
    FinancialStatement,
    IndexMembership,
    MacroRegime,
)
from us_picker.utils.splits import invalidate_split_cache


@pytest.fixture()
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    invalidate_split_cache()
    s = sessionmaker(bind=engine)()
    yield s
    s.close()
    invalidate_split_cache()


class _StubSEC:
    def __init__(self, tickers: list[dict], profiles: dict[int, dict]):
        self._tickers = tickers
        self._profiles = profiles
        self.submission_calls: list[int] = []

    def fetch_company_tickers(self):
        return pd.DataFrame(self._tickers, columns=["cik", "name", "ticker", "exchange"])

    def fetch_submissions(self, cik):
        self.submission_calls.append(cik)
        return self._profiles.get(cik)


class _StubAlpaca:
    def __init__(self, assets: list[dict], liquidity: dict[str, tuple[float, float]]):
        self._assets = assets
        self._liquidity = liquidity

    def fetch_assets(self, status="active"):
        return self._assets

    def fetch_daily_bars(self, symbols, start, end, adjustment="raw"):
        rows = []
        for symbol in symbols:
            if symbol not in self._liquidity:
                continue
            price, volume = self._liquidity[symbol]
            for i in range(20):
                rows.append(
                    {"symbol": symbol, "date": end - timedelta(days=i), "close": price, "volume": volume}
                )
        return pd.DataFrame(rows)


def _submissions(name: str, sic: str, form: str = "10-K") -> dict:
    return {
        "name": name, "sic": sic, "sicDescription": f"SIC {sic}", "tickers": [], "exchanges": [],
        "filings": {"recent": {"form": ["8-K", form]}},
    }


def _fetcher(session, sec, alpaca):
    fetcher = DataFetcher(session=session, console=Console(quiet=True), sec_client=sec, alpaca_client=alpaca)
    return fetcher


def test_fetch_universe_filters_instruments_liquidity_and_sic(session, monkeypatch):
    sec = _StubSEC(
        tickers=[
            {"cik": 1, "name": "Apple Inc.", "ticker": "AAPL", "exchange": "Nasdaq"},
            {"cik": 2, "name": "Berkshire", "ticker": "BRK-B", "exchange": "NYSE"},
            {"cik": 3, "name": "Tiny Co", "ticker": "TINY", "exchange": "Nasdaq"},
            {"cik": 4, "name": "Blank Check", "ticker": "SPAC", "exchange": "NYSE"},
            {"cik": 5, "name": "OTC Co", "ticker": "OTCX", "exchange": "OTC"},
            {"cik": 6, "name": "Warrant Co", "ticker": "WRNTW", "exchange": "Nasdaq"},
            {"cik": 7, "name": "ASML Holding", "ticker": "ASML", "exchange": "Nasdaq"},
        ],
        profiles={
            1: _submissions("Apple Inc.", "3571"),
            2: _submissions("Berkshire Hathaway", "6331"),
            3: _submissions("Tiny Co", "7372"),
            4: _submissions("Blank Check Acquisition", "6770"),
            6: _submissions("Warrant Co", "7372"),
            7: _submissions("ASML Holding", "3559", form="20-F"),
        },
    )
    alpaca = _StubAlpaca(
        assets=[
            {"symbol": "AAPL", "name": "Apple Inc. Common Stock", "exchange": "NASDAQ", "tradable": True},
            {"symbol": "BRK.B", "name": "Berkshire Hathaway Class B", "exchange": "NYSE", "tradable": True},
            {"symbol": "TINY", "name": "Tiny Co Common", "exchange": "NASDAQ", "tradable": True},
            {"symbol": "SPAC", "name": "Blank Check Corp", "exchange": "NYSE", "tradable": True},
            {"symbol": "WRNTW", "name": "Warrant Co Warrants", "exchange": "NASDAQ", "tradable": True},
            {"symbol": "ASML", "name": "ASML Holding N.V. New York Registry Shares", "exchange": "NASDAQ", "tradable": True},
        ],
        liquidity={
            "AAPL": (230.0, 50_000_000),
            "BRK.B": (480.0, 4_000_000),
            "TINY": (3.0, 10_000_000),  # below the $5 floor
            "SPAC": (10.5, 5_000_000),
            "WRNTW": (1.0, 90_000_000),
            "ASML": (900.0, 2_000_000),
        },
    )
    monkeypatch.setattr(
        "us_picker.data.sources.sp500.fetch_intervals",
        lambda: [MembershipInterval("AAPL", date(1982, 11, 30), None)],
    )

    stats = _fetcher(session, sec, alpaca).fetch_universe()

    companies = {c.ticker: c for c in session.query(Company).all()}
    assert set(companies) == {"AAPL", "BRK.B"}
    assert companies["AAPL"].is_bist100 is True
    assert companies["BRK.B"].is_bist100 is False
    assert companies["AAPL"].cik == 1 and companies["AAPL"].sic == "3571"
    assert stats["bist100_count"] == 1
    assert session.query(IndexMembership).count() == 1


def test_fetch_universe_refuses_empty_sec_list(session, monkeypatch):
    monkeypatch.setattr(
        "us_picker.data.sources.sp500.fetch_intervals",
        lambda: [MembershipInterval("AAPL", date(1982, 11, 30), None)],
    )
    sec = _StubSEC(tickers=[], profiles={})
    alpaca = _StubAlpaca(assets=[], liquidity={})
    session.add(Company(ticker="AAPL", is_active=True, is_bist100=True))
    session.commit()

    with pytest.raises(RuntimeError, match="empty"):
        _fetcher(session, sec, alpaca).fetch_universe()
    assert session.query(Company).one().is_active is True


def test_upsert_financials_keeps_publication_date_and_normalizes_split_shares(session):
    company = Company(ticker="NVDA", is_active=True)
    session.add(company)
    session.flush()
    session.add(
        CorporateAction(
            company_id=company.id, action_date=date(2024, 6, 10), action_type="SPLIT", adjustment_factor=10.0
        )
    )
    session.commit()
    statements = {
        "BALANCE": {
            date(2024, 3, 31): {
                "period_type": "Q1",
                "publication_date": date(2024, 5, 30),
                "items": [
                    {"item_code": "1BL", "desc_tr": "", "desc_eng": "", "value": 1000.0},
                    # cover count taken after the split: 24.6B post-split shares
                    {"item_code": "2OA", "desc_tr": "", "desc_eng": "", "value": 24.6e9, "as_of": "2024-08-20"},
                ],
            }
        }
    }
    fetcher = DataFetcher(session=session, console=Console(quiet=True))
    assert fetcher._upsert_financials(company.id, statements) == 1
    row = session.query(FinancialStatement).one()
    assert row.publication_date == date(2024, 5, 30)
    import json

    shares = next(i for i in json.loads(row.data_json) if i["item_code"] == "2OA")
    assert shares["value"] == pytest.approx(2.46e9)


def test_cpi_yoy_becomes_visible_mid_next_month():
    cpi = pd.Series(
        [100.0 + i for i in range(14)],
        index=[date(2024, 1, 1) + pd.DateOffset(months=i) for i in range(14)],
    )
    cpi.index = [d.date() for d in cpi.index]
    yoy = _cpi_yoy_by_availability(cpi)
    # January-2025 print (index 112 vs 100) is public on 2025-02-15.
    assert date(2025, 2, 15) in yoy.index
    assert yoy[date(2025, 2, 15)] == pytest.approx(0.12)
    assert min(yoy.index) == date(2025, 2, 15)


class _StubFRED:
    def fetch_series(self, series_id, start, end=None):
        values = {
            "DFF": 5.33,
            "DGS10": 4.20,
            "T5YIFR": 2.30,
            "BAA10Y": 3.10,
        }
        if series_id == "CPIAUCSL":
            idx = [date(2023, m, 1) for m in range(1, 13)] + [date(2024, m, 1) for m in range(1, 4)]
            return pd.Series([300.0] * 12 + [309.0] * 3, index=idx)
        return pd.Series([values[series_id]], index=[date(2024, 4, 1)])


def test_fetch_macro_maps_fred_series_to_bist_columns(session, monkeypatch):
    monkeypatch.setattr(
        "us_picker.data.sources.damodaran.fetch_us_erp", lambda: None
    )
    fetcher = DataFetcher(session=session, console=Console(quiet=True), fred_client=_StubFRED())
    fetcher._fetch_settings = {"macro_history_start": "2024-01-01"}
    fetcher.fetch_macro()
    row = session.query(MacroRegime).filter(MacroRegime.date == date(2024, 4, 1)).one()
    assert row.policy_rate_pct == pytest.approx(0.0533)
    assert row.bond_yield_10y_pct == pytest.approx(0.042)
    assert row.inflation_expectation_24m_pct == pytest.approx(0.023)
    assert row.turkey_cds_5y == pytest.approx(310.0)  # Baa-10y spread in bps
    assert row.cpi_yoy_pct == pytest.approx(0.03)


class _StubBars:
    """Vendor re-adjusted history after a dividend; raw closes unchanged."""

    def __init__(self):
        self.full_refetches = []

    def fetch_price_history(self, symbols, start, end):
        return pd.DataFrame(
            [
                {"symbol": "KO", "date": date(2026, 9, 21), "open": 70, "high": 71, "low": 69,
                 "close": 70.0, "volume": 1000, "adjusted_close": 69.3, "source": "ALPACA"},
                {"symbol": "KO", "date": date(2026, 9, 22), "open": 70, "high": 71, "low": 69,
                 "close": 70.5, "volume": 1000, "adjusted_close": 70.5, "source": "ALPACA"},
            ]
        )

    def fetch_daily_bars(self, symbols, start, end, adjustment="raw"):
        self.full_refetches.append(tuple(symbols))
        return pd.DataFrame(
            [
                {"symbol": "KO", "date": date(2026, 9, 18), "close": 68.6},
                {"symbol": "KO", "date": date(2026, 9, 21), "close": 69.3},
            ]
        )

    def fetch_corporate_actions(self, symbols, start, end):
        return pd.DataFrame(columns=["symbol", "action_date", "action_type", "adjustment_factor", "details"])


def test_fetch_prices_rewrites_adjusted_history_when_vendor_factor_moves(session, monkeypatch):
    company = Company(ticker="KO", is_active=True)
    session.add(company)
    session.flush()
    session.add_all(
        [
            DailyPrice(company_id=company.id, date=date(2026, 9, 18), close=69.0, adjusted_close=69.0),
            DailyPrice(company_id=company.id, date=date(2026, 9, 21), close=70.0, adjusted_close=70.0),
        ]
    )
    session.commit()
    bars = _StubBars()
    fetcher = DataFetcher(session=session, console=Console(quiet=True), alpaca_client=bars)
    monkeypatch.setattr(fetcher_module, "_last_complete_session_day", lambda: date(2026, 9, 22))
    monkeypatch.setattr(fetcher, "fetch_benchmark_prices", lambda days_back: {})

    stats = fetcher.fetch_prices(tickers=["KO"], days_back=10)

    assert stats["readjusted"] == 1
    prices = {p.date: p for p in session.query(DailyPrice).all()}
    assert prices[date(2026, 9, 18)].close == 69.0  # raw never touched
    assert prices[date(2026, 9, 18)].adjusted_close == pytest.approx(68.6)
    assert prices[date(2026, 9, 22)].close == 70.5


def test_infer_split_ratio_only_accepts_exact_split_factors():
    assert infer_split_ratio(7.0 * 1.01) == 7.0
    assert infer_split_ratio(0.1) == pytest.approx(0.1)
    assert infer_split_ratio(1.2) is None
    assert infer_split_ratio(2.6) is None


def test_pre_coverage_split_is_inferred_and_shares_become_continuous(session):
    company = Company(ticker="AAPL", is_active=True)
    session.add(company)
    session.commit()

    def balance(value, as_of):
        return {
            "period_type": "Q2",
            "publication_date": as_of,
            "items": [
                {"item_code": "1BL", "desc_tr": "", "desc_eng": "", "value": 1.0},
                {"item_code": "2OA", "desc_tr": "", "desc_eng": "", "value": value, "as_of": as_of.isoformat()},
            ],
        }

    statements = {
        "BALANCE": {
            date(2014, 3, 31): balance(861e6, date(2014, 4, 11)),
            date(2014, 6, 30): balance(5.99e9, date(2014, 7, 11)),  # 7:1 split in June 2014
            date(2014, 9, 30): balance(5.87e9, date(2014, 10, 10)),
        }
    }
    fetcher = DataFetcher(session=session, console=Console(quiet=True))
    assert fetcher._record_inferred_splits(company.id, statements, date(2016, 1, 1)) == 1
    fetcher._upsert_financials(company.id, statements)
    import json

    shares = [
        next(i["value"] for i in json.loads(r.data_json) if i["item_code"] == "2OA")
        for r in session.query(FinancialStatement).order_by(FinancialStatement.period_end)
    ]
    assert shares[1] == pytest.approx(shares[0], rel=0.01)


def test_alternating_share_concepts_are_not_splits(session):
    company = Company(ticker="TSCO", is_active=True)
    session.add(company)
    session.commit()

    def balance(value, as_of):
        return {"period_type": "Q1", "publication_date": as_of, "items": [
            {"item_code": "2OA", "desc_tr": "", "desc_eng": "", "value": value, "as_of": as_of.isoformat()}]}

    statements = {"BALANCE": {
        date(2010, 3, 31): balance(36e6, date(2010, 3, 31)),
        date(2010, 6, 30): balance(72e6, date(2010, 6, 30)),
        date(2010, 9, 30): balance(36e6, date(2010, 9, 30)),
        date(2010, 12, 31): balance(72e6, date(2010, 12, 31)),
    }}
    fetcher = DataFetcher(session=session, console=Console(quiet=True))
    assert fetcher._record_inferred_splits(company.id, statements, date(2016, 1, 1)) == 0
