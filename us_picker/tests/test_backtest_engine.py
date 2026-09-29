import pytest
import pandas as pd
from datetime import date, timedelta
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from us_picker.db.schema import (
    Base,
    Company,
    DailyPrice,
    ScoringResult,
    ModelPerformance
)
from us_picker.portfolio.selector import PortfolioSelector
from us_picker.backtest.engine import (
    BacktestEngine,
    _compose_scores_for_date,
    _ensure_scores_for_date,
)
from us_picker.scoring.version import SCORING_PIPELINE_VERSION


@pytest.fixture
def session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    sess = Session()
    yield sess
    sess.close()


def test_backtest_engine_calculates_returns(session, monkeypatch, tmp_path):
    # Setup rebalance dates (2 Mondays: March 16 & March 23, 2026)
    d1 = date(2026, 3, 16)
    d2 = date(2026, 3, 23)
    exec_d = d1 + timedelta(days=1)

    # 1. Add mock companies
    c1 = Company(ticker="TEST1", name="Test 1", company_type="OPERATING", is_active=True)
    c2 = Company(ticker="TEST2", name="Test 2", company_type="OPERATING", is_active=True)
    benchmark = Company(ticker="SPY", name="BIST 100", company_type="INDEX", is_active=False)
    session.add_all([c1, c2, benchmark])
    session.flush()

    # 2. Add prices
    # TEST1 goes from 10 to 11 (+10%), TEST2 goes from 20 to 22 (+10%)
    session.add_all([
        DailyPrice(company_id=c1.id, date=d1, close=10.0, adjusted_close=10.0),
        DailyPrice(company_id=c1.id, date=exec_d, open=10.0, close=10.0, adjusted_close=10.0),
        DailyPrice(company_id=c1.id, date=d2, close=11.0, adjusted_close=11.0),
        
        DailyPrice(company_id=c2.id, date=d1, close=20.0, adjusted_close=20.0),
        DailyPrice(company_id=c2.id, date=exec_d, open=20.0, close=20.0, adjusted_close=20.0),
        DailyPrice(company_id=c2.id, date=d2, close=22.0, adjusted_close=22.0),
        
        # Benchmark goes from 1000 to 1050 (+5%)
        DailyPrice(company_id=benchmark.id, date=d1, close=1000.0, adjusted_close=1000.0),
        DailyPrice(company_id=benchmark.id, date=d2, close=1050.0, adjusted_close=1050.0),
    ])

    # 3. Add scoring results (to satisfy the cache check)
    session.add_all([
        ScoringResult(company_id=c1.id, scoring_date=d1, composite_alpha=95.0, model_used="OPERATING"),
        ScoringResult(company_id=c2.id, scoring_date=d1, composite_alpha=90.0, model_used="OPERATING"),
        
        ScoringResult(company_id=c1.id, scoring_date=d2, composite_alpha=95.0, model_used="OPERATING"),
        ScoringResult(company_id=c2.id, scoring_date=d2, composite_alpha=90.0, model_used="OPERATING"),
    ])
    session.commit()

    # Mock the PortfolioSelector select method to return our test picks directly
    monkeypatch.setattr(
        PortfolioSelector,
        "select",
        lambda self, portfolio, session, current_holdings=None, exclude_tickers=None: [
            {
                "company_id": c1.id,
                "ticker": "TEST1",
                "score": 95.0,
                "quality_flags_json": '["DCF_OVERVALUED"]',
            },
            {"company_id": c2.id, "ticker": "TEST2", "score": 90.0},
        ]
    )

    # Run backtester
    backtester = BacktestEngine(session)
    # Target rebalance count is 2 (start d1, end d2)
    df = backtester.run_1y_backtest(
        start_date=d1,
        end_date=d2,
        export_dir=tmp_path,
    )

    assert not df.empty
    assert len(df) == 2

    # Check model_performance rows in database
    perf_rows = session.query(ModelPerformance).order_by(ModelPerformance.date).all()
    assert len(perf_rows) == 2

    # Row 1 (Initial point)
    assert perf_rows[0].date == d1.isoformat()
    assert perf_rows[0].strategy_return == 100.0
    assert perf_rows[0].benchmark_return == 100.0
    assert perf_rows[0].alpha == 0.0

    # Row 2 (After 1 week)
    # Expected returns:
    # Portfolio return = average of TEST1 (+10%) and TEST2 (+10%) = 10%
    # Friction = 0.4% round-trip on ALL stocks (both are new buys in week 1)
    # Net return = 10% - 0.4% = 9.6%
    # Strategy NAV = 100.0 * 1.096 = 109.6
    # Benchmark NAV = (1050/1000) * 100 * (1 - 0.005/52)^1 ≈ 104.99
    # Alpha ≈ 4.61
    assert perf_rows[1].date == d2.isoformat()
    assert perf_rows[1].strategy_return == pytest.approx(109.6, abs=0.1)
    assert perf_rows[1].benchmark_return == pytest.approx(104.99, abs=0.1)
    assert perf_rows[1].alpha == pytest.approx(4.61, abs=0.2)

    weekly_csv = tmp_path / "backtest_weekly_details_index_aware.csv"
    weekly = pd.read_csv(weekly_csv)
    row = weekly.iloc[-1]
    assert row["DcfOvervalued_Count"] == 1
    assert row["DcfOther_Count"] == 1
    assert row["DcfOvervalued_Avg_Return_Pct"] == pytest.approx(9.6)
    assert row["DcfOther_Avg_Return_Pct"] == pytest.approx(9.6)
    assert row["DcfOvervalued_Equity_Contribution_Pct"] == pytest.approx(4.8)
    assert backtester._last_run_meta["stale_score_cache_dates"] == 1
    assert (
        backtester._last_run_meta["scoring_pipeline_version"]
        == SCORING_PIPELINE_VERSION
    )


