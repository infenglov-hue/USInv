"""Autonomous screener tool for filtering high-risk high-reward ETF universe."""

from pathlib import Path
from typing import Any
import yaml

from ai.core.models import ETFProfile, QuantMetrics
from ai.tools.alpaca_client import AlpacaClient
from ai.tools.technical_tool import TechnicalTool


class ScreenerTool:
    """Screens universe of funds and ETFs for high-risk / high-reward potential."""

    def __init__(
        self,
        client: AlpacaClient,
        technical_tool: TechnicalTool,
        universe_config_path: str | Path | None = None,
        settings_config_path: str | Path | None = None,
    ):
        self.client = client
        self.technical_tool = technical_tool
        self.base_dir = Path(__file__).resolve().parent.parent
        self.universe_path = Path(universe_config_path or (self.base_dir / "config" / "universe.yaml"))
        self.settings_path = Path(settings_config_path or (self.base_dir / "config" / "settings.yaml"))

        self.universe: list[ETFProfile] = []
        self.settings: dict[str, Any] = {}
        self._load_configs()

    def _load_configs(self) -> None:
        """Load universe and settings YAML files."""
        if self.universe_path.exists():
            with open(self.universe_path, "r", encoding="utf-8") as f:
                raw_u = yaml.safe_load(f) or {}
                categories = raw_u.get("categories", {})
                for cat_key, cat_val in categories.items():
                    symbols = cat_val.get("symbols", [])
                    for s in symbols:
                        self.universe.append(
                            ETFProfile(
                                symbol=s["symbol"],
                                name=s["name"],
                                category=cat_key,
                                leverage=float(s.get("leverage", 1.0)),
                                underlying=s.get("underlying", ""),
                                theme=s.get("theme", ""),
                                risk_level=cat_val.get("risk_level", "HIGH"),
                            )
                        )

        if self.settings_path.exists():
            with open(self.settings_path, "r", encoding="utf-8") as f:
                self.settings = yaml.safe_load(f) or {}

    def screen_universe(
        self,
        categories: list[str] | None = None,
        min_volatility: float | None = None,
        require_liquid: bool = True,
    ) -> list[tuple[ETFProfile, QuantMetrics]]:
        """Screen and rank universe ETFs, returning candidates sorted by composite score."""
        target_profiles = self.universe
        if categories:
            target_profiles = [p for p in target_profiles if p.category in categories]

        symbols = [p.symbol for p in target_profiles]
        if not symbols:
            return []

        # Batch fetch bars for efficiency
        bars_dict = self.client.get_bars(symbols, limit=200)

        # Pre-warm benchmarks
        spy_bars, qqq_bars = self.technical_tool.get_benchmark_bars()

        screen_conf = self.settings.get("screener", {})
        min_vol = min_volatility or screen_conf.get("min_annualized_volatility", 0.25)
        min_price = screen_conf.get("min_price", 5.0)

        results: list[tuple[ETFProfile, QuantMetrics]] = []

        for profile in target_profiles:
            bars = bars_dict.get(profile.symbol, [])
            if not bars or len(bars) < 10:
                continue

            metrics, _ = self.technical_tool.analyze_symbol(profile.symbol, custom_bars=bars)

            # Price check
            if metrics.current_price < min_price:
                continue

            # Liquidity check
            if require_liquid and not metrics.is_liquid:
                continue

            # High risk / volatility check (leveraged ETFs are automatically high risk)
            if abs(profile.leverage) <= 1.0 and metrics.annualized_volatility < min_vol:
                continue

            results.append((profile, metrics))

        # Composite ranking
        def composite_score(item: tuple[ETFProfile, QuantMetrics]) -> float:
            p, m = item
            # Favor positive momentum + high Sortino + bullish trend
            trend_bonus = 15.0 if "BULLISH" in m.trend_alignment else (0.0 if m.trend_alignment == "NEUTRAL" else -15.0)
            score = (
                (m.return_20d * 40.0)
                + (m.return_5d * 20.0)
                + (max(-2.0, min(m.sortino_ratio, 4.0)) * 10.0)
                + trend_bonus
            )
            return score

        results.sort(key=composite_score, reverse=True)
        return results
