"""Integration test for end-to-end autonomous orchestrator pipeline."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from ai.agent.orchestrator import AutonomousOrchestrator
from ai.core.models import BarData, NewsItem, SentimentType
from ai.tools.alpaca_client import AlpacaClient


def test_orchestrator_pipeline(tmp_path):
    mock_client = MagicMock(spec=AlpacaClient)

    base_time = datetime(2026, 1, 1, 14, 30, tzinfo=timezone.utc)
    mock_bars = [
        BarData(
            timestamp=base_time + timedelta(days=i),
            open=100.0 + i,
            high=102.0 + i,
            low=99.0 + i,
            close=101.0 + i,
            volume=2_000_000.0,
            vwap=100.5 + i,
        )
        for i in range(60)
    ]

    symbols = ["SPY", "QQQ", "TLT", "GLD", "UUP", "UVXY", "HYG", "SMH", "TQQQ", "SOXL"]
    mock_client.get_bars.return_value = {s: mock_bars for s in symbols}
    mock_client.get_news.return_value = [
        NewsItem(
            id="1",
            headline="Tech sector surges to new highs on strong corporate earnings",
            published_at=datetime.now(timezone.utc),
            sentiment=SentimentType.BULLISH,
        )
    ]

    db_path = tmp_path / "test_alpha.duckdb"
    reports_dir = tmp_path / "reports"

    orchestrator = AutonomousOrchestrator(
        client=mock_client,
        db_path=db_path,
        reports_dir=reports_dir,
    )

    macro, recs = orchestrator.run_discovery_pipeline(top_n=2)

    assert macro is not None
    assert len(recs) > 0
    assert db_path.exists()

    # Verify generated markdown files
    md_files = list(reports_dir.glob("*.md"))
    assert len(md_files) > 0