def test_same_day_open_fills_on_effective_session_not_next_day(session):
    friday = date(2026, 7, 10)
    monday = date(2026, 7, 13)
    tuesday = date(2026, 7, 14)
    company = Company(
        ticker="ASELS",
        name="Aselsan",
        company_type="OPERATING",
        is_active=True,
    )
    session.add(company)
    session.flush()
    session.add_all(
        [
            DailyPrice(
                company_id=company.id,
                date=friday,
                open=368.0,
                close=370.0,
                adjusted_close=370.0,
            ),
            DailyPrice(
                company_id=company.id,
                date=monday,
                open=355.0,
                close=358.0,
                adjusted_close=358.0,
            ),
            DailyPrice(
                company_id=company.id,
                date=tuesday,
                open=360.0,
                close=361.0,
                adjusted_close=361.0,
            ),
        ]
    )
    session.commit()

    engine = BacktestEngine(session)
    same_day_price, same_day_date = engine._get_entry_price(
        "ASELS", monday, tuesday, "same-day-open"
    )
    next_open_price, next_open_date = engine._get_entry_price(
        "ASELS", monday, tuesday, "next-open"
    )

    assert same_day_date == monday
    assert same_day_price == pytest.approx(355.0)
    assert next_open_date == tuesday
    assert next_open_price == pytest.approx(360.0)


