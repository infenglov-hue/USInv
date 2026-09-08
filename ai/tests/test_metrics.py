"""Tests for statistical metrics, volatility, drawdown, and RSI."""

from datetime import datetime, timedelta, timezone
from ai.core.metrics import calculate_atr, calculate_quant_metrics, calculate_rsi
from ai.core.models import BarData


def generate_mock_bars(count: int = 100, trend: float = 0.002, volatility: float = 0.02) -> list[BarData]:
    bars = []
    base_time = datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc)
    price = 100.0
    for i in range(count):
        price = price * (1.0 + trend + (volatility if i % 2 == 0 else -volatility * 0.9))
        bars.append(
            BarData(
                timestamp=base_time + timedelta(days=i),
                open=price * 0.99,
                high=price * 1.02,
                low=price * 0.98,
                close=price,
                volume=1_000_000.0,
                vwap=price,
            )
        )
    return bars


def test_calculate_rsi_neutral_and_extreme():
    import numpy as np

    # Perfectly rising prices -> RSI 100
    rising = np.linspace(50, 100, 30)
    rsi_up = calculate_rsi(rising, period=14)
    assert rsi_up > 90.0

    # Falling prices -> RSI low
    falling = np.linspace(100, 50, 30)
    rsi_down = calculate_rsi(falling, period=14)
    assert rsi_down < 15.0


def test_calculate_atr():
    bars = generate_mock_bars(20)
    atr = calculate_atr(bars, period=14)
    assert atr > 0.0
    # For price around ~100 with 4% daily range, ATR should be roughly ~3-5
    assert 1.0 < atr < 10.0


def test_calculate_quant_metrics():
    bars = generate_mock_bars(100, trend=0.003, volatility=0.025)
    bench_bars = generate_mock_bars(100, trend=0.001, volatility=0.01)

    metrics = calculate_quant_metrics(
        symbol="TQQQ",
        bars=bars,
        benchmark_spy_bars=bench_bars,
        benchmark_qqq_bars=bench_bars,
    )

    assert metrics.symbol == "TQQQ"
    assert metrics.current_price > 0.0
    assert metrics.annualized_volatility > 0.20
    assert metrics.is_liquid is True
    assert metrics.is_high_risk is True
    assert metrics.rsi_14 > 0.0
    assert metrics.sortino_ratio != 0.0
