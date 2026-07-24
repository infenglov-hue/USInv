"""Momentum sleeve — 12-1 total return (MODEL_SPEC §1).

Computed on the adjusted TOTAL-RETURN series (dividends reinvested), skipping the
most recent month to avoid the short-term reversal. The skip window is why
momentum binds rebalance frequency; both windows come from ``factors.yaml``.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

MOMENTUM_METRICS = ("momentum_12_1",)


class MomentumError(ValueError):
    """Raised when a momentum window is inconsistent."""


@dataclass(frozen=True, slots=True)
class MomentumInputs:
    """Total-return index levels bounding the 12-1 window."""

    tr_index_lookback: Decimal | None = None
    tr_index_skip: Decimal | None = None


def momentum_12_1(inputs: MomentumInputs) -> float | None:
    """Return the total return from the lookback point to the skip point.

    ``None`` when either endpoint is missing or the lookback level is not
    positive — a non-positive index level cannot produce a meaningful return.
    """
    start = inputs.tr_index_lookback
    end = inputs.tr_index_skip
    if start is None or end is None or start <= 0 or end < 0:
        return None
    return float(end / start) - 1.0


def momentum_metrics(inputs: MomentumInputs) -> dict[str, float | None]:
    """Return the momentum sleeve's raw metrics; unavailable ones stay ``None``."""
    return {"momentum_12_1": momentum_12_1(inputs)}
