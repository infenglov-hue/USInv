"""Point-in-time trend, credit, volatility and slow macro regime signals."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from statistics import fmean

from usinv.data.macro.fred import HY_OAS_SERIES, NFCI_SERIES, SAHM_REALTIME_SERIES
from usinv.data.macro.vintage import (
    MacroDataError,
    VintagedObservation,
    latest_observation_as_of,
    observations_as_of,
)


class TrendBand(StrEnum):
    BELOW = "below"
    HYSTERESIS = "hysteresis"
    ABOVE = "above"


class MarketRegime(StrEnum):
    RISK_OFF = "risk_off"
    BEAR = "bear"
    CHOP = "chop"
    RISK_ON = "risk_on"


@dataclass(frozen=True, slots=True)
class TrendSignal:
    series_id: str
    band: TrendBand
    level: float
    moving_average: float
    lower_bound: float
    upper_bound: float
    window_observations: int
    available_from: datetime
    evidence_pointer: str


@dataclass(frozen=True, slots=True)
class CreditStressSignal:
    source: str
    stressed: bool
    spread_bps: float
    moving_average_bps: float
    absolute_threshold_bps: float
    window_observations: int
    available_from: datetime
    evidence_pointer: str


@dataclass(frozen=True, slots=True)
class SlowRegimeSignals:
    nfci_value: float | None
    tighter_financial_conditions: bool | None
    sahm_value: float | None
    sahm_triggered: bool | None
    shade_gross_exposure: bool
    tighten_quality_gate: bool
    evidence_pointers: tuple[str, ...]


def moving_average_trend(
    observations: tuple[VintagedObservation, ...],
    *,
    series_id: str,
    as_of: datetime,
    window_observations: int,
    hysteresis_fraction: float,
) -> TrendSignal:
    """Classify the latest visible level around its trailing moving average."""
    if window_observations < 2:
        raise MacroDataError("trend window must contain at least two observations")
    if not 0 <= hysteresis_fraction < 1:
        raise MacroDataError("trend hysteresis must be in [0, 1)")
    history = observations_as_of(observations, series_id=series_id, as_of=as_of)
    if len(history) < window_observations:
        raise MacroDataError(f"insufficient {series_id} history for trend signal")
    window = history[-window_observations:]
    average = fmean(item.value for item in window)
    if not math.isfinite(average) or average <= 0:
        raise MacroDataError("trend moving average must be finite and positive")
    latest = window[-1]
    lower = average * (1 - hysteresis_fraction)
    upper = average * (1 + hysteresis_fraction)
    if latest.value < lower:
        band = TrendBand.BELOW
    elif latest.value > upper:
        band = TrendBand.ABOVE
    else:
        band = TrendBand.HYSTERESIS
    return TrendSignal(
        series_id=series_id,
        band=band,
        level=latest.value,
        moving_average=average,
        lower_bound=lower,
        upper_bound=upper,
        window_observations=len(window),
        available_from=latest.available_from,
        evidence_pointer=latest.evidence_pointer,
    )


def month_end_trend(
    observations: tuple[VintagedObservation, ...],
    *,
    series_id: str,
    as_of: datetime,
    window_months: int,
) -> TrendSignal:
    """Calculate a trailing SMA from the last visible observation of each month."""
    if window_months < 2:
        raise MacroDataError("monthly trend window must contain at least two months")
    history = observations_as_of(observations, series_id=series_id, as_of=as_of)
    month_ends: dict[tuple[int, int], VintagedObservation] = {}
    for item in history:
        month_ends[(item.observation_date.year, item.observation_date.month)] = item
    monthly = tuple(month_ends[key] for key in sorted(month_ends))
    if len(monthly) < window_months:
        raise MacroDataError(f"insufficient {series_id} month-end history")
    window = monthly[-window_months:]
    average = fmean(item.value for item in window)
    if average <= 0:
        raise MacroDataError("monthly moving average must be positive")
    latest = window[-1]
    band = TrendBand.BELOW if latest.value < average else TrendBand.ABOVE
    return TrendSignal(
        series_id=series_id,
        band=band,
        level=latest.value,
        moving_average=average,
        lower_bound=average,
        upper_bound=average,
        window_observations=len(window),
        available_from=latest.available_from,
        evidence_pointer=latest.evidence_pointer,
    )


def hy_oas_credit_stress(
    observations: tuple[VintagedObservation, ...],
    *,
    as_of: datetime,
    absolute_threshold_bps: float,
    moving_average_sessions: int,
) -> CreditStressSignal:
    """Apply O3's exact dual HY-OAS confirmation rule in canonical basis points."""
    if absolute_threshold_bps <= 0 or moving_average_sessions < 2:
        raise MacroDataError("HY-OAS thresholds must be positive")
    history = observations_as_of(observations, series_id=HY_OAS_SERIES, as_of=as_of)
    if len(history) < moving_average_sessions:
        raise MacroDataError("insufficient HY-OAS history for credit confirmation")
    window = history[-moving_average_sessions:]
    average = fmean(item.value for item in window)
    latest = window[-1]
    return CreditStressSignal(
        source="fred_hy_oas",
        stressed=latest.value > absolute_threshold_bps and latest.value > average,
        spread_bps=latest.value,
        moving_average_bps=average,
        absolute_threshold_bps=absolute_threshold_bps,
        window_observations=len(window),
        available_from=latest.available_from,
        evidence_pointer=latest.evidence_pointer,
    )


def slow_regime_signals(
    nfci: tuple[VintagedObservation, ...],
    sahm_realtime: tuple[VintagedObservation, ...],
    *,
    as_of: datetime,
) -> SlowRegimeSignals:
    """Resolve slow gates from ALFRED NFCI and the real-time Sahm series."""
    nfci_latest = latest_observation_as_of(nfci, series_id=NFCI_SERIES, as_of=as_of)
    sahm_latest = latest_observation_as_of(
        sahm_realtime,
        series_id=SAHM_REALTIME_SERIES,
        as_of=as_of,
    )
    tighter = None if nfci_latest is None else nfci_latest.value > 0
    sahm_triggered = None if sahm_latest is None else sahm_latest.value >= 0.5
    active = tighter is True or sahm_triggered is True
    pointers = tuple(
        item.evidence_pointer for item in (nfci_latest, sahm_latest) if item is not None
    )
    return SlowRegimeSignals(
        nfci_value=None if nfci_latest is None else nfci_latest.value,
        tighter_financial_conditions=tighter,
        sahm_value=None if sahm_latest is None else sahm_latest.value,
        sahm_triggered=sahm_triggered,
        shade_gross_exposure=active,
        tighten_quality_gate=active,
        evidence_pointers=pointers,
    )


def high_volatility_signal(
    vix: tuple[VintagedObservation, ...],
    *,
    as_of: datetime,
    threshold: float,
) -> bool | None:
    """Return a high-vol flag using an explicit, caller-registered threshold."""
    if not math.isfinite(threshold) or threshold <= 0:
        raise MacroDataError("volatility threshold must be finite and positive")
    latest = latest_observation_as_of(vix, series_id="VIXCLS", as_of=as_of)
    return None if latest is None else latest.value > threshold


def classify_market_regime(
    trend: TrendSignal,
    credit: CreditStressSignal | None,
) -> MarketRegime:
    """Name the diagnostic state without changing portfolio exposure itself."""
    if trend.band is TrendBand.BELOW:
        if credit is not None and credit.stressed:
            return MarketRegime.RISK_OFF
        return MarketRegime.BEAR
    if trend.band is TrendBand.HYSTERESIS:
        return MarketRegime.CHOP
    return MarketRegime.RISK_ON
