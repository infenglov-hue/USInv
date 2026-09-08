"""Tests for universe screener filtering logic."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from ai.core.models import BarData
from ai.tools.alpaca_client import AlpacaClient
from ai.tools.screener_tool import ScreenerTool
from ai.tools.technical_tool import TechnicalTool


def test_screener_filters():
    mock_client = MagicMock(spec=AlpacaClient)

    base_time = datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc)
    mock_bars = [
        BarData(
            timestamp=base_time + timedelta(days=i),
            open=50.0 + i,
            high=51.0 + i,
            low=49.0 + i,
            close=50.0 + i,
            volume=500_000.0,
            vwap=50.0 + i,
        )
        for i in range(60)
    ]

    mock_client.get_bars.return_value = {
        "SPY": mock_bars,
        "QQQ": mock_bars,
        "TQQQ": mock_bars,
        "SOXL": mock_bars,
    }

    tech_tool = TechnicalTool(mock_client)
    screener = ScreenerTool(mock_client, tech_tool)

    results = screener.screen_universe(categories=["leveraged_tech_semi"])
    assert len(results) > 0

    profile, metrics = results[0]
    assert profile.symbol in ("TQQQ", "SOXL", "TECL", "FNGU", "NVDL", "USD")
    assert metrics.current_price > 0
