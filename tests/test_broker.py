"""Unit tests for Alpaca broker adapter and deterministic execution contracts."""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock

import pytest

from usinv.broker.alpaca import (
    LIVE_API_URL,
    PAPER_API_URL,
    AlpacaBroker,
    BrokerError,
    make_client_order_id,
)


def test_deterministic_client_order_id():
    """Verify that client order ID generation is stable and deterministic."""
    sess = date(2026, 7, 17)
    sec_id = "SEC:0000320193:AAPL"
    cid1 = make_client_order_id(sess, sec_id, "buy", attempt=1)
    cid2 = make_client_order_id(sess, sec_id, "buy", attempt=1)
    cid_retry = make_client_order_id(sess, sec_id, "buy", attempt=2)
    cid_sell = make_client_order_id(sess, sec_id, "sell", attempt=1)

    assert cid1 == cid2
    assert cid1 == "usinv_20260717_SEC000032019_buy_a1"
    assert cid_retry == "usinv_20260717_SEC000032019_buy_a2"
    assert cid_sell == "usinv_20260717_SEC000032019_sell_a1"


def test_broker_initialization_urls():
    """Verify URL routing between paper and live modes."""
    paper_broker = AlpacaBroker(api_key="key", api_secret="sec", paper=True)
    assert paper_broker.base_url == PAPER_API_URL

    live_broker = AlpacaBroker(api_key="key", api_secret="sec", paper=False)
    assert live_broker.base_url == LIVE_API_URL

    custom_broker = AlpacaBroker(api_key="key", api_secret="sec", base_url="http://mock-broker")
    assert custom_broker.base_url == "http://mock-broker"


def test_broker_requires_credentials_for_requests():
    """Verify that unconfigured broker fails closed."""
    broker = AlpacaBroker(api_key=None, api_secret=None)
    assert not broker.is_configured
    with pytest.raises(BrokerError, match="Alpaca credentials missing"):
        broker.get_account()


def test_loo_order_collar_calculation():
    """Verify LOO limit price calculation on buy and sell collars."""
    broker = AlpacaBroker(api_key="k", api_secret="s")

    # Mock _request to intercept payload
    captured_payloads = []

    def mock_request(endpoint: str, method: str = "GET", payload: dict | None = None):
        captured_payloads.append(payload)
        return {
            "id": "ord_123",
            "client_order_id": payload["client_order_id"],
            "symbol": payload["symbol"],
            "qty": payload["qty"],
            "side": payload["side"],
            "type": payload["type"],
            "time_in_force": payload["time_in_force"],
            "limit_price": payload["limit_price"],
            "status": "accepted",
            "submitted_at": "2026-07-17T13:17:00Z",
        }

    broker._request = mock_request  # type: ignore[assignment]

    # 1. Buy LOO order with 2% collar on $100.00 reference close
    res_buy = broker.submit_loo_order(
        symbol="AAPL",
        qty=10,
        side="buy",
        reference_price=100.00,
        collar_pct=0.02,
        client_order_id="cid_buy_1",
    )
    assert res_buy.limit_price == 102.00
    assert captured_payloads[0]["time_in_force"] == "opg"
    assert captured_payloads[0]["limit_price"] == "102.0"

    # 2. Sell LOO order with 2% collar on $100.00 reference close
    res_sell = broker.submit_loo_order(
        symbol="AAPL",
        qty=10,
        side="sell",
        reference_price=100.00,
        collar_pct=0.02,
        client_order_id="cid_sell_1",
    )
    assert res_sell.limit_price == 98.00
    assert captured_payloads[1]["limit_price"] == "98.0"


def test_preflight_verification():
    """Verify preflight check for account active status and cash sufficiency."""
    broker = AlpacaBroker(api_key="k", api_secret="s")

    # 1. Active account with $15,000 cash passes for $10,000 requirement
    broker.get_account = MagicMock(
        return_value={"status": "ACTIVE", "cash": "15000.00", "trading_blocked": False}
    )  # type: ignore[assignment]
    ok, msg = broker.preflight_check(required_cash=10000.0)
    assert ok is True
    assert "Preflight verified" in msg

    # 2. Insufficient cash fails
    ok, msg = broker.preflight_check(required_cash=20000.0)
    assert ok is False
    assert "Insufficient cash" in msg

    # 3. Inactive account fails
    broker.get_account = MagicMock(
        return_value={"status": "ONBOARDING", "cash": "50000.00", "trading_blocked": False}
    )  # type: ignore[assignment]
    ok, msg = broker.preflight_check(required_cash=5000.0)
    assert ok is False
    assert "not ACTIVE" in msg
