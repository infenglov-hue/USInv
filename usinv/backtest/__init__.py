"""Execution-faithful historical simulation."""

from usinv.backtest.costs import CostModelError, FixedBpsCostModel, money
from usinv.backtest.engine import (
    BacktestEngine,
    BacktestError,
    BacktestResult,
    DividendEvent,
    ManualExecutionRequired,
    MarketBar,
    SplitEvent,
    TerminationEvent,
    TerminationEvidenceError,
    TerminationKind,
)
from usinv.backtest.metrics import BacktestMetrics, MetricsError, compute_metrics

__all__ = [
    "BacktestEngine",
    "BacktestError",
    "BacktestMetrics",
    "BacktestResult",
    "CostModelError",
    "DividendEvent",
    "FixedBpsCostModel",
    "ManualExecutionRequired",
    "MarketBar",
    "MetricsError",
    "SplitEvent",
    "TerminationEvent",
    "TerminationEvidenceError",
    "TerminationKind",
    "compute_metrics",
    "money",
]
