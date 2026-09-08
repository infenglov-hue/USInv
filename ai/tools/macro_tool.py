"""Macroeconomic and market regime evaluation tool using multi-asset proxies."""

from pathlib import Path
from typing import Any
import yaml

from ai.core.models import BarData, MacroRegime, MacroRegimeType
from ai.tools.alpaca_client import AlpacaClient


class MacroTool:
    """Evaluates broader macroeconomic regime, interest rate pressure, and risk appetite."""

    def __init__(
        self,
        client: AlpacaClient,
        config_path: str | Path | None = None,
    ):
        self.client = client
        self.base_dir = Path(__file__).resolve().parent.parent
        self.config_path = Path(config_path or (self.base_dir / "config" / "macro_indicators.yaml"))
        self.config: dict[str, Any] = {}
        self._load_config()

    def _load_config(self) -> None:
        if self.config_path.exists():
            with open(self.config_path, "r", encoding="utf-8") as f:
                self.config = yaml.safe_load(f) or {}

    def get_macro_regime(self) -> MacroRegime:
        """Fetch macro proxy bars and classify the macroeconomic regime."""
        benchmarks = self.config.get("benchmarks", {})
        symbols = [b["symbol"] for b in benchmarks.values()]
        if not symbols:
            symbols = ["SPY", "QQQ", "TLT", "GLD", "UUP", "UVXY", "HYG", "SMH"]

        bars_dict = self.client.get_bars(symbols, limit=60)

        spy_bars = bars_dict.get("SPY", [])
        qqq_bars = bars_dict.get("QQQ", [])
        tlt_bars = bars_dict.get("TLT", [])
        uvxy_bars = bars_dict.get("UVXY", [])
        uup_bars = bars_dict.get("UUP", [])
        hyg_bars = bars_dict.get("HYG", [])
        smh_bars = bars_dict.get("SMH", [])

        # Assess individual trends
        spy_trend = self._assess_bar_trend(spy_bars)
        qqq_trend = self._assess_bar_trend(qqq_bars)
        tlt_trend = self._assess_bar_trend(tlt_bars)
        uvxy_trend = self._assess_bar_trend(uvxy_bars)
        uup_trend = self._assess_bar_trend(uup_bars)
        hyg_trend = self._assess_bar_trend(hyg_bars)

        # Quantitative checks
        spy_bullish = spy_trend in ("STRONG_BULLISH", "BULLISH")
        qqq_bullish = qqq_trend in ("STRONG_BULLISH", "BULLISH")
        uvxy_spiking = uvxy_trend in ("STRONG_BULLISH", "BULLISH")
        hyg_bullish = hyg_trend in ("STRONG_BULLISH", "BULLISH")
        rates_easing = tlt_trend in ("STRONG_BULLISH", "BULLISH")

        confidence = 70.0

        if spy_bullish and qqq_bullish and not uvxy_spiking:
            regime = MacroRegimeType.RISK_ON_EXPANSION
            confidence = 85.0 if hyg_bullish else 75.0
            summary = (
                "Equities are in an established uptrend led by tech/growth, with volatility suppressed. "
                "Ideal environment for aggressive high-beta and leveraged bull instruments."
            )
            favored = ["leveraged_tech_semi", "thematic_and_crypto", "leveraged_broad_smallcap"]
            unfavored = ["leveraged_inverse_hedge", "commodities_and_volatility"]

        elif not spy_bullish and not qqq_bullish and uvxy_spiking:
            regime = MacroRegimeType.RISK_OFF_CONTRACTION
            confidence = 85.0
            summary = (
                "Severe risk-off contraction. Major indices breaking down with volatility surging. "
                "Leveraged long positions carry acute decay risk; tactical inverse hedges favored."
            )
            favored = ["leveraged_inverse_hedge", "commodities_and_volatility"]
            unfavored = ["leveraged_tech_semi", "leveraged_broad_smallcap", "thematic_and_crypto"]

        elif uvxy_spiking:
            regime = MacroRegimeType.VOLATILITY_EXPANSION
            confidence = 80.0
            summary = (
                "Volatility expansion shock underway. High whipsaw risk. Caution advised for 3x funds."
            )
            favored = ["commodities_and_volatility", "leveraged_inverse_hedge"]
            unfavored = ["leveraged_tech_semi", "thematic_and_crypto"]

        elif rates_easing and not spy_bullish:
            regime = MacroRegimeType.DEFENSIVE
            confidence = 75.0
            summary = (
                "Defensive posture. Bonds rallying while equity momentum stalls. Selective stock picking."
            )
            favored = ["commodities_and_volatility"]
            unfavored = ["leveraged_broad_smallcap", "leveraged_tech_semi"]

        else:
            regime = MacroRegimeType.CHOPPY_ROTATION
            confidence = 65.0
            summary = (
                "Mixed / rotational market regime. Divergence between sectors. Strict risk management "
                "and shorter holding horizons recommended."
            )
            favored = ["thematic_and_crypto", "leveraged_tech_semi"]
            unfavored = ["leveraged_broad_smallcap"]

        return MacroRegime(
            regime_type=regime,
            confidence_score=confidence,
            summary=summary,
            spy_trend=spy_trend,
            qqq_trend=qqq_trend,
            rates_tlt_trend=tlt_trend,
            volatility_uvxy_trend=uvxy_trend,
            dollar_uup_trend=uup_trend,
            credit_hyg_trend=hyg_trend,
            favored_etf_types=favored,
            unfavored_etf_types=unfavored,
        )

    def _assess_bar_trend(self, bars: list[BarData]) -> str:
        """Helper to determine trend based on recent price vs simple moving averages."""
        if not bars or len(bars) < 10:
            return "NEUTRAL"

        closes = [b.close for b in bars]
        current = closes[-1]
        sma_20 = sum(closes[-20:]) / min(len(closes), 20)
        ret_5d = (current / closes[-5] - 1.0) if len(closes) >= 5 else 0.0

        if current > sma_20 and ret_5d > 0.01:
            return "STRONG_BULLISH"
        elif current > sma_20:
            return "BULLISH"
        elif current < sma_20 and ret_5d < -0.01:
            return "STRONG_BEARISH"
        elif current < sma_20:
            return "BEARISH"
        else:
            return "NEUTRAL"
