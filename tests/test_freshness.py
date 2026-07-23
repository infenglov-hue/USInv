"""Phase 2.4 freshness / kill-switch gate tests (DATA_SPEC §8, §6)."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from usinv.calendar import default_calendar
from usinv.config.loader import load_config
from usinv.freshness import (
    DelistedAuditInput,
    FreshnessGateError,
    FundamentalsFreshnessInput,
    MacroFreshnessInput,
    PriceFreshnessInput,
    build_freshness_report,
    enforce_freshness_gate,
    evaluate_delisted_audit,
    evaluate_fundamentals_freshness,
    evaluate_macro_freshness,
    evaluate_price_freshness,
    filing_deadline,
    infer_next_periodic,
)

# 2026-02-17 16:00 ET (a full XNYS session; 2026-02-16 is Presidents' Day).
AS_OF = datetime(2026, 2, 17, 21, 0, tzinfo=UTC)
AS_OF_DATE = date(2026, 2, 17)


@pytest.fixture(scope="module")
def config():
    return load_config().freshness


def _fresh_fundamental() -> FundamentalsFreshnessInput:
    # Next 10-Q period ends 2026-01-31; due 45d + grace ⇒ deadline well after AS_OF.
    return FundamentalsFreshnessInput("SEC-FRESH", "other", "10-Q", date(2026, 1, 31))


def _stale_fundamental(security_id: str = "SEC-STALE") -> FundamentalsFreshnessInput:
    # Next 10-Q period ended 2025-09-30; deadline (~mid-Nov 2025) is long past.
    return FundamentalsFreshnessInput(security_id, "other", "10-Q", date(2025, 9, 30))


# --------------------------------------------------------------------------- #
# Config + helpers.
# --------------------------------------------------------------------------- #
def test_due_windows_match_blueprint(config):
    assert config.due_window_days("10-Q", "large_accelerated") == 40
    assert config.due_window_days("10-Q", "other") == 45
    assert config.due_window_days("10-K", "accelerated") == 75
    assert config.due_window_days("10-K", "other") == 90


def test_infer_next_periodic_advances_one_quarter():
    form, period_end = infer_next_periodic("10-Q", date(2025, 12, 31))
    assert form == "10-Q"
    assert period_end == date(2026, 3, 31)


def test_filing_input_rejects_unknown_category():
    with pytest.raises(FreshnessGateError):
        FundamentalsFreshnessInput("X", "mega", "10-Q", date(2025, 12, 31))


def test_filing_deadline_is_after_period_end(config):
    calendar = default_calendar()
    deadline = filing_deadline(_stale_fundamental(), config, calendar)
    assert deadline > date(2025, 9, 30)
    assert deadline < AS_OF_DATE


# --------------------------------------------------------------------------- #
# Fundamentals gate.
# --------------------------------------------------------------------------- #
def test_fundamentals_fresh_is_green(config):
    result = evaluate_fundamentals_freshness(
        [_fresh_fundamental()], scored_total=1, as_of=AS_OF_DATE, config=config
    )
    assert result.stale_security_ids == ()
    assert result.red is False


def test_fundamentals_over_threshold_is_red(config):
    inputs = [_fresh_fundamental(), *[_stale_fundamental(f"S{i}") for i in range(2)]]
    # 2 of 10 scored stale = 20% > 10% ⇒ red.
    result = evaluate_fundamentals_freshness(
        inputs, scored_total=10, as_of=AS_OF_DATE, config=config
    )
    assert len(result.stale_security_ids) == 2
    assert result.stale_fraction == pytest.approx(0.2)
    assert result.red is True


def test_fundamentals_at_threshold_is_green(config):
    # Exactly 10% (1 of 10) is NOT over the strict > threshold.
    result = evaluate_fundamentals_freshness(
        [_stale_fundamental()], scored_total=10, as_of=AS_OF_DATE, config=config
    )
    assert result.stale_fraction == pytest.approx(0.1)
    assert result.red is False


# --------------------------------------------------------------------------- #
# Price gate.
# --------------------------------------------------------------------------- #
def test_price_recent_bar_is_green(config):
    result = evaluate_price_freshness(
        [PriceFreshnessInput("P1", date(2026, 2, 13))], as_of=AS_OF_DATE, config=config
    )
    assert result.red is False
    assert result.stale_security_ids == ()


def test_price_old_bar_is_red(config):
    result = evaluate_price_freshness(
        [
            PriceFreshnessInput("P1", date(2026, 2, 13)),
            PriceFreshnessInput("P2", date(2026, 1, 15)),
        ],
        as_of=AS_OF_DATE,
        config=config,
    )
    assert result.stale_security_ids == ("P2",)
    assert result.red is True


# --------------------------------------------------------------------------- #
# Delisted-coverage audit.
# --------------------------------------------------------------------------- #
def test_delisted_audit_reconciled_is_green():
    result = evaluate_delisted_audit(
        DelistedAuditInput(
            active_symbols=frozenset({"AAA"}),
            delisted_symbols=frozenset({"BBB"}),
            master_active_symbols=frozenset({"AAA", "BBB"}),
        )
    )
    assert result.red is False


def test_delisted_audit_unexplained_gap_is_red():
    result = evaluate_delisted_audit(
        DelistedAuditInput(
            active_symbols=frozenset({"AAA"}),
            delisted_symbols=frozenset(),
            master_active_symbols=frozenset({"AAA", "CCC"}),
        )
    )
    assert result.unexplained_symbols == ("CCC",)
    assert result.red is True


# --------------------------------------------------------------------------- #
# Macro gate.
# --------------------------------------------------------------------------- #
def test_macro_within_two_cadences_is_green(config):
    result = evaluate_macro_freshness(
        [MacroFreshnessInput("NFCI", date(2026, 2, 10), cadence_days=7)],
        as_of=AS_OF_DATE,
        config=config,
    )
    assert result.red is False


def test_macro_past_max_age_is_red(config):
    result = evaluate_macro_freshness(
        [MacroFreshnessInput("NFCI", date(2026, 1, 1), cadence_days=7)],
        as_of=AS_OF_DATE,
        config=config,
    )
    assert result.stale_series_ids == ("NFCI",)
    assert result.red is True


# --------------------------------------------------------------------------- #
# Aggregate report + KILL-SWITCH demo (the Phase 2.4 acceptance gate).
# --------------------------------------------------------------------------- #
def _all_fresh_report(config):
    return build_freshness_report(
        as_of=AS_OF,
        fundamentals_inputs=[_fresh_fundamental()],
        scored_total=1,
        price_inputs=[PriceFreshnessInput("P1", date(2026, 2, 13))],
        delisted=DelistedAuditInput(
            frozenset({"AAA"}), frozenset({"BBB"}), frozenset({"AAA", "BBB"})
        ),
        macro_inputs=[MacroFreshnessInput("NFCI", date(2026, 2, 10), 7)],
        config=config,
    )


def test_all_fresh_report_passes_and_does_not_raise(config):
    report = _all_fresh_report(config)
    assert report.passed is True
    assert report.red_reasons == ()
    enforce_freshness_gate(report)  # must not raise
    assert report.health_block()["passed"] is True


def test_stale_fixture_trips_kill_switch(config):
    # Artificially stale price fixture ⇒ the gate goes red and blocks delivery.
    report = build_freshness_report(
        as_of=AS_OF,
        fundamentals_inputs=[_fresh_fundamental()],
        scored_total=1,
        price_inputs=[PriceFreshnessInput("P1", date(2026, 1, 15))],
        delisted=DelistedAuditInput(
            frozenset({"AAA"}), frozenset({"BBB"}), frozenset({"AAA", "BBB"})
        ),
        config=config,
    )
    assert report.passed is False
    assert any("prices" in reason for reason in report.red_reasons)
    with pytest.raises(FreshnessGateError):
        enforce_freshness_gate(report)


def test_build_report_requires_tz_aware_as_of(config):
    with pytest.raises(FreshnessGateError):
        build_freshness_report(
            as_of=datetime(2026, 2, 17, 16, 0),
            fundamentals_inputs=[],
            scored_total=0,
            price_inputs=[],
            delisted=DelistedAuditInput(frozenset(), frozenset(), frozenset()),
            config=config,
        )
