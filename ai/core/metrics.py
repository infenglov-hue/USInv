"""Statistical and quantitative indicators calculation engine for high-risk funds and ETFs."""

import math
import numpy as np
import pandas as pd

from ai.core.models import BarData, QuantMetrics


def calculate_quant_metrics(
    symbol: str,
    bars: list[BarData],
    benchmark_spy_bars: list[BarData] | None = None,
    benchmark_qqq_bars: list[BarData] | None = None,
    min_volume_threshold: float = 200000.0,
    min_dollar_vol_threshold: float = 5000000.0,
    min_volatility_threshold: float = 0.25,
    risk_free_rate: float = 0.04,
) -> QuantMetrics:
    """Calculate comprehensive technical, momentum, and risk metrics for an ETF."""
    if not bars or len(bars) < 10:
        return QuantMetrics(
            symbol=symbol,
            current_price=bars[-1].close if bars else 0.0,
            is_liquid=False,
            is_high_risk=False,
        )

    df = pd.DataFrame([b.model_dump() for b in bars])
    df.sort_values("timestamp", inplace=True)
    df.reset_index(drop=True, inplace=True)

    closes = df["close"].values
    volumes = df["volume"].values
    highs = df["high"].values
    lows = df["low"].values
    n = len(closes)

    current_price = float(closes[-1])

    # Daily returns
    returns = np.diff(closes) / closes[:-1]

    # Cumulative returns
    ret_5d = float((closes[-1] / closes[-5] - 1.0)) if n >= 5 else 0.0
    ret_20d = float((closes[-1] / closes[-20] - 1.0)) if n >= 20 else 0.0
    ret_60d = float((closes[-1] / closes[-60] - 1.0)) if n >= 60 else (float(closes[-1] / closes[0] - 1.0) if n > 1 else 0.0)
    ret_120d = float((closes[-1] / closes[-120] - 1.0)) if n >= 120 else (float(closes[-1] / closes[0] - 1.0) if n > 1 else 0.0)

    # Volatility & Downside Deviation
    std_daily = float(np.std(returns)) if len(returns) > 1 else 0.0
    ann_vol = float(std_daily * math.sqrt(252))

    downside_returns = returns[returns < 0]
    if len(downside_returns) > 1:
        downside_std = float(np.sqrt(np.mean(downside_returns**2)) * math.sqrt(252))
    else:
        downside_std = max(ann_vol * 0.7, 0.001)

    mean_return_ann = float(np.mean(returns) * 252) if len(returns) > 0 else 0.0

    sharpe = float((mean_return_ann - risk_free_rate) / ann_vol) if ann_vol > 0 else 0.0
    sortino = float((mean_return_ann - risk_free_rate) / downside_std) if downside_std > 0 else 0.0

    # Max Drawdown (last 60 days)
    lookback_window = min(n, 60)
    window_closes = closes[-lookback_window:]
    cum_max = np.maximum.accumulate(window_closes)
    drawdowns = (window_closes - cum_max) / cum_max
    max_dd = float(np.min(drawdowns)) if len(drawdowns) > 0 else 0.0

    # Moving averages
    ema_20 = float(pd.Series(closes).ewm(span=20, adjust=False).mean().iloc[-1])
    sma_50 = float(pd.Series(closes).rolling(50, min_periods=10).mean().iloc[-1])
    sma_200 = float(pd.Series(closes).rolling(200, min_periods=50).mean().iloc[-1]) if n >= 50 else None

    # Trend alignment
    if sma_200 is not None:
        if current_price > ema_20 and ema_20 > sma_50 and sma_50 > sma_200:
            trend = "STRONG_BULLISH"
        elif current_price > ema_20 and ema_20 > sma_50:
            trend = "BULLISH"
        elif current_price < ema_20 and ema_20 < sma_50 and sma_50 < sma_200:
            trend = "STRONG_BEARISH"
        elif current_price < ema_20 and ema_20 < sma_50:
            trend = "BEARISH"
        else:
            trend = "NEUTRAL"
    else:
        if current_price > ema_20 and ema_20 > sma_50:
            trend = "BULLISH"
        elif current_price < ema_20 and ema_20 < sma_50:
            trend = "BEARISH"
        else:
            trend = "NEUTRAL"

    # RSI-14
    rsi_14 = calculate_rsi(closes, period=14)

    # Beta vs Benchmarks
    beta_spy = calculate_beta(bars, benchmark_spy_bars) if benchmark_spy_bars else 1.0
    beta_qqq = calculate_beta(bars, benchmark_qqq_bars) if benchmark_qqq_bars else 1.0

    # Liquidity metrics
    vol_window = min(n, 20)
    avg_vol_20d = float(np.mean(volumes[-vol_window:]))
    avg_dollar_vol_20d = float(avg_vol_20d * current_price)

    is_liquid = (avg_vol_20d >= min_volume_threshold) and (avg_dollar_vol_20d >= min_dollar_vol_threshold)
    is_high_risk = ann_vol >= min_volatility_threshold or abs(beta_qqq) >= 1.5

    return QuantMetrics(
        symbol=symbol,
        current_price=current_price,
        return_5d=round(ret_5d, 4),
        return_20d=round(ret_20d, 4),
        return_60d=round(ret_60d, 4),
        return_120d=round(ret_120d, 4),
        annualized_volatility=round(ann_vol, 4),
        downside_deviation=round(downside_std, 4),
        sortino_ratio=round(sortino, 2),
        sharpe_ratio=round(sharpe, 2),
        max_drawdown_60d=round(max_dd, 4),
        rsi_14=round(rsi_14, 2),
        ema_20=round(ema_20, 2),
        sma_50=round(sma_50, 2),
        sma_200=round(sma_200, 2) if sma_200 is not None else None,
        trend_alignment=trend,
        beta_spy=round(beta_spy, 2),
        beta_qqq=round(beta_qqq, 2),
        avg_volume_20d=round(avg_vol_20d, 0),
        avg_dollar_volume_20d=round(avg_dollar_vol_20d, 0),
        is_liquid=is_liquid,
        is_high_risk=is_high_risk,
    )


