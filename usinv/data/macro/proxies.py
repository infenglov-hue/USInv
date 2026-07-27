"""License-light HYG/LQD total-return credit-stress fallback."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from statistics import fmean, pstdev

from usinv.data.macro.vintage import MacroDataError, VintagedObservation, observations_as_of


@dataclass(frozen=True, slots=True)
class CreditProxySignal:
    source: str
    stressed: bool
    zscore: float
    threshold_z: float
    observation_count: int
    available_from: datetime
    evidence_pointers: tuple[str, str]


def hyg_lqd_credit_stress(
    hyg_total_return: tuple[VintagedObservation, ...],
    lqd_total_return: tuple[VintagedObservation, ...],
    *,
    as_of: datetime,
    window_sessions: int,
    threshold_z: float,
) -> CreditProxySignal:
    """Flag stress when the HYG/LQD total-return ratio has a low z-score.

    The fallback threshold is deliberately explicit because DATA_SPEC names the
    proxy but does not register a numerical cutoff.
    """
    if window_sessions < 2:
        raise MacroDataError("credit proxy window must contain at least two sessions")
    if not math.isfinite(threshold_z) or threshold_z >= 0:
        raise MacroDataError("credit proxy stress threshold must be finite and negative")
    hyg = observations_as_of(hyg_total_return, series_id="HYG_TR", as_of=as_of)
    lqd = observations_as_of(lqd_total_return, series_id="LQD_TR", as_of=as_of)
    hyg_by_date = {item.observation_date: item for item in hyg}
    lqd_by_date = {item.observation_date: item for item in lqd}
    common_dates = sorted(set(hyg_by_date) & set(lqd_by_date))
    if len(common_dates) < window_sessions:
        raise MacroDataError("insufficient aligned HYG/LQD total-return history")
    dates = common_dates[-window_sessions:]
    ratios: list[float] = []
    for day in dates:
        denominator = lqd_by_date[day].value
        if denominator <= 0 or hyg_by_date[day].value <= 0:
            raise MacroDataError("total-return index levels must be positive")
        ratios.append(hyg_by_date[day].value / denominator)
    deviation = pstdev(ratios)
    if deviation == 0:
        raise MacroDataError("HYG/LQD ratio has zero variation")
    zscore = (ratios[-1] - fmean(ratios)) / deviation
    last_hyg = hyg_by_date[dates[-1]]
    last_lqd = lqd_by_date[dates[-1]]
    return CreditProxySignal(
        source="hyg_lqd_tr_zscore",
        stressed=zscore <= threshold_z,
        zscore=zscore,
        threshold_z=threshold_z,
        observation_count=len(ratios),
        available_from=max(last_hyg.available_from, last_lqd.available_from),
        evidence_pointers=(last_hyg.evidence_pointer, last_lqd.evidence_pointer),
    )
