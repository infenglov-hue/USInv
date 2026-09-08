"""Tests for macroeconomic regime evaluation."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from ai.core.models import BarData, MacroRegimeType
from ai.tools.alpaca_client import AlpacaClient
from ai.tools.macro_tool import MacroTool


def create_bars_with_trend(start_price: float, end_price: float, count: int = 50) -> list[BarData]:
    step = (end_price - start_price) / count
    base_time = datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc)
    return [
        BarData(
            timestamp=base_time + timedelta(days=i),
            open=start_price + i * step,
            high=start_price + i * step + 1.0,
            low=start_price + i * step - 1.0,
            close=start_price + i * step,
            volume=1_000_000.0,
            vwap=start_price + i * step,
        )
        for i in range(count)
    ]


def test_macro_risk_on_detection():
    mock_client = MagicMock(spec=AlpacaClient)

    # Bullish equities, falling UVXY
    mock_client.get_bars.return_value = {
        "SPY": create_bars_with_trend(400, 480),
        "QQQ": create_bars_with_trend(350, 450),
        "TLT": create_bars_with_trend(90, 95),
        "UVXY": create_bars_with_trend(30, 15),  # collapsing vol
        "UUP": create_bars_with_trend(28, 28),
        "HYG": create_bars_with_trend(75, 78),
        "SMH": create_bars_with_trend(200, 260),
    }

    macro_tool = MacroTool(mock_client)
    regime = macro_tool.get_macro_regime()

    assert regime.regime_type == MacroRegimeType.RISK_ON_EXPANSION
    assert "leveraged_tech_semi" in regime.favored_etf_types


def test_macro_risk_off_detection():
    mock_client = MagicMock(spec=AlpacaClient)

    # Crashing equities, spiking UVXY
    mock_client.get_bars.return_value = {
        "SPY": create_bars_with_trend(480, 400),
        "QQQ": create_bars_with_trend(450, 350),
        "TLT": create_bars_with_trend(85, 95),
        "UVXY": create_bars_with_trend(15, 45),  # surging vol
        "UUP": create_bars_with_trend(27, 30),
        "HYG": create_bars_with_trend(78, 70),
        "SMH": create_bars_with_trend(260, 190),
    }

    macro_tool = MacroTool(mock_client)
    regime = macro_tool.get_macro_regime()

    assert regime.regime_type == MacroRegimeType.RISK_OFF_CONTRACTION
    assert "leveraged_inverse_hedge" in regime.favored_etf_types
