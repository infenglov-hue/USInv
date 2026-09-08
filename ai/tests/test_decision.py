"""Tests for decision engine and trade plan construction."""

from datetime import datetime, timezone
from ai.agent.decision_engine import DecisionEngine
from ai.core.models import (
    BarData,
    ETFProfile,
    MacroRegime,
    MacroRegimeType,
    NewsItem,
    QuantMetrics,
    RecommendationAction,
    SentimentType,
)


def test_decision_engine_strong_buy():
    engine = DecisionEngine()

    profile = ETFProfile(
        symbol="SOXL",
        name="Direxion Daily Semiconductor Bull 3X",
        category="leveraged_tech_semi",
        leverage=3.0,
        theme="Semiconductors",
    )

    metrics = QuantMetrics(
        symbol="SOXL",
        current_price=45.0,
        return_5d=0.08,
        return_20d=0.25,
        return_60d=0.45,
        annualized_volatility=0.65,
        sortino_ratio=2.4,
        rsi_14=62.0,
        trend_alignment="STRONG_BULLISH",
    )

    macro = MacroRegime(
        regime_type=MacroRegimeType.RISK_ON_EXPANSION,
        confidence_score=85.0,
        summary="Risk-on expansion",
        spy_trend="STRONG_BULLISH",
        qqq_trend="STRONG_BULLISH",
        rates_tlt_trend="BULLISH",
        volatility_uvxy_trend="BEARISH",
        dollar_uup_trend="NEUTRAL",
        credit_hyg_trend="BULLISH",
    )

    news = [
        NewsItem(
            id="1",
            headline="Semiconductor demand skyrockets with new AI datacenter orders",
            published_at=datetime.now(timezone.utc),
            sentiment=SentimentType.BULLISH,
        )
    ]

    bars = [
        BarData(
            timestamp=datetime.now(timezone.utc),
            open=43.0,
            high=46.0,
            low=42.0,
            close=45.0,
            volume=5_000_000,
        )
        for _ in range(30)
    ]

    rec = engine.evaluate_candidate(
        profile=profile,
        metrics=metrics,
        bars=bars,
        macro=macro,
        news_items=news,
        bull_catalysts=["Datacenter AI orders surging"],
        bear_risks=["Potential tariff announcements"],
    )

    assert rec.action == RecommendationAction.STRONG_BUY
    assert rec.conviction_score >= 75.0
    assert rec.trade_plan.target_2 > rec.trade_plan.target_1 > metrics.current_price
    assert rec.trade_plan.stop_loss < metrics.current_price
    assert rec.trade_plan.risk_reward_ratio >= 1.5
