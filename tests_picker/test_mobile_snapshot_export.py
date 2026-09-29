"""Tests for the offline mobile snapshot export."""

from __future__ import annotations

import json
import sqlite3
import gzip
from datetime import date, timedelta
from datetime import datetime, timezone

from click.testing import CliRunner
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

import us_picker.read_service as read_service
import us_picker.mobile_feed as mobile_feed
from us_picker.cli import cli
from us_picker.db.schema import (
    AdjustedMetric,
    Base,
    Company,
    DailyPrice,
    MacroRegime,
    PortfolioSelection,
    ScoringResult,
)
from us_picker.mobile_snapshot import (
    ROOM_DATABASE_VERSION,
    SNAPSHOT_SCHEMA_VERSION,
    _coherent_model_performance_records,
    export_mobile_snapshot,
    validate_mobile_snapshot,
)
from us_picker.mobile_feed import export_mobile_feed
from us_picker.mobile_feed import _load_backtest_audit


@pytest.fixture
def source_engine(monkeypatch):
    """Provide an isolated source database for snapshot export tests."""
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine)

    monkeypatch.setattr(read_service, "get_engine", lambda: engine)

    session = session_factory()
    try:
        company = Company(
            ticker="TEST1",
            name="Test One",
            company_type="OPERATING",
            sector_bist="Industrial",
            sector_custom="Industrial",
            free_float_pct=35.0,
            is_bist100=True,
            is_active=True,
        )
        session.add(company)
        session.flush()

        today = date(2026, 3, 19)
        session.add_all(
            [
                DailyPrice(
                    company_id=company.id,
                    date=today - timedelta(days=365),
                    close=80.0,
                    adjusted_close=82.0,
                    volume=15_000_000,
                ),
                DailyPrice(
                    company_id=company.id,
                    date=today,
                    close=100.0,
                    adjusted_close=101.0,
                    high=102.0,
                    low=99.0,
                    volume=20_000_000,
                ),
                PortfolioSelection(
                    portfolio="ALPHA",
                    selection_date=today - timedelta(days=7),
                    signal_date=today - timedelta(days=8),
                    company_id=company.id,
                    entry_price=95.0,
                    composite_score=89.0,
                    target_price=120.0,
                    target_source="SCORE_IMPLIED",
                    stop_loss_price=88.0,
                    cycle_ref_date=today,
                    cycle_signal_date=today,
                ),
                PortfolioSelection(
                    portfolio="ALPHA",
                    selection_date=today - timedelta(days=60),
                    signal_date=today - timedelta(days=61),
                    company_id=company.id,
                    entry_price=75.0,
                    exit_date=today - timedelta(days=30),
                    exit_price=90.0,
                ),
                ScoringResult(
                    company_id=company.id,
                    scoring_date=today,
                    model_used="OPERATING",
                    composite_alpha=91.0,
                    composite_beta=77.0,
                    composite_delta=66.0,
                    buffett_score=80.0,
                    graham_score=72.0,
                    piotroski_fscore=66.0,
                    piotroski_fscore_raw=6,
                    magic_formula_rank=64.0,
                    lynch_peg_score=61.0,
                    dcf_margin_of_safety_pct=18.0,
                    momentum_score=68.0,
                    technical_score=70.0,
                    above_200ma=True,
                    dividend_score=55.0,
                    risk_tier="LOW",
                    data_completeness=88.0,
                    quality_flags_json=json.dumps({"earnings": "clean"}),
                    target_price=123.0,
                    target_source="DCF_INTRINSIC",
                    stop_loss_price=88.5,
                ),
                AdjustedMetric(
                    company_id=company.id,
                    period_end=today - timedelta(days=90),
                    adjusted_net_income=1_250_000.0,
                    owner_earnings=1_100_000.0,
                    free_cash_flow=950_000.0,
                    roe_adjusted=0.22,
                    roa_adjusted=0.11,
                    real_eps_growth_pct=0.18,
                ),
                MacroRegime(
                    date=today,
                    policy_rate_pct=0.42,
                    cpi_yoy_pct=0.31,
                    usdtry_rate=39.1,
                    regime="RISK_OFF",
                ),
            ]
        )
        session.commit()
    finally:
        session.close()

    return engine


