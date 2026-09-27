"""4-Tier Macro Cash Overlay State Machine for US equities.

Blends US market regime (S&P 500 trend + VIX volatility) and macro credit regime
(High-Yield OAS credit spread + Treasury conditions) into a sticky 4-tier cash
overlay matching the MobileInv / BIST Picker model.

States
------
NORMAL    -> 0%  cash (100% equity exposure) — full bull risk
CAUTION   -> 25% cash (75%  equity exposure) — elevated volatility / spread widening
DEFENSIVE -> 50% cash (50%  equity exposure) — trend breakdown or high macro stress
RISK_OFF  -> 75% cash (25%  equity exposure) — combined bear market + credit crunch
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final


class CashState(StrEnum):
    NORMAL = "NORMAL"
    CAUTION = "CAUTION"
    DEFENSIVE = "DEFENSIVE"
    RISK_OFF = "RISK_OFF"


STATE_ORDER: Final[tuple[CashState, ...]] = (
    CashState.NORMAL,
    CashState.CAUTION,
    CashState.DEFENSIVE,
    CashState.RISK_OFF,
)

DEFAULT_CASH_PCT: Final[dict[CashState, float]] = {
    CashState.NORMAL: 0.0,
    CashState.CAUTION: 0.25,
    CashState.DEFENSIVE: 0.50,
    CashState.RISK_OFF: 0.75,
}

DEFAULT_EQUITY_PCT: Final[dict[CashState, float]] = {
    CashState.NORMAL: 1.0,
    CashState.CAUTION: 0.75,
    CashState.DEFENSIVE: 0.50,
    CashState.RISK_OFF: 0.25,
}


@dataclass(frozen=True, slots=True)
class MacroCashConfig:
    """Configurable thresholds for market and credit signals."""

    vix_high_threshold: float = 20.0
    hy_oas_elevated_bps: float = 375.0
    hy_oas_stress_bps: float = 500.0
    max_step_per_transition: int = 1  # Sticky: step at most 1 rung at a time


@dataclass(frozen=True, slots=True)
class MacroCashDecision:
    """The computed cash state, target percentages, and diagnostic explanation."""

    as_of: datetime
    state: CashState
    cash_pct: float
    equity_pct: float
    raw_stress_score: int  # 0..4
    market_regime: str  # BULL_LOW_VOL, BULL_HIGH_VOL, BEAR
    macro_regime: str  # RISK_ON, NEUTRAL, RISK_OFF
    summary_tr: str  # Turkish summary for PWA and Telegram
    summary_en: str  # English summary
    details: dict[str, float | str | bool]


def stress_level_for_state(state: CashState) -> int:
    return STATE_ORDER.index(state)


def state_for_stress_level(level: int) -> CashState:
    clamped = max(0, min(level, len(STATE_ORDER) - 1))
    return STATE_ORDER[clamped]


def calculate_raw_market_score(
    spy_price: float,
    spy_200_sma: float,
    vix: float | None = None,
    *,
    vix_threshold: float = 20.0,
) -> tuple[int, str]:
    """Score market trend and volatility on a 0..2 scale."""
    if spy_price < spy_200_sma:
        return 2, "BEAR"

    is_high_vol = vix is not None and vix >= vix_threshold
    if is_high_vol:
        return 1, "BULL_HIGH_VOL"
    return 0, "BULL_LOW_VOL"


def calculate_raw_macro_score(
    hy_oas_bps: float | None = None,
    *,
    elevated_bps: float = 375.0,
    stress_bps: float = 500.0,
) -> tuple[int, str]:
    """Score macro credit spread stress on a 0..2 scale."""
    if hy_oas_bps is None:
        return 1, "NEUTRAL"
    if hy_oas_bps >= stress_bps:
        return 2, "RISK_OFF"
    if hy_oas_bps >= elevated_bps:
        return 1, "NEUTRAL"
    return 0, "RISK_ON"


def raw_signal_to_target_state(raw_score: int) -> CashState:
    """Map combined 0..4 score to desired target cash state before hysteresis."""
    if raw_score <= 1:
        return CashState.NORMAL
    if raw_score == 2:
        return CashState.CAUTION
    if raw_score == 3:
        return CashState.DEFENSIVE
    return CashState.RISK_OFF


def apply_hysteresis_step(
    current_state: CashState | None,
    target_state: CashState,
    max_step: int = 1,
) -> CashState:
    """Clamp state transition so the state machine moves at most ``max_step`` rungs."""
    if current_state is None:
        return target_state

    current_idx = stress_level_for_state(current_state)
    target_idx = stress_level_for_state(target_state)

    if current_idx == target_idx:
        return current_state

    diff = target_idx - current_idx
    step = max(-max_step, min(max_step, diff))
    return state_for_stress_level(current_idx + step)


def evaluate_macro_cash_overlay(
    *,
    as_of: datetime,
    spy_price: float,
    spy_200_sma: float,
    vix: float | None = None,
    hy_oas_bps: float | None = None,
    previous_state: CashState | None = None,
    config: MacroCashConfig | None = None,
) -> MacroCashDecision:
    """Evaluate current market and macro conditions to produce the sticky cash allocation."""
    cfg = config or MacroCashConfig()

    mkt_score, mkt_regime = calculate_raw_market_score(
        spy_price,
        spy_200_sma,
        vix=vix,
        vix_threshold=cfg.vix_high_threshold,
    )

    macro_score, macro_regime = calculate_raw_macro_score(
        hy_oas_bps,
        elevated_bps=cfg.hy_oas_elevated_bps,
        stress_bps=cfg.hy_oas_stress_bps,
    )

    raw_score = mkt_score + macro_score
    target_state = raw_signal_to_target_state(raw_score)
    final_state = apply_hysteresis_step(
        previous_state,
        target_state,
        max_step=cfg.max_step_per_transition,
    )

    cash_pct = DEFAULT_CASH_PCT[final_state]
    equity_pct = DEFAULT_EQUITY_PCT[final_state]

    # Generate friendly Turkish and English summary strings
    if final_state == CashState.NORMAL:
        summary_tr = "Piyasa Normal. SPY 200 SMA üzerinde, kredi spreadleri sakin. %100 hissede."
        summary_en = "Market Normal. SPY above 200 SMA, credit spreads calm. 100% equity exposure."
    elif final_state == CashState.CAUTION:
        summary_tr = (
            "Piyasa Dikkat rejiminde. Artan volatilite/yayılma nedeniyle %25 nakit koruması aktif."
        )
        summary_en = "Market in Caution regime. 25% cash buffer active due to elevated stress."
    elif final_state == CashState.DEFENSIVE:
        summary_tr = (
            "Piyasa Savunma rejiminde. Trend kaybı veya makro baskı nedeniyle %50 nakit tutuluyor."
        )
        summary_en = "Defensive regime. 50% cash allocation due to trend breakdown or macro stress."
    else:
        summary_tr = (
            "Piyasa Risk-Off rejiminde! Ayı piyasası ve kredi baskısı nedeniyle %75 nakit aktif."
        )
        summary_en = (
            "Risk-Off regime! 75% cash allocation to protect capital against severe stress."
        )

    details: dict[str, float | str | bool] = {
        "spy_price": spy_price,
        "spy_200_sma": spy_200_sma,
        "spy_above_sma": bool(spy_price >= spy_200_sma),
        "vix": vix if vix is not None else -1.0,
        "hy_oas_bps": hy_oas_bps if hy_oas_bps is not None else -1.0,
        "raw_stress_score": raw_score,
        "target_state": target_state.value,
    }

    return MacroCashDecision(
        as_of=as_of,
        state=final_state,
        cash_pct=cash_pct,
        equity_pct=equity_pct,
        raw_stress_score=raw_score,
        market_regime=mkt_regime,
        macro_regime=macro_regime,
        summary_tr=summary_tr,
        summary_en=summary_en,
        details=details,
    )
