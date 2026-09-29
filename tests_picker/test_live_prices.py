"""Tests for the public PWA near-live price feed."""

import json
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
import requests
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.schema import Base, Company, DailyPrice, PortfolioSelection
from us_picker.live_prices import (
    build_live_price_feed,
    build_live_ticker_universe,
    load_live_tickers,
)


def _response_payload() -> dict:
    return {
        "spark": {
            "result": [
                {
                    "symbol": "ASELS.IS",
                    "response": [
                        {
                            "meta": {
                                "regularMarketPrice": 393.75,
                                "regularMarketTime": 1_782_225_826,
                                "currency": "TRY",
                                "exchangeName": "IST",
                            }
                        }
                    ],
                },
                {
                    "symbol": "XU100.IS",
                    "response": [
                        {
                            "meta": {
                                "regularMarketPrice": 14_516.32,
                                "regularMarketTime": 1_782_225_826,
                                "currency": "TRY",
                                "exchangeName": "IST",
                            }
                        }
                    ],
                },
            ],
            "error": None,
        }
    }


def test_build_live_price_feed_normalizes_symbols_and_metadata():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = _response_payload()
    http = MagicMock()
    http.get.return_value = response

    generated_at = datetime(2026, 6, 23, 15, 0, tzinfo=timezone.utc)
    payload = build_live_price_feed(
        ["ASELS", "XU100"],
        http=http,
        generated_at=generated_at,
    )

    assert payload["generated_at"] == "2026-06-23T15:00:00+00:00"
    assert payload["success_count"] == 2
    assert payload["failed_tickers"] == []
    assert payload["prices"]["ASELS"]["price"] == pytest.approx(393.75)
    assert payload["prices"]["XU100"]["price"] == pytest.approx(14_516.32)
    params = http.get.call_args.kwargs["params"]
    assert params["symbols"] == "ASELS.IS,XU100.IS"
    assert params["interval"] == "1d"


def test_build_live_price_feed_ignores_blank_and_none_tickers():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = _response_payload()
    http = MagicMock()
    http.get.return_value = response

    payload = build_live_price_feed(["ASELS", None, " ", "asels", "XU100"], http=http)

    assert payload["requested_count"] == 2
    params = http.get.call_args.kwargs["params"]
    assert params["symbols"] == "ASELS.IS,XU100.IS"


@patch("us_picker.live_prices.time.sleep")
def test_build_live_price_feed_retries_transient_failure(mock_sleep):
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = _response_payload()
    http = MagicMock()
    http.get.side_effect = [requests.ConnectionError("temporary"), response]

    payload = build_live_price_feed(["ASELS", "XU100"], http=http)

    assert payload["success_count"] == 2
    assert http.get.call_count == 2
    mock_sleep.assert_called_once_with(1)


def test_build_live_price_feed_rejects_mostly_empty_result():
    response = MagicMock()
    response.raise_for_status.return_value = None
    response.json.return_value = {"spark": {"result": [], "error": None}}
    http = MagicMock()
    http.get.return_value = response

    with pytest.raises(RuntimeError, match="0/2 quotes succeeded"):
        build_live_price_feed(["ASELS", "XU100"], http=http)


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
    assert payload["tickers"] == ["OPENOLD", "RECENT", "XU100"]
    assert "KAPONLY" not in payload["tickers"]


def test_load_live_tickers_validates_and_normalizes(tmp_path):
    path = tmp_path / "live_tickers.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tickers": ["asels", " XU100 ", "ASELS"],
            }
        ),
        encoding="utf-8",
    )

    assert load_live_tickers(path) == ["ASELS", "XU100"]
