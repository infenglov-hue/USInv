"""Net-of-cost portfolio metrics registered in EXPERIMENT_PLAN §2."""

from __future__ import annotations

import math
import random
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


@dataclass(frozen=True, slots=True)
class ConfidenceInterval:
    lower: float
    upper: float


@dataclass(frozen=True, slots=True)
class BootstrapIntervals:
    annualized_active_return_80: ConfidenceInterval
    annualized_active_return_95: ConfidenceInterval
    sharpe_80: ConfidenceInterval
    sharpe_95: ConfidenceInterval
    samples: int
    expected_block_length: int
    seed: int


@dataclass(frozen=True, slots=True)
class SteadyReturnsAssessment:
    ulcer_pass: bool
    max_drawdown_pass: bool
    rolling_12m_win_rate_pass: bool
    calendar_year_pass: bool

    @property
    def passes(self) -> bool:
        return all(
            (
                self.ulcer_pass,
                self.max_drawdown_pass,
                self.rolling_12m_win_rate_pass,
                self.calendar_year_pass,
            )
        )


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


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _interval(values: list[float], coverage: float) -> ConfidenceInterval:
    tail = (1 - coverage) / 2
    return ConfidenceInterval(_percentile(values, tail), _percentile(values, 1 - tail))


def stationary_block_bootstrap(
    strategy_monthly_returns: tuple[float, ...],
    benchmark_monthly_returns: tuple[float, ...],
    *,
    samples: int = 2_000,
    expected_block_length: int = 6,
    seed: int = 0,
) -> BootstrapIntervals:
    """Stationary-block bootstrap CIs for annualized active return and Sharpe."""
    if (
        len(strategy_monthly_returns) != len(benchmark_monthly_returns)
        or len(strategy_monthly_returns) < 2
    ):
        raise MetricsError("bootstrap requires matching monthly return series")
    if samples <= 0 or not 1 <= expected_block_length <= len(strategy_monthly_returns):
        raise MetricsError("bootstrap sample count or block length is invalid")
    if any(
        not math.isfinite(value) or value <= -1
        for value in (*strategy_monthly_returns, *benchmark_monthly_returns)
    ):
        raise MetricsError("bootstrap returns must be finite and greater than -100%")

    generator = random.Random(seed)
    restart_probability = 1 / expected_block_length
    observations = len(strategy_monthly_returns)
    active_returns: list[float] = []
    sharpes: list[float] = []
    for _ in range(samples):
        indices = [generator.randrange(observations)]
        for _index in range(1, observations):
            if generator.random() < restart_probability:
                indices.append(generator.randrange(observations))
            else:
                indices.append((indices[-1] + 1) % observations)
        strategy = tuple(strategy_monthly_returns[index] for index in indices)
        benchmark = tuple(benchmark_monthly_returns[index] for index in indices)
        strategy_growth = math.prod(1 + value for value in strategy)
        benchmark_growth = math.prod(1 + value for value in benchmark)
        active_returns.append(
            strategy_growth ** (12 / observations) - benchmark_growth ** (12 / observations)
        )
        standard_deviation = _sample_std(strategy)
        sharpes.append(
            0.0
            if standard_deviation == 0
            else (sum(strategy) / observations) / standard_deviation * math.sqrt(12)
        )
    return BootstrapIntervals(
        annualized_active_return_80=_interval(active_returns, 0.80),
        annualized_active_return_95=_interval(active_returns, 0.95),
        sharpe_80=_interval(sharpes, 0.80),
        sharpe_95=_interval(sharpes, 0.95),
        samples=samples,
        expected_block_length=expected_block_length,
        seed=seed,
    )


def assess_steady_returns(
    nav: tuple[NavRecord, ...],
    benchmark_nav: tuple[Decimal, ...],
    *,
    strategy_metrics: BacktestMetrics,
    benchmark_max_drawdown: float,
    benchmark_ulcer_index: float,
) -> SteadyReturnsAssessment:
    """Apply the fixed Ulcer, MaxDD, rolling-win and calendar-year constraints."""
    if len(nav) != len(benchmark_nav) or not nav:
        raise MetricsError("steady-return assessment requires matching NAV rows")
    strategy_by_year: OrderedDict[int, tuple[float, float]] = OrderedDict()
    benchmark_by_year: OrderedDict[int, tuple[float, float]] = OrderedDict()
    for item, benchmark_value in zip(nav, benchmark_nav, strict=True):
        strategy_value = float(item.nav)
        benchmark_float = float(benchmark_value)
        if item.session.year not in strategy_by_year:
            strategy_by_year[item.session.year] = (strategy_value, strategy_value)
            benchmark_by_year[item.session.year] = (benchmark_float, benchmark_float)
        else:
            strategy_by_year[item.session.year] = (
                strategy_by_year[item.session.year][0],
                strategy_value,
            )
            benchmark_by_year[item.session.year] = (
                benchmark_by_year[item.session.year][0],
                benchmark_float,
            )
    calendar_year_pass = True
    previous_strategy_end: float | None = None
    previous_benchmark_end: float | None = None
    for year, (strategy_first, strategy_end) in strategy_by_year.items():
        benchmark_first, benchmark_end = benchmark_by_year[year]
        strategy_start = previous_strategy_end or strategy_first
        benchmark_start = previous_benchmark_end or benchmark_first
        if strategy_end / strategy_start - 1 < benchmark_end / benchmark_start - 1 - 0.10:
            calendar_year_pass = False
        previous_strategy_end = strategy_end
        previous_benchmark_end = benchmark_end
    return SteadyReturnsAssessment(
        ulcer_pass=strategy_metrics.ulcer_index <= benchmark_ulcer_index,
        max_drawdown_pass=abs(strategy_metrics.max_drawdown) <= abs(benchmark_max_drawdown),
        rolling_12m_win_rate_pass=strategy_metrics.rolling_12m_win_rate >= 0.55,
        calendar_year_pass=calendar_year_pass,
    )


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
    rolling_win_rate = sum(comparisons) / len(comparisons) if comparisons else 0.0

    strategy_cagr = _cagr(strategy, sessions)
    benchmark_cagr = _cagr(benchmark, sessions)
    mean_benchmark = sum(benchmark_returns) / len(benchmark_returns)
    variance_benchmark = sum((value - mean_benchmark) ** 2 for value in benchmark_returns)
    if variance_benchmark == 0:
        regression_alpha = 0.0
    else:
        mean_strategy = sum(returns) / len(returns)
        beta = (
            sum(
                (strategy_return - mean_strategy) * (benchmark_return - mean_benchmark)
                for strategy_return, benchmark_return in zip(
                    returns, benchmark_returns, strict=True
                )
            )
            / variance_benchmark
        )
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