def test_export_mobile_snapshot_writes_expected_tables(source_engine, tmp_path):
    output_path = tmp_path / "mobile_snapshot.db"

    exported_path = export_mobile_snapshot(output_path)
    metadata = validate_mobile_snapshot(exported_path)

    assert exported_path == output_path.resolve()
    assert metadata["schema_version"] == SNAPSHOT_SCHEMA_VERSION
    assert metadata["snapshot_date"] == "2026-03-19"
    assert metadata["company_count"] == 1

    with sqlite3.connect(exported_path) as connection:
        connection.row_factory = sqlite3.Row

        user_version = connection.execute("PRAGMA user_version").fetchone()[0]
        assert user_version == ROOM_DATABASE_VERSION

        scoring_row = connection.execute(
            "SELECT ticker, alpha, quality_flags_json, beta, delta, "
            "above_200ma, target_price, target_source, stop_loss_price "
            "FROM scoring_latest"
        ).fetchone()
        assert dict(scoring_row) == {
            "ticker": "TEST1",
            "alpha": pytest.approx(91.0),
            "quality_flags_json": json.dumps({"earnings": "clean"}),
            "beta": pytest.approx(77.0),
            "delta": pytest.approx(66.0),
            "above_200ma": 1,
            "target_price": pytest.approx(123.0),
            "target_source": "DCF_INTRINSIC",
            "stop_loss_price": pytest.approx(88.5),
        }

        adjusted_row = connection.execute(
            "SELECT adjusted_net_income, roe_adjusted FROM adjusted_metrics_latest"
        ).fetchone()
        assert adjusted_row["adjusted_net_income"] == pytest.approx(1_250_000.0)
        assert adjusted_row["roe_adjusted"] == pytest.approx(0.22)

        open_position = connection.execute(
            "SELECT ticker, current_price, target_price, target_source, "
            "selection_date, signal_date, cycle_ref_date, cycle_signal_date "
            "FROM open_positions"
        ).fetchone()
        assert dict(open_position) == {
            "ticker": "TEST1",
            "current_price": pytest.approx(101.0),
            "target_price": pytest.approx(120.0),
            "target_source": "SCORE_IMPLIED",
            "selection_date": "2026-03-12",
            "signal_date": "2026-03-11",
            "cycle_ref_date": "2026-03-19",
            "cycle_signal_date": "2026-03-19",
        }

        price_count = connection.execute(
            "SELECT COUNT(*) FROM price_history_730d"
        ).fetchone()[0]
        assert price_count == 2


def test_latest_price_date_ignores_index_only_update(source_engine, tmp_path):
    session = sessionmaker(bind=source_engine)()
    try:
        xu100 = Company(
            ticker="XU100",
            name="BIST 100",
            company_type="INDEX",
            is_active=False,
        )
        session.add(xu100)
        session.flush()
        session.add(
            DailyPrice(
                company_id=xu100.id,
                date=date(2026, 3, 20),
                close=10_250.0,
                adjusted_close=10_250.0,
                volume=1_000_000_000,
            )
        )
        session.commit()
    finally:
        session.close()

    output_path = tmp_path / "mobile_snapshot.db"
    exported_path = export_mobile_snapshot(output_path)

    with sqlite3.connect(exported_path) as connection:
        latest_price_date = connection.execute(
            "SELECT latest_price_date FROM snapshot_metadata WHERE id = 1"
        ).fetchone()[0]

    assert latest_price_date == "2026-03-19"


def test_export_mobile_snapshot_cli_command(source_engine, tmp_path):
    output_path = tmp_path / "cli_snapshot.db"
    runner = CliRunner()

    result = runner.invoke(cli, ["export-mobile-snapshot", "--output", str(output_path)])

    assert result.exit_code == 0
    assert output_path.exists()
    assert "Mobile snapshot exported" in result.output


def test_export_mobile_feed_writes_manifest_and_gzip(source_engine, tmp_path):
    feed_dir = tmp_path / "feed"

    result = export_mobile_feed(
        feed_dir,
        base_download_url="https://example.test/mobile-feed",
    )

    manifest_path = feed_dir / "manifest.json"
    snapshot_gzip_path = feed_dir / "mobile_snapshot.db.gz"
    live_tickers_path = feed_dir / "live_tickers.json"

    assert result.manifest_path == manifest_path
    assert result.snapshot_path == snapshot_gzip_path
    assert result.live_tickers_path == live_tickers_path
    assert manifest_path.exists()
    assert snapshot_gzip_path.exists()
    assert live_tickers_path.exists()

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["snapshot_version"] == ROOM_DATABASE_VERSION
    assert "exported_at" in manifest
    assert manifest["snapshot"]["filename"] == "mobile_snapshot.db.gz"
    assert manifest["snapshot"]["url"] == "https://example.test/mobile-feed/mobile_snapshot.db.gz"
    assert manifest["snapshot"]["size_bytes"] == snapshot_gzip_path.stat().st_size
    assert manifest["snapshot"]["compression"] == "gzip"
    assert len(manifest["snapshot"]["sha256"]) == 64
    assert manifest["live_tickers"]["filename"] == "live_tickers.json"
    assert manifest["live_tickers"]["ticker_count"] == 2
    live_tickers = json.loads(live_tickers_path.read_text(encoding="utf-8"))
    assert live_tickers["tickers"] == ["TEST1", "XU100"]

    extracted_snapshot = tmp_path / "extracted_snapshot.db"
    with gzip.open(snapshot_gzip_path, "rb") as source:
        extracted_snapshot.write_bytes(source.read())

    metadata = validate_mobile_snapshot(extracted_snapshot)
    assert metadata["snapshot_date"] == "2026-03-19"