def test_backtest_engine_treats_empty_selection_as_flat_period(
    session,
    monkeypatch,
    tmp_path,
):
    d1 = date(2026, 3, 16)
    d2 = date(2026, 3, 23)
    company = Company(ticker="REAL", name="Real", is_active=True)
    benchmark = Company(
        ticker="SPY",
        name="BIST 100",
        company_type="INDEX",
        is_active=False,
    )
    session.add_all([company, benchmark])
    session.flush()
    session.add_all([
        DailyPrice(
            company_id=benchmark.id,
            date=d1,
            close=1000.0,
            adjusted_close=1000.0,
        ),
        DailyPrice(
            company_id=benchmark.id,
            date=d2,
            close=1010.0,
            adjusted_close=1010.0,
        ),
        ScoringResult(
            company_id=company.id,
            scoring_date=d1,
            composite_alpha=80.0,
            pipeline_version=SCORING_PIPELINE_VERSION,
        ),
    ])
    session.commit()

    monkeypatch.setattr(
        PortfolioSelector,
        "select",
        lambda self, portfolio, session, current_holdings=None, exclude_tickers=None: [],
    )

    backtester = BacktestEngine(session)
    df = backtester.run_1y_backtest(
        start_date=d1,
        end_date=d2,
        export_dir=tmp_path,
    )

    assert df.iloc[-1]["strategy_return"] == pytest.approx(100.0)
    weekly = pd.read_csv(tmp_path / "backtest_weekly_details_index_aware.csv")
    row = weekly.iloc[-1]
    assert row["Position_Count"] == 0
    assert row["Equity_Return_Pct"] == pytest.approx(0.0)
    assert row["DcfOvervalued_Equity_Contribution_Pct"] == pytest.approx(0.0)
    assert row["DcfOther_Equity_Contribution_Pct"] == pytest.approx(0.0)


def test_incremental_summary_fetches_deflators_from_full_nav_start(session):
    session.add(
        ModelPerformance(
            date="2018-03-19",
            strategy_return=100.0,
            benchmark_return=100.0,
            alpha=0.0,
        )
    )
    session.commit()
    backtester = BacktestEngine(session)

    assert backtester._deflator_history_start(
        date(2026, 6, 29),
        persist_model_performance=True,
    ) == date(2018, 3, 19)
    assert backtester._deflator_history_start(
        date(2026, 6, 29),
        persist_model_performance=False,
    ) == date(2026, 6, 29)


def test_score_cache_version_distinguishes_current_and_legacy_rows(session):
    scoring_date = date(2026, 3, 16)
    company = Company(ticker="TEST1", name="Test 1", is_active=True)
    session.add(company)
    session.flush()
    cached = ScoringResult(
        company_id=company.id,
        scoring_date=scoring_date,
        composite_alpha=95.0,
        pipeline_version=SCORING_PIPELINE_VERSION,
    )
    session.add(cached)
    session.commit()

    assert _ensure_scores_for_date(session, scoring_date) is True

    cached.pipeline_version = None
    session.commit()
    assert _ensure_scores_for_date(session, scoring_date) is False
    assert (
        _ensure_scores_for_date(
            session,
            scoring_date,
            rebuild_stale_scores=True,
        )
        is True
    )
    assert session.query(ScoringResult).count() == 0


def test_composition_failure_leaves_score_date_stale(session, monkeypatch):
    scoring_date = date(2026, 3, 16)
    company = Company(ticker="REAL", name="Real", is_active=True)
    session.add(company)
    session.flush()
    session.add(
        ScoringResult(
            company_id=company.id,
            scoring_date=scoring_date,
            pipeline_version=SCORING_PIPELINE_VERSION,
        )
    )
    session.commit()

    called = {"value": False}

    def fail_composition(*args, **kwargs):
        called["value"] = True
        raise UnicodeEncodeError("cp1252", "→", 0, 1, "test")

    monkeypatch.setattr(
        "us_picker.scoring.composer.ScoreComposer.compose_all",
        fail_composition,
    )

    with pytest.raises(RuntimeError, match="score cache left stale"):
        _compose_scores_for_date(session, scoring_date)

    assert called["value"] is True
    row = session.query(ScoringResult).one()
    assert row.pipeline_version is None


