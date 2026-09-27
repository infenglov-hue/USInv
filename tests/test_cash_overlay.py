"""Unit tests for the 4-tier macro cash overlay state machine."""

from __future__ import annotations

from datetime import UTC, datetime

from usinv.regime import (
    CashState,
    MacroCashConfig,
    evaluate_macro_cash_overlay,
)


def test_bull_market_normal_cash():
    as_of = datetime(2026, 7, 17, 16, 0, tzinfo=UTC)
    dec = evaluate_macro_cash_overlay(
        as_of=as_of,
        spy_price=550.0,
        spy_200_sma=500.0,
        vix=14.5,
        hy_oas_bps=320.0,
    )
    assert dec.state == CashState.NORMAL
    assert dec.cash_pct == 0.0
    assert dec.equity_pct == 1.0
    assert dec.market_regime == "BULL_LOW_VOL"
    assert dec.macro_regime == "RISK_ON"
    assert "%100 hissede" in dec.summary_tr


def test_high_volatility_triggers_caution():
    as_of = datetime(2026, 7, 17, 16, 0, tzinfo=UTC)
    dec = evaluate_macro_cash_overlay(
        as_of=as_of,
        spy_price=550.0,
        spy_200_sma=500.0,
        vix=26.0,  # High VIX
        hy_oas_bps=380.0,  # Elevated spread
    )
    # Market score = 1 (BULL_HIGH_VOL), Macro score = 1 (NEUTRAL) -> raw = 2 -> CAUTION
    assert dec.state == CashState.CAUTION
    assert dec.cash_pct == 0.25
    assert dec.equity_pct == 0.75
    assert "%25 nakit" in dec.summary_tr


def test_bear_market_triggers_defensive_or_risk_off():
    as_of = datetime(2026, 7, 17, 16, 0, tzinfo=UTC)
    # SPY below 200 SMA (market score 2) + normal credit (macro score 0) -> raw = 2 -> CAUTION
    # SPY below 200 SMA (2) + credit spread stress (2) -> raw = 4 -> RISK_OFF
    dec_severe = evaluate_macro_cash_overlay(
        as_of=as_of,
        spy_price=450.0,
        spy_200_sma=500.0,
        vix=35.0,
        hy_oas_bps=550.0,
    )
    assert dec_severe.state == CashState.RISK_OFF
    assert dec_severe.cash_pct == 0.75
    assert dec_severe.equity_pct == 0.25
    assert "%75 nakit" in dec_severe.summary_tr


def test_hysteresis_stickiness():
    as_of = datetime(2026, 7, 17, 16, 0, tzinfo=UTC)
    # Severe conditions would target RISK_OFF (step index 3)
    # But previous state was NORMAL (step index 0)
    # With max_step_per_transition = 1, it only steps to CAUTION (step index 1)
    dec = evaluate_macro_cash_overlay(
        as_of=as_of,
        spy_price=450.0,
        spy_200_sma=500.0,
        vix=35.0,
        hy_oas_bps=600.0,
        previous_state=CashState.NORMAL,
        config=MacroCashConfig(max_step_per_transition=1),
    )
    assert dec.state == CashState.CAUTION
    assert dec.cash_pct == 0.25
