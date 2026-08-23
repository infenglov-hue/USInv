"""Explicit regime-conditional factor weights and slow-gate directives."""

from __future__ import annotations

import math
from dataclasses import dataclass

from usinv.config.loader import FactorWeights
from usinv.regime.signals import SlowRegimeSignals


class RegimeWeightError(ValueError):
    """Raised when a defensive policy is implicit or internally inconsistent."""


@dataclass(frozen=True, slots=True)
class RegimeWeightDecision:
    weights: FactorWeights
    defensive: bool
    reason: str


@dataclass(frozen=True, slots=True)
class SlowGateDirectives:
    shade_gross_exposure: bool
    tighten_quality_gate: bool
    magnitude_registered: bool = False


def _validate(weights: FactorWeights) -> None:
    values = (weights.quality, weights.value, weights.momentum)
    if any(not math.isfinite(item) or item < 0 for item in values):
        raise RegimeWeightError("factor weights must be finite and non-negative")
    if not math.isclose(sum(values), 1.0, abs_tol=1e-9):
        raise RegimeWeightError("factor weights must sum to one")


def regime_conditional_weights(
    base: FactorWeights,
    defensive: FactorWeights,
    *,
    below_trend: bool,
    high_volatility: bool,
) -> RegimeWeightDecision:
    """Select explicit defensive weights in high-vol or below-trend states.

    MODEL_SPEC requires momentum de-weighting but does not register a numerical
    defensive vector. Requiring the caller to supply it prevents an undocumented
    default from entering experiments.
    """
    _validate(base)
    _validate(defensive)
    if defensive.momentum >= base.momentum:
        raise RegimeWeightError("defensive weights must de-weight momentum")
    active = below_trend or high_volatility
    return RegimeWeightDecision(
        weights=defensive if active else base,
        defensive=active,
        reason="below_trend_or_high_vol" if active else "normal_regime",
    )


def slow_gate_directives(signals: SlowRegimeSignals) -> SlowGateDirectives:
    """Expose slow-gate intent without inventing an unregistered magnitude."""
    return SlowGateDirectives(
        shade_gross_exposure=signals.shade_gross_exposure,
        tighten_quality_gate=signals.tighten_quality_gate,
    )
