from datetime import date
from pathlib import Path

from us_picker.cli import (
    _resolve_backtest_rebalance_weeks,
    _should_skip_portfolio_rotation,
    _week_start_monday,
)
from us_picker.portfolio.rotation import (
    load_rotation_config,
    next_rotation_date,
    rotation_cycle_start,
    week_start_monday,
)

# Production anchor: 2026-06-29 is a rotation Monday (bi-weekly cadence).
ANCHOR = date(2026, 6, 29)


def test_week_start_monday_returns_current_week_monday():
    assert _week_start_monday(date(2026, 7, 1)) == date(2026, 6, 29)
    assert _week_start_monday(date(2026, 6, 29)) == date(2026, 6, 29)
    assert week_start_monday(date(2026, 7, 5)) == date(2026, 6, 29)


class TestRotationCycleStart:
    def test_weekly_cadence_is_week_monday(self):
        assert rotation_cycle_start(date(2026, 7, 1), 1, None) == date(2026, 6, 29)
        # No anchor -> weekly behavior even with weeks > 1
        assert rotation_cycle_start(date(2026, 7, 1), 2, None) == date(2026, 6, 29)

    def test_biweekly_on_rotation_week(self):
        # Week of the anchor itself
        assert rotation_cycle_start(date(2026, 6, 29), 2, ANCHOR) == ANCHOR
        assert rotation_cycle_start(date(2026, 7, 4), 2, ANCHOR) == ANCHOR

    def test_biweekly_on_off_week_points_back_to_rotation_monday(self):
        # 2026-07-06 is a Monday but an off-cycle one
        assert rotation_cycle_start(date(2026, 7, 6), 2, ANCHOR) == ANCHOR
        assert rotation_cycle_start(date(2026, 7, 12), 2, ANCHOR) == ANCHOR

    def test_biweekly_next_cycle(self):
        assert rotation_cycle_start(date(2026, 7, 13), 2, ANCHOR) == date(2026, 7, 13)
        assert rotation_cycle_start(date(2026, 7, 20), 2, ANCHOR) == date(2026, 7, 13)

    def test_dates_before_anchor_keep_parity(self):
        assert rotation_cycle_start(date(2026, 6, 15), 2, ANCHOR) == date(2026, 6, 15)
        assert rotation_cycle_start(date(2026, 6, 22), 2, ANCHOR) == date(2026, 6, 15)


class TestNextRotationDate:
    def test_on_rotation_monday_next_is_one_cycle_ahead(self):
        assert next_rotation_date(date(2026, 6, 29), 2, ANCHOR) == date(2026, 7, 13)

    def test_mid_cycle(self):
        assert next_rotation_date(date(2026, 7, 6), 2, ANCHOR) == date(2026, 7, 13)
        assert next_rotation_date(date(2026, 7, 12), 2, ANCHOR) == date(2026, 7, 13)


class TestShouldSkipRotation:
    CYCLE = ANCHOR  # cycle containing 2026-06-29 .. 2026-07-12

    def test_rotation_monday_with_stale_selection_rotates(self):
        assert _should_skip_portfolio_rotation(ANCHOR, date(2026, 6, 15), self.CYCLE) is False

    def test_same_cycle_selection_skips_even_on_off_monday(self):
        # 2026-07-06: off-cycle Monday; portfolio from 06-29 must be kept
        assert _should_skip_portfolio_rotation(date(2026, 7, 6), date(2026, 6, 29), self.CYCLE) is True
        assert _should_skip_portfolio_rotation(date(2026, 7, 3), date(2026, 6, 29), self.CYCLE) is True

    def test_second_run_on_rotation_day_skips(self):
        assert _should_skip_portfolio_rotation(ANCHOR, ANCHOR, self.CYCLE) is True

    def test_missed_rotation_monday_catches_up(self):
        # Rotation Monday 06-29 missed; Wednesday run must rotate once
        assert _should_skip_portfolio_rotation(date(2026, 7, 1), date(2026, 6, 15), self.CYCLE) is False

    def test_no_selection_always_rotates(self):
        assert _should_skip_portfolio_rotation(date(2026, 7, 6), None, self.CYCLE) is False


class TestLoadRotationConfig:
    def test_production_config_is_biweekly_anchored(self):
        weeks, anchor = load_rotation_config()
        assert weeks == 2
        assert anchor == ANCHOR
        assert anchor.weekday() == 0

    def test_missing_file_falls_back_to_weekly(self, tmp_path: Path):
        weeks, anchor = load_rotation_config(tmp_path / "missing.yaml")
        assert weeks == 1
        assert anchor is None

    def test_bad_values_fall_back(self, tmp_path: Path):
        cfg = tmp_path / "thresholds.yaml"
        cfg.write_text(
            "selection:\n  rotation_weeks: banana\n  rotation_anchor_monday: not-a-date\n",
            encoding="utf-8",
        )
        weeks, anchor = load_rotation_config(cfg)
        assert weeks == 1
        assert anchor is None

    def test_non_monday_anchor_normalized_to_monday(self, tmp_path: Path):
        cfg = tmp_path / "thresholds.yaml"
        cfg.write_text(
            "selection:\n  rotation_weeks: 2\n  rotation_anchor_monday: \"2026-07-01\"\n",
            encoding="utf-8",
        )
        weeks, anchor = load_rotation_config(cfg)
        assert weeks == 2
        assert anchor == date(2026, 6, 29)


class TestBacktestRebalanceDefault:
    def test_none_uses_live_rotation_config(self):
        assert _resolve_backtest_rebalance_weeks(None) == 2

    def test_cli_override_wins(self):
        assert _resolve_backtest_rebalance_weeks(4) == 4
        assert _resolve_backtest_rebalance_weeks(0) == 1