def test_backtest_engine_take_profit_exit(session, monkeypatch, tmp_path):
    d1 = date(2026, 3, 16)
    d2 = date(2026, 3, 23)

    c1 = Company(ticker="TEST1", name="Test 1", company_type="OPERATING", is_active=True)
    benchmark = Company(ticker="SPY", name="BIST 100", company_type="INDEX", is_active=False)
    session.add_all([c1, benchmark])
    session.flush()

    # TEST1 has a mid-week high that hits take_profit.
    mid_date = d1 + timedelta(days=2)
    session.add_all([
        DailyPrice(company_id=c1.id, date=d1, close=10.0, adjusted_close=10.0),
        DailyPrice(
            company_id=c1.id,
            date=mid_date,
            open=10.0,
            high=15.0,
            low=9.0,
            close=12.0,
            adjusted_close=12.0,
        ),
        DailyPrice(company_id=c1.id, date=d2, close=12.0, adjusted_close=12.0),
        DailyPrice(company_id=benchmark.id, date=d1, close=1000.0, adjusted_close=1000.0),
        DailyPrice(company_id=benchmark.id, date=d2, close=1000.0, adjusted_close=1000.0),
    ])

    session.add_all([
        ScoringResult(company_id=c1.id, scoring_date=d1, composite_alpha=95.0, model_used="OPERATING"),
        ScoringResult(company_id=c1.id, scoring_date=d2, composite_alpha=95.0, model_used="OPERATING"),
    ])
    session.commit()

    monkeypatch.setattr(
        PortfolioSelector,
        "select",
        lambda self, portfolio, session, current_holdings=None, exclude_tickers=None: [
            {"company_id": c1.id, "ticker": "TEST1", "score": 95.0, "stop_loss": 8.0, "target_price": 14.0},
        ]
    )

    backtester = BacktestEngine(session)
    df = backtester.run_1y_backtest(
        start_date=d1,
        end_date=d2,
        export_dir=tmp_path,
    )

    perf_rows = session.query(ModelPerformance).order_by(ModelPerformance.date).all()
    assert len(perf_rows) == 2

    # TEST1 exits at the configured target price, not the optimistic daily high.
    # Return = 40%
    # Friction = 0.4% round-trip on new buy
    # Net return = 40% - 0.4% = 39.6%
    # Strategy NAV = 100 * 1.396 = 139.6
    assert perf_rows[1].strategy_return == pytest.approx(139.6, abs=0.1)


def test_backtest_engine_assumes_stop_first_when_daily_range_hits_stop_and_target(session):
    d1 = date(2026, 3, 16)
    d2 = date(2026, 3, 23)
    mid_date = d1 + timedelta(days=2)

    c1 = Company(ticker="TEST1", name="Test 1", company_type="OPERATING", is_active=True)
    session.add(c1)
    session.flush()
    session.add_all([
        DailyPrice(company_id=c1.id, date=d1, close=10.0, adjusted_close=10.0),
        DailyPrice(
            company_id=c1.id,
            date=mid_date,
            open=10.0,
            high=16.0,
            low=7.0,
            close=12.0,
            adjusted_close=12.0,
        ),
        DailyPrice(company_id=c1.id, date=d2, close=12.0, adjusted_close=12.0),
    ])
    session.commit()

    exit_price, exited, reason, exit_date = BacktestEngine(session)._get_weekly_exit(
        "TEST1",
        d1,
        d2,
        stop_loss=8.0,
        take_profit=14.0,
    )

    assert exited is True
    assert reason == "STOP_LOSS"
    assert exit_date == mid_date
    assert exit_price == pytest.approx(8.0)


def test_backtest_engine_gap_up_take_profit_exits_at_target_not_open(session):
    d1 = date(2026, 3, 16)
    d2 = date(2026, 3, 23)
    mid_date = d1 + timedelta(days=1)

    c1 = Company(ticker="TEST1", name="Test 1", company_type="OPERATING", is_active=True)
    session.add(c1)
    session.flush()
    session.add_all([
        DailyPrice(company_id=c1.id, date=d1, close=10.0, adjusted_close=10.0),
        DailyPrice(
            company_id=c1.id,
            date=mid_date,
            open=30.0,
            high=32.0,
            low=29.0,
            close=31.0,
            adjusted_close=31.0,
        ),
    ])
    session.commit()

    exit_price, exited, reason, exit_date = BacktestEngine(session)._get_weekly_exit(
        "TEST1",
        d1,
        d2,
        stop_loss=8.0,
        take_profit=14.0,
    )

    assert exited is True
    assert reason == "TAKE_PROFIT"
    assert exit_date == mid_date
    assert exit_price == pytest.approx(14.0)


