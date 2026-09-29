"""A1 (2026-07-07): real / hard-currency reporting in the backtest summary.

These are pure unit tests over ``BacktestEngine._summarize_performance`` — no
DB needed. They pin the deflated (CPI-real / gram-gold / USD) NAV math so the
nominal-TRY headline can never again be the only number reported.
"""

import pandas as pd
import pytest

from us_picker.backtest.engine import BacktestEngine


def _synthetic_nav_frame() -> pd.DataFrame:
    """Two-year, 3-point NAV where the real answer is known analytically.

    Nominal strategy NAV doubles (100 -> 200). Every deflator also doubles
    (10 -> 20), so the strategy's *real* total return is exactly 0%. The
    benchmark rises only 100 -> 150 nominal, i.e. -25% in the deflated unit,
    so deflated alpha is +25pp. The middle point is off the geometric line so
    deflated periodic returns vary (non-degenerate Sharpe).
    """
    return pd.DataFrame(
        {
            "date": ["2018-01-01", "2019-01-01", "2020-01-01"],
            "strategy_return": [100.0, 160.0, 200.0],
            "benchmark_return": [100.0, 130.0, 150.0],
            "gram_gold_try": [10.0, 14.1421356, 20.0],
            "usd_try": [10.0, 14.1421356, 20.0],
            "cpi_index": [10.0, 14.1421356, 20.0],
        }
    )


def test_nominal_headline_unchanged():
    summary = BacktestEngine._summarize_performance(_synthetic_nav_frame())
    # Nominal doubling => +100% total, ~41.4% CAGR over ~2 years.
    assert summary["total_return_pct"] == pytest.approx(100.0, abs=1e-6)
    assert summary["cagr_pct"] == pytest.approx(41.42, abs=0.2)
    assert summary["years"] == pytest.approx(2.0, abs=0.01)


@pytest.mark.parametrize("unit", ["gram_gold", "usd", "cpi_real"])
def test_deflated_real_return_is_zero_and_alpha_positive(unit):
    summary = BacktestEngine._summarize_performance(_synthetic_nav_frame())
    deflated = summary["deflated"]
    assert unit in deflated, f"expected deflated unit {unit!r}"
    metrics = deflated[unit]
    # Strategy exactly tracks the deflator end-to-end => ~0% real total return.
    assert metrics["total_return_pct"] == pytest.approx(0.0, abs=1e-6)
    # Benchmark lags the deflator => -25% real; alpha therefore ~+25pp.
    assert metrics["benchmark_total_return_pct"] == pytest.approx(-25.0, abs=1e-6)
    assert metrics["alpha_pct"] == pytest.approx(25.0, abs=1e-6)
    # Middle point is off the geometric line => a real Sharpe is computable.
    assert metrics["sharpe"] is not None


def test_deflated_absent_when_columns_missing():
    frame = _synthetic_nav_frame().drop(
        columns=["gram_gold_try", "usd_try", "cpi_index"]
    )
    summary = BacktestEngine._summarize_performance(frame)
    assert summary["deflated"] == {}
    # Nominal metrics still present.
    assert summary["total_return_pct"] == pytest.approx(100.0, abs=1e-6)


def test_deflator_with_nonpositive_values_is_skipped():
    frame = _synthetic_nav_frame()
    frame.loc[1, "usd_try"] = 0.0  # invalid deflator level
    summary = BacktestEngine._summarize_performance(frame)
    # usd unit dropped (non-positive), the other two survive.
    assert "usd" not in summary["deflated"]
    assert "gram_gold" in summary["deflated"]
    assert "cpi_real" in summary["deflated"]


def test_nav_metrics_handles_degenerate_series():
    # Single point / non-positive start must not raise.
    flat = BacktestEngine._nav_metrics(pd.Series([100.0]), years=1.0)
    assert flat["total_return_pct"] is None
    assert flat["sharpe"] is None


def test_tail_risk_block_present_and_sane():
    # A frame with a real drawdown so tail metrics are meaningful.
    frame = pd.DataFrame(
        {
            "date": ["2020-01-01", "2020-02-01", "2020-03-01", "2020-04-01", "2020-05-01"],
            "strategy_return": [100.0, 110.0, 70.0, 90.0, 120.0],
            "benchmark_return": [100.0, 105.0, 80.0, 88.0, 110.0],
        }
    )
    summary = BacktestEngine._summarize_performance(frame)
    tail = summary["tail_risk"]
    assert tail["worst_period_pct"] is not None
    assert tail["ulcer_index_pct"] is not None and tail["ulcer_index_pct"] >= 0
    # The −36% month is the worst single period.
    assert tail["worst_period_pct"] == pytest.approx(-36.3636, abs=0.01)


def test_backtest_config_defaults_reproduce_current_behavior():
    # Every default must be the no-op that keeps published numbers unchanged.
    from us_picker.backtest.engine import _load_backtest_config

    cfg = _load_backtest_config()
    assert cfg["idle_cash_yield"] == "none"
    assert cfg["commission_pct_per_side"] == 0.0
    assert cfg["bsmv_pct_of_commission"] == 0.0
    assert cfg["impact_enabled"] is False


def test_idle_yield_none_is_zero():
    from datetime import date as _date

    class _Dummy(BacktestEngine):
        def __init__(self):  # skip DB session
            self._gold_cache = None

    eng = _Dummy()
    assert eng._idle_yield("none", _date(2020, 1, 1), _date(2020, 1, 15)) == 0.0
    # Reversed/degenerate range is also 0.
    assert eng._idle_yield("repo", _date(2020, 1, 15), _date(2020, 1, 1)) == 0.0


def test_idle_yield_repo_is_positive_over_time():
    from datetime import date as _date

    class _Dummy(BacktestEngine):
        def __init__(self):
            self._gold_cache = None

        def _get_risk_free_rate(self, d):
            return 0.40  # 40% annual policy rate

    eng = _Dummy()
    y = eng._idle_yield("repo", _date(2020, 1, 1), _date(2020, 1, 15))
    # ~14 days at 40% annual -> small positive.
    assert 0.0 < y < 0.03
