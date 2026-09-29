"""Tests for the public PWA near-live price feed."""

import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.schema import Base, Company, DailyPrice, PortfolioSelection
from us_picker.live_prices import (
    build_live_price_feed,
    build_live_ticker_universe,
    load_live_tickers,
)


class _FakeAlpaca:
    """Stands in for AlpacaClient.fetch_snapshots."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def fetch_snapshots(self, symbols, feed="iex"):
        self.calls.append((list(symbols), feed))
        result = self._responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def _snapshots() -> dict:
    return {
        "AAPL": {"latestTrade": {"p": 228.5, "t": "2026-06-23T14:59:58.123456Z"}},
        "SPY": {"latestTrade": {"p": 612.25, "t": "2026-06-23T14:59:59Z"}},
    }


def test_build_live_price_feed_normalizes_symbols_and_metadata():
    client = _FakeAlpaca([_snapshots()])
    generated_at = datetime(2026, 6, 23, 15, 0, tzinfo=timezone.utc)
    payload = build_live_price_feed(["AAPL", "SPY"], client=client, generated_at=generated_at)

    assert payload["generated_at"] == "2026-06-23T15:00:00+00:00"
    assert payload["success_count"] == 2
    assert payload["failed_tickers"] == []
    assert payload["prices"]["AAPL"]["price"] == pytest.approx(228.5)
    assert payload["prices"]["AAPL"]["currency"] == "USD"
    assert payload["prices"]["SPY"]["quote_time"] == "2026-06-23T14:59:59+00:00"
    assert client.calls == [(["AAPL", "SPY"], "iex")]


def test_build_live_price_feed_ignores_blank_and_none_tickers():
    client = _FakeAlpaca([_snapshots()])
    payload = build_live_price_feed(["AAPL", None, " ", "aapl", "SPY"], client=client)

    assert payload["requested_count"] == 2
    assert client.calls[0][0] == ["AAPL", "SPY"]


@patch("us_picker.live_prices.time.sleep")
def test_build_live_price_feed_retries_transient_failure(mock_sleep):
    client = _FakeAlpaca([ConnectionError("temporary"), _snapshots()])

    payload = build_live_price_feed(["AAPL", "SPY"], client=client)

    assert payload["success_count"] == 2
    assert len(client.calls) == 2
    mock_sleep.assert_called_once_with(1)


def test_build_live_price_feed_rejects_mostly_empty_result():
    client = _FakeAlpaca([{}])

    with pytest.raises(RuntimeError, match="0/2 quotes succeeded"):
        build_live_price_feed(["AAPL", "SPY"], client=client)


def test_live_ticker_universe_excludes_unpriced_kap_rows_but_keeps_open_position():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    session = sessionmaker(bind=engine)()
    recent = Company(ticker="RECENT", is_active=True, company_type="OPERATING")
    kap_only = Company(ticker="KAPONLY", is_active=True, company_type="OPERATING")
    open_stale = Company(ticker="OPENOLD", is_active=False, company_type="OPERATING")
    session.add_all([recent, kap_only, open_stale])
    session.flush()
    latest = date(2026, 7, 10)
    session.add_all(
        [
            DailyPrice(company_id=recent.id, date=latest, close=100.0),
            DailyPrice(
                company_id=open_stale.id,
                date=latest - timedelta(days=60),
                close=50.0,
            ),
            PortfolioSelection(
                portfolio="ALPHA",
                selection_date=latest,
                company_id=open_stale.id,
                entry_price=50.0,
            ),
        ]
    )
    session.commit()

    payload = build_live_ticker_universe(session)

    assert payload["latest_price_date"] == "2026-07-10"
    assert payload["tickers"] == ["OPENOLD", "RECENT", "SPY"]
    assert "KAPONLY" not in payload["tickers"]


def test_load_live_tickers_validates_and_normalizes(tmp_path):
    path = tmp_path / "live_tickers.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tickers": ["aapl", " SPY ", "AAPL"],
            }
        ),
        encoding="utf-8",
    )

    assert load_live_tickers(path) == ["AAPL", "SPY"]