def calculate_rsi(closes: np.ndarray, period: int = 14) -> float:
    """Calculate Wilder's Relative Strength Index (RSI)."""
    if len(closes) < period + 1:
        return 50.0

    deltas = np.diff(closes)
    gains = np.where(deltas > 0, deltas, 0.0)
    losses = np.where(deltas < 0, -deltas, 0.0)

    avg_gain = np.mean(gains[:period])
    avg_loss = np.mean(losses[:period])

    for i in range(period, len(deltas)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period

    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0

    rs = avg_gain / avg_loss
    rsi = 100.0 - (100.0 / (1.0 + rs))
    return float(rsi)


def calculate_atr(bars: list[BarData], period: int = 14) -> float:
    """Calculate Average True Range (ATR) for volatility stop loss sizing."""
    if len(bars) < 2:
        return bars[-1].close * 0.05 if bars else 1.0

    highs = [b.high for b in bars]
    lows = [b.low for b in bars]
    closes = [b.close for b in bars]

    tr_list = []
    for i in range(1, len(bars)):
        tr = max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        tr_list.append(tr)

    if not tr_list:
        return closes[-1] * 0.05

    window = min(len(tr_list), period)
    return float(np.mean(tr_list[-window:]))


def calculate_beta(asset_bars: list[BarData], benchmark_bars: list[BarData]) -> float:
    """Calculate beta of asset relative to benchmark using aligned timestamps."""
    if not asset_bars or not benchmark_bars:
        return 1.0

    df_asset = pd.DataFrame([{"t": b.timestamp.date(), "c_a": b.close} for b in asset_bars])
    df_bench = pd.DataFrame([{"t": b.timestamp.date(), "c_b": b.close} for b in benchmark_bars])

    merged = pd.merge(df_asset, df_bench, on="t").sort_values("t")
    if len(merged) < 10:
        return 1.0

    ret_a = merged["c_a"].pct_change().dropna()
    ret_b = merged["c_b"].pct_change().dropna()

    cov_matrix = np.cov(ret_a, ret_b)
    var_bench = cov_matrix[1, 1]

    if var_bench == 0:
        return 1.0

    cov_ab = cov_matrix[0, 1]
    return float(cov_ab / var_bench)
