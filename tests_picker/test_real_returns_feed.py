"""L1 (2026-07-10): real-returns pipeline backtest → manifest.

Pins the artifact contract between backtest/engine.persist_daily_summary and
mobile_feed._load_real_returns so the PWA decision card can show CPI/USD/gold
returns: write→load roundtrip, stale-file drop, corrupt-file drop, and the
None-unit skip for unavailable deflators.
"""

import json
from datetime import date, datetime, timedelta, timezone

from us_picker.backtest.engine import persist_daily_summary
from us_picker.mobile_feed import _load_real_returns

SUMMARY = {
    "total_return_pct": 2085.5,
    "benchmark_return_pct": 547.2,
    "alpha_pct": 1538.2,
    "cagr_pct": 45.1,
    "sharpe_nominal": 1.53,
    "max_drawdown_pct": -19.1,
    "deflated": {
        "cpi_real": {"total_return_pct": 79.5, "cagr_pct": 7.3, "max_drawdown_pct": -31.7},
        "usd": {"total_return_pct": 92.0, "cagr_pct": 8.2, "max_drawdown_pct": -28.0},
        "gram_gold": {"total_return_pct": None},  # deflator unavailable
    },
}


def test_roundtrip_write_then_load(tmp_path):
    target = tmp_path / "backtest_daily_summary.json"
    persist_daily_summary(SUMMARY, date(2018, 3, 19), date(2026, 7, 6), path=target)

    block = _load_real_returns(path=target)
    assert block is not None
    assert block["window"] == {"start": "2018-03-19", "end": "2026-07-06"}
    assert block["nominal"]["total_return_pct"] == 2085.5
    assert block["nominal"]["sharpe"] == 1.53
    assert block["deflated"]["cpi_real"]["total_return_pct"] == 79.5
    # Unavailable deflator serializes as None so the PWA can skip it.
    assert block["deflated"]["gram_gold"] is None


def test_stale_file_is_dropped(tmp_path):
    target = tmp_path / "backtest_daily_summary.json"
    persist_daily_summary(SUMMARY, date(2018, 3, 19), date(2026, 7, 6), path=target)

    week_later = datetime.now(timezone.utc) + timedelta(days=7)
    assert _load_real_returns(path=target, now=week_later) is None
    # Within the age window it still loads.
    tomorrow = datetime.now(timezone.utc) + timedelta(days=1)
    assert _load_real_returns(path=target, now=tomorrow) is not None


def test_missing_or_corrupt_file_is_none(tmp_path):
    assert _load_real_returns(path=tmp_path / "missing.json") is None

    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{not json", encoding="utf-8")
    assert _load_real_returns(path=corrupt) is None

    no_deflated = tmp_path / "nodef.json"
    no_deflated.write_text(
        json.dumps({"generated_at": datetime.now(timezone.utc).isoformat()}),
        encoding="utf-8",
    )
    assert _load_real_returns(path=no_deflated) is None


def test_bad_generated_at_is_none(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps({"generated_at": "dun aksam", "deflated": {}}),
        encoding="utf-8",
    )
    assert _load_real_returns(path=bad) is None
