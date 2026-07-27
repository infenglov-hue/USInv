"""Exact pre-registered O0-O3 exposure overlays (EXPERIMENT_PLAN section 3)."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from usinv.config.loader import OverlayConfig
from usinv.data.macro.proxies import CreditProxySignal
from usinv.data.macro.vintage import MacroDataError, VintagedObservation
from usinv.regime.signals import (
    CreditStressSignal,
    TrendBand,
    TrendSignal,
    hy_oas_credit_stress,
    month_end_trend,
    moving_average_trend,
)


class OverlayError(ValueError):
    """Raised when an active overlay lacks required point-in-time evidence."""


@dataclass(frozen=True, slots=True)
class OverlayDecision:
    overlay_id: str
    as_of: datetime
    evaluated: bool
    risk_off: bool
    gross_exposure: float
    trend: TrendSignal | None
    credit_source: str | None
    reason: str


def _trend_risk_off(trend: TrendSignal, previous_risk_off: bool) -> bool:
    if trend.band is TrendBand.BELOW:
        return True
    if trend.band is TrendBand.ABOVE:
        return False
    return previous_risk_off


def evaluate_overlay(
    config: OverlayConfig,
    *,
    as_of: datetime,
    spy_total_return: tuple[VintagedObservation, ...] = (),
    hy_oas_bps: tuple[VintagedObservation, ...] = (),
    fallback_credit: CreditProxySignal | None = None,
    previous_risk_off: bool = False,
    is_month_end_evaluation: bool = False,
) -> OverlayDecision:
    """Evaluate one overlay using only observations visible at ``as_of``."""
    if as_of.tzinfo is None:
        raise OverlayError("overlay as_of must be timezone-aware")
    if config.overlay_id == "O0":
        return OverlayDecision(
            overlay_id="O0",
            as_of=as_of,
            evaluated=True,
            risk_off=False,
            gross_exposure=1.0,
            trend=None,
            credit_source=None,
            reason="overlay_disabled",
        )

    try:
        if config.overlay_id == "O2":
            if not is_month_end_evaluation:
                return OverlayDecision(
                    overlay_id="O2",
                    as_of=as_of,
                    evaluated=False,
                    risk_off=previous_risk_off,
                    gross_exposure=config.risk_off_exposure if previous_risk_off else 1.0,
                    trend=None,
                    credit_source=None,
                    reason="not_month_end_evaluation",
                )
            trend = month_end_trend(
                spy_total_return,
                series_id="SPY_TR",
                as_of=as_of,
                window_months=10,
            )
            risk_off = trend.band is TrendBand.BELOW
        elif config.overlay_id in {"O1", "O3"}:
            trend = moving_average_trend(
                spy_total_return,
                series_id="SPY_TR",
                as_of=as_of,
                window_observations=200,
                hysteresis_fraction=config.hysteresis_fraction,
            )
            risk_off = _trend_risk_off(trend, previous_risk_off)
        else:
            raise OverlayError(f"unsupported overlay id {config.overlay_id!r}")
    except MacroDataError as exc:
        raise OverlayError(str(exc)) from exc

    credit_source: str | None = None
    if config.overlay_id == "O3":
        credit: CreditStressSignal | CreditProxySignal
        try:
            credit = hy_oas_credit_stress(
                hy_oas_bps,
                as_of=as_of,
                absolute_threshold_bps=config.hy_oas_threshold_bps,
                moving_average_sessions=config.hy_oas_ma_sessions,
            )
        except MacroDataError:
            if fallback_credit is None or fallback_credit.available_from > as_of:
                raise OverlayError(
                    "O3 requires visible HY-OAS or HYG/LQD credit evidence"
                ) from None
            credit = fallback_credit
        credit_source = credit.source
        risk_off = risk_off and credit.stressed

    return OverlayDecision(
        overlay_id=config.overlay_id,
        as_of=as_of,
        evaluated=True,
        risk_off=risk_off,
        gross_exposure=config.risk_off_exposure if risk_off else 1.0,
        trend=trend,
        credit_source=credit_source,
        reason="risk_off_confirmed" if risk_off else "risk_on_or_unconfirmed",
    )
