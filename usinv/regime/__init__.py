"""Market-regime signals and exposure overlays."""

from usinv.regime.overlay import OverlayDecision, OverlayError, evaluate_overlay
from usinv.regime.signals import (
    CreditStressSignal,
    MarketRegime,
    SlowRegimeSignals,
    TrendBand,
    TrendSignal,
    classify_market_regime,
    high_volatility_signal,
    hy_oas_credit_stress,
    month_end_trend,
    moving_average_trend,
    slow_regime_signals,
)
from usinv.regime.weights import (
    RegimeWeightDecision,
    RegimeWeightError,
    SlowGateDirectives,
    regime_conditional_weights,
    slow_gate_directives,
)

__all__ = [
    "CreditStressSignal",
    "MarketRegime",
    "OverlayDecision",
    "OverlayError",
    "RegimeWeightDecision",
    "RegimeWeightError",
    "SlowGateDirectives",
    "SlowRegimeSignals",
    "TrendBand",
    "TrendSignal",
    "classify_market_regime",
    "evaluate_overlay",
    "high_volatility_signal",
    "hy_oas_credit_stress",
    "month_end_trend",
    "moving_average_trend",
    "regime_conditional_weights",
    "slow_gate_directives",
    "slow_regime_signals",
]
