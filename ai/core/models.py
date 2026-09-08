"""Pydantic data models for the AI Alpha ETF system."""

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class MacroRegimeType(str, Enum):
    RISK_ON_EXPANSION = "RISK_ON_EXPANSION"
    RISK_OFF_CONTRACTION = "RISK_OFF_CONTRACTION"
    CHOPPY_ROTATION = "CHOPPY_ROTATION"
    VOLATILITY_EXPANSION = "VOLATILITY_EXPANSION"
    DEFENSIVE = "DEFENSIVE"


class RecommendationAction(str, Enum):
    STRONG_BUY = "STRONG_BUY"
    SPECULATIVE_BUY = "SPECULATIVE_BUY"
    HOLD_NEUTRAL = "HOLD_NEUTRAL"
    AVOID_CASH = "AVOID_CASH"
    HEDGE_SHORT = "HEDGE_SHORT"


class SentimentType(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class BarData(BaseModel):
    timestamp: datetime
    open: float
    high: float
    low: float
    close: float
    volume: float
    vwap: float | None = None


class ETFProfile(BaseModel):
    symbol: str
    name: str
    category: str
    leverage: float = 1.0
    underlying: str = ""
    theme: str = ""
    risk_level: str = "HIGH"


class QuantMetrics(BaseModel):
    symbol: str
    current_price: float
    return_5d: float = 0.0
    return_20d: float = 0.0
    return_60d: float = 0.0
    return_120d: float = 0.0
    annualized_volatility: float = 0.0
    downside_deviation: float = 0.0
    sortino_ratio: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown_60d: float = 0.0
    rsi_14: float = 50.0
    ema_20: float = 0.0
    sma_50: float = 0.0
    sma_200: float | None = None
    trend_alignment: str = "NEUTRAL"  # BULLISH, BEARISH, NEUTRAL
    beta_spy: float = 1.0
    beta_qqq: float = 1.0
    avg_volume_20d: float = 0.0
    avg_dollar_volume_20d: float = 0.0
    is_liquid: bool = True
    is_high_risk: bool = True


class MacroRegime(BaseModel):
    regime_type: MacroRegimeType
    confidence_score: float = Field(default=0.0, ge=0.0, le=100.0)
    summary: str
    spy_trend: str
    qqq_trend: str
    rates_tlt_trend: str
    volatility_uvxy_trend: str
    dollar_uup_trend: str
    credit_hyg_trend: str
    favored_etf_types: list[str] = Field(default_factory=list)
    unfavored_etf_types: list[str] = Field(default_factory=list)
    measured_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class NewsItem(BaseModel):
    id: str
    headline: str
    summary: str = ""
    source: str = ""
    published_at: datetime
    symbols: list[str] = Field(default_factory=list)
    sentiment: SentimentType = SentimentType.NEUTRAL
    relevance_score: float = 1.0


class TradePlan(BaseModel):
    entry_zone_low: float
    entry_zone_high: float
    target_1: float
    target_2: float
    stop_loss: float
    risk_reward_ratio: float
    max_portfolio_allocation_pct: float
    expected_holding_days: int
    trailing_stop_atr: float


class RiskProfile(BaseModel):
    volatility_grade: str  # EXTREME, VERY_HIGH, HIGH
    leverage_decay_risk: str  # HIGH, MODERATE, LOW
    max_historical_drawdown: float
    suggested_stop_loss_pct: float
    beta_exposure: float


class InvestmentRecommendation(BaseModel):
    symbol: str
    name: str
    category: str
    leverage: float
    action: RecommendationAction
    conviction_score: float = Field(..., ge=0.0, le=100.0)
    thesis_summary: str
    bull_catalysts: list[str] = Field(default_factory=list)
    bear_risks: list[str] = Field(default_factory=list)
    macro_context: str
    metrics: QuantMetrics
    trade_plan: TradePlan
    risk_profile: RiskProfile
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    raw_evidence: dict[str, Any] = Field(default_factory=dict)
