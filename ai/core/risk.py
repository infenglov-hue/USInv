"""Risk evaluation, leverage decay estimation, and trade plan construction for high-risk ETFs."""

from ai.core.metrics import calculate_atr
from ai.core.models import BarData, QuantMetrics, RiskProfile, TradePlan


def evaluate_risk_profile(
    metrics: QuantMetrics,
    leverage: float,
    max_portfolio_allocation_pct: float = 15.0,
) -> RiskProfile:
    """Assess volatility grade, leverage decay risks, and suggested risk limits."""
    abs_lev = abs(leverage)

    # Volatility classification
    if metrics.annualized_volatility >= 0.60 or abs_lev >= 3.0:
        vol_grade = "EXTREME"
        decay_risk = "HIGH" if abs_lev > 1.0 else "MODERATE"
        stop_pct = 9.0
    elif metrics.annualized_volatility >= 0.35 or abs_lev >= 2.0:
        vol_grade = "VERY_HIGH"
        decay_risk = "MODERATE" if abs_lev > 1.0 else "LOW"
        stop_pct = 7.0
    else:
        vol_grade = "HIGH"
        decay_risk = "LOW"
        stop_pct = 5.0

    return RiskProfile(
        volatility_grade=vol_grade,
        leverage_decay_risk=decay_risk,
        max_historical_drawdown=metrics.max_drawdown_60d,
        suggested_stop_loss_pct=stop_pct,
        beta_exposure=metrics.beta_qqq if metrics.beta_qqq != 1.0 else metrics.beta_spy,
    )


def construct_trade_plan(
    symbol: str,
    bars: list[BarData],
    metrics: QuantMetrics,
    leverage: float,
    action: str = "STRONG_BUY",
    atr_multiplier: float = 2.0,
    min_risk_reward: float = 2.0,
    max_allocation: float = 15.0,
) -> TradePlan:
    """Build concrete tactical trade plan with entry zone, targets, stop-loss, and sizing."""
    price = metrics.current_price
    atr = calculate_atr(bars, period=14)

    # Dynamic stop based on ATR with percentage floor/ceiling
    atr_stop_dist = atr * atr_multiplier
    pct_stop_dist = price * (0.08 if abs(leverage) >= 3.0 else 0.06)
    stop_distance = max(atr_stop_dist, pct_stop_dist)

    # Cap stop loss at max 12% to prevent catastrophic losses in leveraged ETFs
    max_stop_dist = price * 0.12
    min_stop_dist = price * 0.04
    stop_distance = min(max(stop_distance, min_stop_dist), max_stop_dist)

    stop_loss = round(price - stop_distance, 2)
    risk_amount = price - stop_loss

    # Reward targets based on R/R multiples
    target_1 = round(price + (risk_amount * 1.5), 2)
    target_2 = round(price + (risk_amount * 2.8), 2)

    # Entry zone: current price ± 0.8%
    entry_low = round(price * 0.992, 2)
    entry_high = round(price * 1.008, 2)

    actual_rr = round((target_2 - price) / max(risk_amount, 0.01), 2)

    # Holding horizon: Leveraged instruments have decay; shorter horizon
    if abs(leverage) >= 3.0:
        holding_days = 15
        capped_allocation = min(max_allocation, 10.0)
    elif abs(leverage) >= 2.0:
        holding_days = 25
        capped_allocation = min(max_allocation, 12.5)
    else:
        holding_days = 45
        capped_allocation = max_allocation

    return TradePlan(
        entry_zone_low=entry_low,
        entry_zone_high=entry_high,
        target_1=target_1,
        target_2=target_2,
        stop_loss=stop_loss,
        risk_reward_ratio=actual_rr,
        max_portfolio_allocation_pct=capped_allocation,
        expected_holding_days=holding_days,
        trailing_stop_atr=round(atr, 2),
    )
