"""DuckDB persistence engine for high-risk ETF scans and recommendations."""

from pathlib import Path
import duckdb

from ai.core.models import InvestmentRecommendation, MacroRegime


class StorageDB:
    """DuckDB wrapper for storing and querying investment history and recommendations."""

    def __init__(self, db_path: str | Path | None = None):
        base_dir = Path(__file__).resolve().parent.parent
        self.db_path = Path(db_path or (base_dir / "data" / "ai_alpha.duckdb"))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _init_db(self) -> None:
        """Create required tables if they don't already exist."""
        with duckdb.connect(str(self.db_path)) as con:
            con.execute("""
                CREATE TABLE IF NOT EXISTS recommendations (
                    id VARCHAR PRIMARY KEY,
                    symbol VARCHAR,
                    name VARCHAR,
                    category VARCHAR,
                    leverage DOUBLE,
                    action VARCHAR,
                    conviction_score DOUBLE,
                    current_price DOUBLE,
                    entry_low DOUBLE,
                    entry_high DOUBLE,
                    target_1 DOUBLE,
                    target_2 DOUBLE,
                    stop_loss DOUBLE,
                    risk_reward DOUBLE,
                    max_alloc_pct DOUBLE,
                    macro_regime VARCHAR,
                    thesis TEXT,
                    generated_at TIMESTAMP
                );
            """)
            con.execute("""
                CREATE TABLE IF NOT EXISTS macro_snapshots (
                    id VARCHAR PRIMARY KEY,
                    regime_type VARCHAR,
                    confidence_score DOUBLE,
                    summary TEXT,
                    spy_trend VARCHAR,
                    qqq_trend VARCHAR,
                    uvxy_trend VARCHAR,
                    measured_at TIMESTAMP
                );
            """)

    def save_recommendation(self, rec: InvestmentRecommendation) -> None:
        """Persist an investment recommendation."""
        import uuid

        rec_id = f"{rec.symbol}_{rec.generated_at.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        with duckdb.connect(str(self.db_path)) as con:
            con.execute(
                """
                INSERT OR REPLACE INTO recommendations VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                );
            """,
                [
                    rec_id,
                    rec.symbol,
                    rec.name,
                    rec.category,
                    rec.leverage,
                    rec.action.value,
                    rec.conviction_score,
                    rec.metrics.current_price,
                    rec.trade_plan.entry_zone_low,
                    rec.trade_plan.entry_zone_high,
                    rec.trade_plan.target_1,
                    rec.trade_plan.target_2,
                    rec.trade_plan.stop_loss,
                    rec.trade_plan.risk_reward_ratio,
                    rec.trade_plan.max_portfolio_allocation_pct,
                    rec.macro_context,
                    rec.thesis_summary,
                    rec.generated_at,
                ],
            )

    def save_macro_snapshot(self, regime: MacroRegime) -> None:
        """Persist a macro regime evaluation."""
        import uuid

        snap_id = f"macro_{regime.measured_at.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
        with duckdb.connect(str(self.db_path)) as con:
            con.execute(
                """
                INSERT OR REPLACE INTO macro_snapshots VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?
                );
            """,
                [
                    snap_id,
                    regime.regime_type.value,
                    regime.confidence_score,
                    regime.summary,
                    regime.spy_trend,
                    regime.qqq_trend,
                    regime.volatility_uvxy_trend,
                    regime.measured_at,
                ],
            )

    def get_recent_recommendations(self, limit: int = 15) -> list[dict]:
        """Query most recent recommendations."""
        with duckdb.connect(str(self.db_path)) as con:
            df = con.execute(
                "SELECT * FROM recommendations ORDER BY generated_at DESC LIMIT ?", [limit]
            ).df()
            return df.to_dict(orient="records")
