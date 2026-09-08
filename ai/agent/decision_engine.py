"""Synthesis and decision engine for evaluating high-risk funds and ETFs."""

from datetime import datetime, timezone
from ai.core.models import (
    BarData,
    ETFProfile,
    InvestmentRecommendation,
    MacroRegime,
    MacroRegimeType,
    NewsItem,
    QuantMetrics,
    RecommendationAction,
    SentimentType,
)
from ai.core.risk import construct_trade_plan, evaluate_risk_profile


class DecisionEngine:
    """Evaluates quantitative metrics, macro regime alignment, and news catalysts to generate recommendations."""

    def evaluate_candidate(
        self,
        profile: ETFProfile,
        metrics: QuantMetrics,
        bars: list[BarData],
        macro: MacroRegime,
        news_items: list[NewsItem],
        bull_catalysts: list[str],
        bear_risks: list[str],
        weights: dict[str, float] | None = None,
    ) -> InvestmentRecommendation:
        """Formulate a comprehensive conviction score, thesis, and trade plan."""
        w = weights or {
            "momentum": 0.35,
            "sortino_ratio": 0.20,
            "macro_alignment": 0.25,
            "catalyst_sentiment": 0.20,
        }

        # 1. Quantitative Momentum Score (0-100)
        # 20D return: +10% is strong, -10% is weak
        ret_score = min(max((metrics.return_20d + 0.10) / 0.25 * 50.0, 0.0), 50.0)
        # Trend bonus
        trend_score = 40.0 if "STRONG_BULLISH" in metrics.trend_alignment else (
            30.0 if "BULLISH" in metrics.trend_alignment else (15.0 if "NEUTRAL" in metrics.trend_alignment else 0.0)
        )
        # RSI sweet spot (45 - 68 is ideal, > 80 is overbought exhaustion, < 30 is falling knife)
        if 45.0 <= metrics.rsi_14 <= 70.0:
            rsi_score = 10.0
        elif 35.0 <= metrics.rsi_14 < 45.0 or 70.0 < metrics.rsi_14 <= 78.0:
            rsi_score = 5.0
        else:
            rsi_score = 0.0

        quant_score = ret_score + trend_score + rsi_score  # 0 to 100

        # 2. Risk-adjusted Quality Score (Sortino / Volatility)
        # Sortino > 2 is exceptional, < 0 is negative return
        sortino_score = min(max((metrics.sortino_ratio + 0.5) / 3.0 * 100.0, 0.0), 100.0)

        # 3. Macro Alignment Score (0-100)
        macro_score = self._calculate_macro_alignment(profile, macro)

        # 4. News & Catalyst Sentiment Score (0-100)
        bull_count = sum(1 for n in news_items if n.sentiment == SentimentType.BULLISH)
        bear_count = sum(1 for n in news_items if n.sentiment == SentimentType.BEARISH)
        total_news = len(news_items)

        if total_news > 0:
            sentiment_ratio = (bull_count - bear_count) / max(total_news, 1)
            sentiment_score = min(max(50.0 + (sentiment_ratio * 40.0), 10.0), 95.0)
        else:
            sentiment_score = 50.0

        # Composite Conviction Score
        conviction = (
            quant_score * w.get("momentum", 0.35)
            + sortino_score * w.get("sortino_ratio", 0.20)
            + macro_score * w.get("macro_alignment", 0.25)
            + sentiment_score * w.get("catalyst_sentiment", 0.20)
        )
        conviction = round(min(max(conviction, 5.0), 98.0), 1)

        # Action Determination
        is_inverse = profile.leverage < 0
        if is_inverse:
            if macro.regime_type == MacroRegimeType.RISK_OFF_CONTRACTION and conviction >= 70.0:
                action = RecommendationAction.HEDGE_SHORT
            else:
                action = RecommendationAction.AVOID_CASH
        else:
            if conviction >= 75.0 and macro_score >= 60.0:
                action = RecommendationAction.STRONG_BUY
            elif conviction >= 58.0 and macro_score >= 45.0:
                action = RecommendationAction.SPECULATIVE_BUY
            elif conviction >= 45.0:
                action = RecommendationAction.HOLD_NEUTRAL
            else:
                action = RecommendationAction.AVOID_CASH

        # Macro Context Explanation
        macro_context = (
            f"Mevcut makro rejim '{macro.regime_type.value}' ({macro.confidence_score:.0f}% güven). "
            f"{'Bu fon rejim dinamikleriyle yüksek korelasyona ve rüzgar arkasına sahip.' if macro_score >= 65 else 'Makro rüzgarlar bu varlık için temkinli olmayı gerektiriyor.'}"
        )

        # Synthesize Investment Thesis
        thesis = self._generate_thesis(profile, metrics, macro, action, conviction)

        # Build Risk Profile & Trade Plan
        risk_profile = evaluate_risk_profile(metrics, profile.leverage)
        trade_plan = construct_trade_plan(
            symbol=profile.symbol,
            bars=bars,
            metrics=metrics,
            leverage=profile.leverage,
            action=action.value,
        )

        return InvestmentRecommendation(
            symbol=profile.symbol,
            name=profile.name,
            category=profile.category,
            leverage=profile.leverage,
            action=action,
            conviction_score=conviction,
            thesis_summary=thesis,
            bull_catalysts=bull_catalysts,
            bear_risks=bear_risks,
            macro_context=macro_context,
            metrics=metrics,
            trade_plan=trade_plan,
            risk_profile=risk_profile,
            generated_at=datetime.now(timezone.utc),
            raw_evidence={
                "quant_score": quant_score,
                "sortino_score": sortino_score,
                "macro_score": macro_score,
                "sentiment_score": sentiment_score,
            },
        )

    def _calculate_macro_alignment(self, profile: ETFProfile, macro: MacroRegime) -> float:
        """Calculate score for how well the ETF aligns with current macro regime."""
        is_inverse = profile.leverage < 0
        cat = profile.category

        if macro.regime_type == MacroRegimeType.RISK_ON_EXPANSION:
            if is_inverse:
                return 15.0  # Strongly avoid shorting in a roaring bull market
            if cat in ("leveraged_tech_semi", "thematic_and_crypto"):
                return 92.0
            if cat == "leveraged_broad_smallcap":
                return 85.0
            return 70.0

        elif macro.regime_type == MacroRegimeType.RISK_OFF_CONTRACTION:
            if is_inverse:
                return 90.0  # Hedges shine in risk-off
            if cat in ("leveraged_tech_semi", "leveraged_broad_smallcap"):
                return 15.0  # Toxic leverage decay
            return 30.0

        elif macro.regime_type == MacroRegimeType.VOLATILITY_EXPANSION:
            if cat == "commodities_and_volatility":
                return 80.0
            return 35.0

        elif macro.regime_type == MacroRegimeType.DEFENSIVE:
            if cat in ("leveraged_tech_semi", "leveraged_broad_smallcap"):
                return 40.0
            return 55.0

        else:  # CHOPPY_ROTATION
            if is_inverse:
                return 35.0
            if cat in ("leveraged_tech_semi", "thematic_and_crypto"):
                return 65.0
            return 50.0

    def _generate_thesis(
        self,
        profile: ETFProfile,
        metrics: QuantMetrics,
        macro: MacroRegime,
        action: RecommendationAction,
        conviction: float,
    ) -> str:
        """Generate a concise, high-clarity investment thesis in Turkish."""
        sym = profile.symbol
        lev = f"{profile.leverage}x kaldıraçlı " if abs(profile.leverage) > 1.0 else ""
        theme = profile.theme or profile.underlying

        if action == RecommendationAction.STRONG_BUY:
            return (
                f"{sym}, {theme} temasına odaklı {lev}yüksek getirili bir enstrümandır. "
                f"Son 20 günde %{metrics.return_20d*100:+.1f} getiri ve {metrics.trend_alignment} teknik görünüm sergilemektedir. "
                f"Mevcut {macro.regime_type.value} makro ortamı büyüme ve teknoloji lehine olup, "
                f"risk/ödül asimetrisi belirgin şekilde alım yönündedir."
            )
        elif action == RecommendationAction.SPECULATIVE_BUY:
            return (
                f"{sym} ({theme}) yüksek volatilite ve güçlü yukarı yönlü tepki potansiyeline sahiptir. "
                f"Pozitif momentum ({metrics.rsi_14:.1f} RSI) korunmakta olup, sıkı stop loss (%{metrics.annualized_volatility*10:.1f}) "
                f"ile kontrollü taktik pozisyon için uygundur."
            )
        elif action == RecommendationAction.HEDGE_SHORT:
            return (
                f"Piyasa {macro.regime_type.value} evresinde olduğundan, {sym} portföyü aşağı yönlü kırılmalardan "
                f"korumak ve volatiliteden getiri üretmek üzere taktik kısa/hedge aracı olarak önerilmektedir."
            )
        elif action == RecommendationAction.HOLD_NEUTRAL:
            return (
                f"{sym} için teknik göstergeler nötr konsolidasyona işaret ediyor. Yeni pozisyon açmak için "
                f"katalizör veya hacimli kırılım beklenmelidir."
            )
        else:
            return (
                f"{sym} için volatilite erimesi veya negatif momentum riski yüksektir. Mevcut makro rejimde "
                f"nakitte kalmak veya daha güçlü alternatiflere yönelmek önerilir."
            )