def test_incremental_backtest_preserves_nav_continuity(
    session,
    monkeypatch,
    tmp_path,
):
    d1 = date(2026, 3, 16)
    d2 = date(2026, 3, 23)
    exec_d = d1 + timedelta(days=1)

    company = Company(
        ticker="TEST1",
        name="Test 1",
        company_type="OPERATING",
        is_active=True,
    )
    benchmark = Company(
        ticker="SPY",
        name="BIST 100",
        company_type="INDEX",
        is_active=False,
    )
    session.add_all([company, benchmark])
    session.flush()
    session.add_all([
        DailyPrice(company_id=company.id, date=d1, close=10.0, adjusted_close=10.0),
        DailyPrice(company_id=company.id, date=exec_d, open=10.0, close=10.0, adjusted_close=10.0),
        DailyPrice(company_id=company.id, date=d2, close=11.0, adjusted_close=11.0),
        DailyPrice(company_id=benchmark.id, date=d1, close=1000.0, adjusted_close=1000.0),
        DailyPrice(company_id=benchmark.id, date=d2, close=1050.0, adjusted_close=1050.0),
        ScoringResult(
            company_id=company.id,
            scoring_date=d1,
            composite_alpha=95.0,
            model_used="OPERATING",
        ),
    ])
    session.commit()

    monkeypatch.setattr(
        PortfolioSelector,
        "select",
        lambda self, portfolio, session, current_holdings=None, exclude_tickers=None: [
            {"company_id": company.id, "ticker": "TEST1", "score": 95.0},
        ],
    )

    BacktestEngine(session).run_1y_backtest(
        start_date=d1,
        end_date=d2,
        initial_strategy_nav=250.0,
        initial_benchmark_nav=180.0,
        previous_tickers={"TEST1"},
        export_dir=tmp_path,
    )

    rows = session.query(ModelPerformance).order_by(ModelPerformance.date).all()
    assert rows[0].strategy_return == pytest.approx(250.0)
    assert rows[0].benchmark_return == pytest.approx(180.0)
    assert rows[1].strategy_return == pytest.approx(275.0, abs=0.1)
    assert rows[1].benchmark_return == pytest.approx(188.98, abs=0.1)


def test_next_open_no_fill_counts_as_not_entered(session, monkeypatch, tmp_path):
    """B3: a pick that never prints after the signal cannot fill — its slot
    must sit in cash (0%, no friction), not silently redistribute to the
    other names or trade a stale pre-signal price."""
    d1 = date(2026, 3, 16)
    d2 = date(2026, 3, 23)
    exec_d = d1 + timedelta(days=1)

    liquid = Company(ticker="TEST1", name="Liquid", company_type="OPERATING", is_active=True)
    halted = Company(ticker="TEST2", name="Halted", company_type="OPERATING", is_active=True)
    session.add_all([liquid, halted])
    session.flush()
    session.add_all([
        DailyPrice(company_id=liquid.id, date=d1, close=10.0, adjusted_close=10.0),
        DailyPrice(company_id=liquid.id, date=exec_d, open=10.0, close=10.0, adjusted_close=10.0),
        DailyPrice(company_id=liquid.id, date=d2, close=11.0, adjusted_close=11.0),
        # Halted name prints ONLY on the signal date — no fill possible.
        DailyPrice(company_id=halted.id, date=d1, close=50.0, adjusted_close=50.0),
    ])
    session.commit()

    monkeypatch.setattr(
        PortfolioSelector,
        "select",
        lambda self, portfolio, session, current_holdings=None, exclude_tickers=None: [
            {"company_id": liquid.id, "ticker": "TEST1", "score": 95.0},
            {"company_id": halted.id, "ticker": "TEST2", "score": 90.0},
        ],
    )

    BacktestEngine(session).run_1y_backtest(
        start_date=d1,
        end_date=d2,
        export_dir=tmp_path,
    )

    rows = session.query(ModelPerformance).order_by(ModelPerformance.date).all()
    # TEST1: +10% - 0.4% friction = +9.6%; TEST2 slot: 0% (cash, no friction).
    # Average = +4.8% -> NAV 104.8. Old behavior averaged only TEST1 (109.6).
    assert rows[1].strategy_return == pytest.approx(104.8, abs=0.1)


