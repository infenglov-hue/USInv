"""Net-of-cost portfolio metrics registered in EXPERIMENT_PLAN §2."""

from __future__ import annotations

import math
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from usinv.ledger import NavRecord


class MetricsError(ValueError):
    """Raised when a metric series is incomplete or non-positive."""


@dataclass(frozen=True, slots=True)
class BacktestMetrics:
    cagr: float
    sharpe: float
    sortino: float
    max_drawdown: float
    ulcer_index: float
    rolling_12m_win_rate: float
    one_way_turnover: float
    benchmark_relative_alpha: float
    regression_alpha: float


def _returns(values: tuple[float, ...]) -> tuple[float, ...]:
    return tuple(values[index] / values[index - 1] - 1 for index in range(1, len(values)))


def _cagr(values: tuple[float, ...], sessions: tuple[date, ...]) -> float:
    years = (sessions[-1] - sessions[0]).days / 365.2425
    if years <= 0:
        return 0.0
    return (values[-1] / values[0]) ** (1 / years) - 1


def _sample_std(values: tuple[float, ...]) -> float:
    if len(values) < 2:
        return 0.0
    mean = sum(values) / len(values)
    return math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def _annualized_ratio(values: tuple[float, ...], *, downside_only: bool) -> float:
    if not values:
        return 0.0
    mean = sum(values) / len(values)
    sample = tuple(min(value, 0.0) for value in values) if downside_only else values
    denominator = _sample_std(sample)
    if denominator == 0:
        return 0.0 if mean == 0 else math.copysign(math.inf, mean)
    return mean / denominator * math.sqrt(252)


def _month_ends(sessions: tuple[date, ...], values: tuple[float, ...]) -> tuple[float, ...]:
    sampled: OrderedDict[tuple[int, int], float] = OrderedDict()
    for session, value in zip(sessions, values, strict=True):
        sampled[(session.year, session.month)] = value
    return tuple(sampled.values())


def compute_metrics(
    nav: tuple[NavRecord, ...],
    benchmark_nav: tuple[Decimal, ...],
    *,
    gross_traded_notional: Decimal,
) -> BacktestMetrics:
    """Compute the fixed metric set from canonical NAV and benchmark rows."""
    if len(nav) != len(benchmark_nav) or len(nav) < 2:
        raise MetricsError("strategy and benchmark require matching series with at least two rows")
    sessions = tuple(item.session for item in nav)
    if tuple(sorted(sessions)) != sessions or len(set(sessions)) != len(sessions):
        raise MetricsError("NAV sessions must be unique and ordered")
    strategy = tuple(float(item.nav) for item in nav)
    benchmark = tuple(float(value) for value in benchmark_nav)
    if any(value <= 0 or not math.isfinite(value) for value in (*strategy, *benchmark)):
        raise MetricsError("NAV values must be finite and positive")
    if not gross_traded_notional.is_finite() or gross_traded_notional < 0:
        raise MetricsError("traded notional must be finite and non-negative")

    returns = _returns(strategy)
    benchmark_returns = _returns(benchmark)
    peaks: list[float] = []
    peak = strategy[0]
    for value in strategy:
        peak = max(peak, value)
        peaks.append(peak)
    drawdowns = tuple(value / high - 1 for value, high in zip(strategy, peaks, strict=True))

    strategy_monthly = _month_ends(sessions, strategy)
    benchmark_monthly = _month_ends(sessions, benchmark)
    comparisons = [
        strategy_monthly[index] / strategy_monthly[index - 12]
        > benchmark_monthly[index] / benchmark_monthly[index - 12]
        for index in range(12, len(strategy_monthly))
    ]
    rolling_win_rate = (
        sum(comparisons) / len(comparisons) if comparisons else 0.0
    )

    strategy_cagr = _cagr(strategy, sessions)
    benchmark_cagr = _cagr(benchmark, sessions)
    mean_benchmark = sum(benchmark_returns) / len(benchmark_returns)
    variance_benchmark = sum(
        (value - mean_benchmark) ** 2 for value in benchmark_returns
    )
    if variance_benchmark == 0:
        regression_alpha = 0.0
    else:
        mean_strategy = sum(returns) / len(returns)
        beta = sum(
            (strategy_return - mean_strategy) * (benchmark_return - mean_benchmark)
            for strategy_return, benchmark_return in zip(
                returns, benchmark_returns, strict=True
            )
        ) / variance_benchmark
        regression_alpha = (mean_strategy - beta * mean_benchmark) * 252

    average_nav = sum(strategy) / len(strategy)
    return BacktestMetrics(
        cagr=strategy_cagr,
        sharpe=_annualized_ratio(returns, downside_only=False),
        sortino=_annualized_ratio(returns, downside_only=True),
        max_drawdown=min(drawdowns),
        ulcer_index=math.sqrt(sum(value**2 for value in drawdowns) / len(drawdowns)),
        rolling_12m_win_rate=rolling_win_rate,
        one_way_turnover=0.5 * float(gross_traded_notional) / average_nav,
        benchmark_relative_alpha=strategy_cagr - benchmark_cagr,
        regression_alpha=regression_alpha,
    )
