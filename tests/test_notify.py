"""Unit tests for Telegram notification client."""

from __future__ import annotations

import json
from datetime import date
from unittest.mock import MagicMock, patch

from usinv.delivery.snapshot import (
    SNAPSHOT_SCHEMA_VERSION,
    DataHealthSummary,
    DeliverySnapshot,
    MacroRegimeSummary,
    OrderSummary,
)
from usinv.delivery.telegram import TelegramNotifier


def _mock_snapshot() -> DeliverySnapshot:
    return DeliverySnapshot(
        schema_version=SNAPSHOT_SCHEMA_VERSION,
        as_of_session="2026-07-17",
        generated_at="2026-07-17T20:00:00Z",
        stale_after="2026-07-20T16:00:00Z",
        config_hash="d3ecd3bd787897c9abfd450d97e33cbfa90de1d9f762d92760789e4ada17cb72",
        code_sha="80f09f3c1d4a89e9f136b6cbef5847db1d12fae2",
        data_manifest_hash="05bdd470475a6c71dd288108a97034fed37000a0492e15cc47a950c3d164ad1b",
        nav=100000.0,
        cash=10000.0,
        positions_value=90000.0,
        positions=[],
        candidates=[],
        macro_regime=MacroRegimeSummary(
            regime="NORMAL",
            overlay="O1",
            equity_exposure_target=1.0,
            signal_summary="Normal conditions",
            benchmark_dd=-0.01,
            as_of_session="2026-07-17",
        ),
        orders=[
            OrderSummary(
                order_id="ord_1",
                client_order_id="cid_1",
                security_id="SEC:1",
                ticker="AAPL",
                side="buy",
                quantity=50.0,
                reference_close=200.0,
                limit_price=204.0,
                collar_pct=0.02,
                status="pending",
                reason="entry",
            )
        ],
        performance_tail=[],
        data_health=DataHealthSummary(
            status="HEALTHY",
            freshness_evaluated_at="2026-07-17T20:00:00Z",
            core_coverage_pct=91.5,
            secondary_coverage_pct=78.8,
            identity_gaps=0,
            sector_gaps=0,
            warnings=[],
        ),
    )


def test_telegram_unconfigured_suppresses_send():
    """Unconfigured client logs safely and returns False without network calls."""
    notifier = TelegramNotifier(bot_token=None, chat_id=None)
    assert not notifier.is_configured
    assert notifier.send_message("Test message") is False


def test_telegram_send_success():
    """Configured client performs POST request and returns True on success."""
    notifier = TelegramNotifier(bot_token="123:TOKEN", chat_id="99999")
    assert notifier.is_configured

    mock_resp = MagicMock()
    mock_resp.read.return_value = json.dumps({"ok": True, "result": {}}).encode("utf-8")
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as mock_urlopen:
        success = notifier.send_message("Hello from USInv")
        assert success is True
        mock_urlopen.assert_called_once()


def test_telegram_notify_decision():
    """Decision notification formats snapshot summary properly."""
    notifier = TelegramNotifier(bot_token="123:TOKEN", chat_id="99999")
    snap = _mock_snapshot()

    with patch.object(notifier, "send_message", return_value=True) as mock_send:
        res = notifier.notify_decision(snap)
        assert res is True
        msg = mock_send.call_args[0][0]
        assert "USInv Decision Pipeline" in msg
        assert "AAPL" in msg
        assert "100,000.00" in msg


def test_telegram_notify_reconciliation_and_alarm():
    """Reconciliation and alarm notifications format correctly."""
    notifier = TelegramNotifier(bot_token="123:TOKEN", chat_id="99999")

    with patch.object(notifier, "send_message", return_value=True) as mock_send:
        # Fill reconciliation
        notifier.notify_fill_reconciliation(
            session=date(2026, 7, 18),
            filled_orders=[{"side": "buy", "ticker": "AAPL", "quantity": 50, "price": 201.5}],
            unfilled_orders=[],
            new_nav=100250.0,
        )
        msg1 = mock_send.call_args[0][0]
        assert "Fill Reconciliation" in msg1
        assert "AAPL filled @ $201.50" in msg1

        # Alarm
        notifier.notify_alarm("Freshness Gate Failure", "Alpaca bars stale by 2 sessions")
        msg2 = mock_send.call_args[0][0]
        assert "USInv ALARM" in msg2
        assert "Freshness Gate Failure" in msg2
