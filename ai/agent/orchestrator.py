"""Autonomous orchestrator driving multi-stage investigation and recommendation pipeline."""

from pathlib import Path
from ai.agent.decision_engine import DecisionEngine
from ai.core.models import (
    ETFProfile,
    InvestmentRecommendation,
    MacroRegime,
    MacroRegimeType,
)
from ai.reports.generator import ReportGenerator
from ai.storage.db import StorageDB
from ai.tools.alpaca_client import AlpacaClient
from ai.tools.macro_tool import MacroTool
from ai.tools.news_tool import NewsTool
from ai.tools.screener_tool import ScreenerTool
from ai.tools.technical_tool import TechnicalTool


class AutonomousOrchestrator:
    """End-to-end agent orchestrator for high-risk, high-reward ETF discovery."""

    def __init__(
        self,
        client: AlpacaClient | None = None,
        db_path: str | Path | None = None,
        reports_dir: str | Path | None = None,
    ):
        self.client = client or AlpacaClient()
        self.technical_tool = TechnicalTool(self.client)
        self.screener_tool = ScreenerTool(self.client, self.technical_tool)
        self.macro_tool = MacroTool(self.client)
        self.news_tool = NewsTool(self.client)
        self.decision_engine = DecisionEngine()
        self.db = StorageDB(db_path=db_path)
        self.report_gen = ReportGenerator(output_dir=reports_dir)

    def run_discovery_pipeline(
        self,
        top_n: int = 4,
        categories: list[str] | None = None,
    ) -> tuple[MacroRegime, list[InvestmentRecommendation]]:
        """Execute full autonomous scan: macro regime -> screening -> catalyst deep-dive -> trade plan."""
        # 1. Establish Macroeconomic Regime
        macro = self.macro_tool.get_macro_regime()
        self.db.save_macro_snapshot(macro)

        # 2. Screen Universe
        screened_candidates = self.screener_tool.screen_universe(
            categories=categories,
            require_liquid=True,
        )

        # 3. Autonomous Filtering based on Macro Regime
        # If market is RISK_OFF, prioritize inverse hedges or short volatility
        # If market is RISK_ON, filter out inverse ETFs and focus on top momentum tech/semi/crypto
        filtered: list[tuple[ETFProfile, any]] = []
        for profile, metrics in screened_candidates:
            is_inverse = profile.leverage < 0
            if macro.regime_type == MacroRegimeType.RISK_ON_EXPANSION and is_inverse:
                continue  # Never short in strong risk-on
            if macro.regime_type == MacroRegimeType.RISK_OFF_CONTRACTION and not is_inverse:
                # In risk-off, only consider long assets if they have exceptional relative strength
                if metrics.return_20d < 0.05:
                    continue
            filtered.append((profile, metrics))

        top_candidates = filtered[:top_n]

        # 4. Deep-dive into Top Candidates
        recommendations: list[InvestmentRecommendation] = []
        for profile, metrics in top_candidates:
            # Autonomously call news tool for catalysts
            news_items, bull_catalysts, bear_risks = self.news_tool.get_catalysts_for_symbol(profile.symbol)

            # Get full bars
            _, bars = self.technical_tool.analyze_symbol(profile.symbol)

            # Evaluate with decision engine
            rec = self.decision_engine.evaluate_candidate(
                profile=profile,
                metrics=metrics,
                bars=bars,
                macro=macro,
                news_items=news_items,
                bull_catalysts=bull_catalysts,
                bear_risks=bear_risks,
            )

            # Persist to database & file
            self.db.save_recommendation(rec)
            self.report_gen.save_report(rec, macro)
            recommendations.append(rec)

        # Sort recommendations by conviction score
        recommendations.sort(key=lambda r: r.conviction_score, reverse=True)
        return macro, recommendations

    def deep_dive(self, symbol: str) -> tuple[MacroRegime, InvestmentRecommendation]:
        """Perform autonomous deep dive on a specific single fund or ETF."""
        sym_clean = symbol.strip().upper()

        # 1. Macro Regime
        macro = self.macro_tool.get_macro_regime()

        # 2. Find profile in universe or infer generic profile
        profile = None
        for p in self.screener_tool.universe:
            if p.symbol == sym_clean:
                profile = p
                break

        if not profile:
            # Fetch asset info from Alpaca
            asset_info = self.client.get_asset(sym_clean)
            name = asset_info.get("name", sym_clean) if asset_info else sym_clean
            # Simple heuristic for leverage
            lev = 1.0
            if any(k in name.lower() for k in ["3x", "ultrapro", "bull 3x", "bear 3x"]):
                lev = 3.0 if "bull" in name.lower() or "ultrapro qqq" in name.lower() else -3.0
            elif any(k in name.lower() for k in ["2x", "ultra"]):
                lev = 2.0

            profile = ETFProfile(
                symbol=sym_clean,
                name=name,
                category="custom_deepdive",
                leverage=lev,
                underlying=sym_clean,
                theme="Deep-dive Asset",
                risk_level="HIGH",
            )

        # 3. Technical analysis
        metrics, bars = self.technical_tool.analyze_symbol(sym_clean)

        # 4. News & Catalysts
        news_items, bull_catalysts, bear_risks = self.news_tool.get_catalysts_for_symbol(sym_clean)

        # 5. Synthesize recommendation
        rec = self.decision_engine.evaluate_candidate(
            profile=profile,
            metrics=metrics,
            bars=bars,
            macro=macro,
            news_items=news_items,
            bull_catalysts=bull_catalysts,
            bear_risks=bear_risks,
        )

        # 6. Save report & DB
        self.db.save_recommendation(rec)
        self.report_gen.save_report(rec, macro)

        return macro, rec