def test_same_day_close_suspension_forces_haircut_exit(
    session,
    monkeypatch,
    tmp_path,
):
    """B3: a position with zero prints during the holding window exits at
    the last known price with a 20% haircut instead of a phantom flat week."""
    d1 = date(2026, 3, 16)
    d2 = date(2026, 3, 23)

    frozen = Company(ticker="TEST1", name="Frozen", company_type="OPERATING", is_active=True)
    session.add(frozen)
    session.flush()
    session.add(DailyPrice(company_id=frozen.id, date=d1, close=10.0, adjusted_close=10.0))
    session.commit()

    monkeypatch.setattr(
        PortfolioSelector,
        "select",
        lambda self, portfolio, session, current_holdings=None, exclude_tickers=None: [
            {"company_id": frozen.id, "ticker": "TEST1", "score": 95.0},
        ],
    )

    BacktestEngine(session).run_1y_backtest(
        start_date=d1,
        end_date=d2,
        execution_mode="same-day-close",
        export_dir=tmp_path,
    )

    rows = session.query(ModelPerformance).order_by(ModelPerformance.date).all()
    # Entry 10.0 at signal close, forced exit 8.0 (-20%), friction 0.4%.
    assert rows[1].strategy_return == pytest.approx(79.6, abs=0.1)


def test_get_weekly_exit_no_prices_returns_forced_haircut(session):
    d1 = date(2026, 3, 16)
    d2 = date(2026, 3, 23)
    c1 = Company(ticker="TEST1", name="Test 1", company_type="OPERATING", is_active=True)
    session.add(c1)
    session.flush()
    session.add(DailyPrice(company_id=c1.id, date=d1, close=10.0, adjusted_close=10.0))
    session.commit()

    exit_price, exited, reason, exit_date = BacktestEngine(session)._get_weekly_exit(
        "TEST1", d1, d2, stop_loss=8.0, take_profit=14.0
    )

    assert exited is True
    assert reason == "NO_PRICE_FORCED_EXIT"
    assert exit_price == pytest.approx(8.0)
    assert exit_date is None


def test_survivorship_audit_ignores_synthetic_mock_tickers(session):
    start = date(2026, 3, 16)
    end = date(2026, 3, 23)
    mock = Company(
        ticker="MOCK1",
        name="Synthetic",
        company_type="OPERATING",
        is_active=False,
    )
    test_macro = Company(
        ticker="TESTMACRO",
        name="Synthetic Macro",
        company_type="OPERATING",
        is_active=False,
    )
    real = Company(
        ticker="OLDCO",
        name="Old Company",
        company_type="OPERATING",
        is_active=False,
    )
    session.add_all([mock, test_macro, real])
    session.flush()
    session.add_all([
        DailyPrice(company_id=mock.id, date=start, close=10.0, adjusted_close=10.0),
        DailyPrice(company_id=test_macro.id, date=start, close=12.0, adjusted_close=12.0),
        DailyPrice(company_id=real.id, date=start, close=20.0, adjusted_close=20.0),
    ])
    session.commit()

    audit = BacktestEngine(session).audit_survivorship_risk(start, end)

    assert audit["inactive_priced_company_count"] == 1
    assert audit["sample"][0]["ticker"] == "OLDCO"


def test_investor_grade_execution_gate_describes_same_day_contract():
    gates = BacktestEngine._investor_grade_gates(
        [{"alpha_pct": 5.0, "max_drawdown_pct": -10.0}],
        {"flag_count": 0},
        {"status": "pass", "inactive_priced_company_count": 0},
        execution_mode="same_day_open",
    )

    execution = next(gate for gate in gates if gate["key"] == "execution")
    assert execution["status"] == "pass"
    assert "T-1 completed data" in execution["label"]
    assert "effective session" in execution["detail"]