def test_export_mobile_feed_rejects_stale_snapshot_regression(source_engine, tmp_path):
    feed_dir = tmp_path / "feed"
    feed_dir.mkdir()
    existing_db = tmp_path / "existing_snapshot.db"
    with sqlite3.connect(existing_db) as connection:
        connection.execute(
            """
            CREATE TABLE snapshot_metadata (
                id INTEGER PRIMARY KEY,
                snapshot_date TEXT,
                latest_price_date TEXT,
                exported_at TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO snapshot_metadata
            (id, snapshot_date, latest_price_date, exported_at)
            VALUES (1, '2026-03-20', '2026-03-20', '2026-03-20T08:00:00+00:00')
            """
        )
        connection.commit()

    snapshot_gzip_path = feed_dir / "mobile_snapshot.db.gz"
    with existing_db.open("rb") as source, gzip.open(snapshot_gzip_path, "wb") as target:
        target.write(source.read())

    with pytest.raises(RuntimeError, match="stale data"):
        export_mobile_feed(feed_dir)


def test_export_mobile_feed_ensures_runtime_schema(source_engine, tmp_path, monkeypatch):
    calls = []

    def fake_ensure_runtime_db_ready(engine=None):
        calls.append(True)
        return engine or source_engine

    monkeypatch.setattr(mobile_feed, "ensure_runtime_db_ready", fake_ensure_runtime_db_ready)

    export_mobile_feed(tmp_path / "feed")

    assert calls == [True]


def test_export_mobile_feed_cli_command(source_engine, tmp_path):
    feed_dir = tmp_path / "cli_feed"
    runner = CliRunner()

    result = runner.invoke(
        cli,
        [
            "export-mobile-feed",
            "--feed-dir",
            str(feed_dir),
            "--base-download-url",
            "https://example.test/mobile-feed",
        ],
    )

    assert result.exit_code == 0
    assert (feed_dir / "manifest.json").exists()
    assert (feed_dir / "mobile_snapshot.db.gz").exists()
    assert "Mobile feed exported" in result.output


def test_export_mobile_feed_cli_does_not_prune_runtime_history(
    source_engine,
    tmp_path,
    monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "state-repo").mkdir()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    runtime_db = data_dir / "us_picker.db"
    with sqlite3.connect(runtime_db) as connection:
        connection.execute(
            "CREATE TABLE scoring_results (scoring_date TEXT, composite_alpha REAL)"
        )
        connection.execute("CREATE TABLE daily_prices (date TEXT, close REAL)")
        connection.execute(
            "INSERT INTO scoring_results VALUES ('2018-03-19', 75.0)"
        )
        connection.execute("INSERT INTO daily_prices VALUES ('2018-03-19', 100.0)")
        connection.commit()

    result = CliRunner().invoke(
        cli,
        ["export-mobile-feed", "--feed-dir", str(tmp_path / "feed")],
    )

    assert result.exit_code == 0
    with sqlite3.connect(runtime_db) as connection:
        scoring_count = connection.execute(
            "SELECT COUNT(*) FROM scoring_results"
        ).fetchone()[0]
        price_count = connection.execute(
            "SELECT COUNT(*) FROM daily_prices"
        ).fetchone()[0]
        assert scoring_count == 1
        assert price_count == 1


def test_backtest_audit_loader_rejects_stale_report(tmp_path):
    path = tmp_path / "audit.json"
    path.write_text(
        json.dumps(
            {
                "generated_at": "2026-07-01T00:00:00+00:00",
                "execution_mode": "same_day_open",
            }
        ),
        encoding="utf-8",
    )

    assert _load_backtest_audit(
        path,
        now=datetime(2026, 7, 13, tzinfo=timezone.utc),
    ) is None
    assert _load_backtest_audit(
        path,
        max_age_days=14,
        now=datetime(2026, 7, 13, tzinfo=timezone.utc),
    )["execution_mode"] == "same_day_open"


def test_model_performance_export_drops_pre_reset_segment():
    rows = [
        {
            "date": "2026-04-20",
            "strategy_return": 227.5,
            "benchmark_return": 153.5,
            "alpha": 74.0,
        },
        {
            "date": "2026-04-27",
            "strategy_return": 100.0,
            "benchmark_return": 100.0,
            "alpha": 0.0,
        },
        {
            "date": "2026-05-04",
            "strategy_return": 100.0,
            "benchmark_return": 100.0,
            "alpha": 0.0,
        },
        {
            "date": "2026-05-11",
            "strategy_return": 102.0,
            "benchmark_return": 101.0,
            "alpha": 1.0,
        },
    ]

    coherent = _coherent_model_performance_records(rows)

    assert [row["date"] for row in coherent] == ["2026-05-04", "2026-05-11"]
